"""Reference lists with proposals and admin decisions (WM-101), on PostgreSQL with demo data only."""

import datetime as dt
import re

import pytest
import sqlalchemy as sa

from metrics import analytics, auth, courses, lists
from metrics.db import (
    ABGESCHLOSSEN,
    CONFIRMED,
    DURCHGEFUEHRT,
    MERGED,
    PROPOSED,
    REJECTED,
    Appointment,
    Context,
    Course,
    Format,
    HistoryEntry,
    Person,
    Place,
    Proposal,
)
from metrics.ingest import store
from tests.test_web import OIDC, _client, _fake_provider, _noop, _oidc_login

TODAY = dt.date(2026, 11, 13)


@pytest.fixture
def seeded(db, instance):
    store.seed_reference(db, instance)
    db.commit()
    return db


def _place(db, name) -> Place:
    return db.scalars(sa.select(Place).where(Place.name == name)).one()


def _history(db, table, row_id) -> list[HistoryEntry]:
    return list(
        db.scalars(
            sa.select(HistoryEntry)
            .where(HistoryEntry.table_name == table, HistoryEntry.row_id == row_id)
            .order_by(HistoryEntry.id)
        )
    )


NEW_PLACE = {"name": "Jugendhaus Seeblick", "is_school": False, "address": "Seestraße 2, 83700 Musterstadt"}


# --- Proposals, decisions, Verlauf ---------------------------------------------------------------


def test_a_proposal_is_usable_at_once_and_recorded(seeded):
    result = lists.propose(seeded, lists.PLACES, NEW_PLACE, "Kim", "für den Kurs am 24.11.")
    place = result.entry
    assert result.created and place.status == PROPOSED and place.id
    proposal = seeded.scalars(sa.select(Proposal).where(Proposal.target_id == place.id)).one()
    assert (proposal.kind, proposal.target_table, proposal.proposed_by) == ("eintrag", "places", "Kim")
    assert proposal.proposed_values["address"] == NEW_PLACE["address"] and proposal.decision is None
    (row,) = _history(seeded, "places", place.id)
    assert (row.field, row.new_value, row.changed_by) == ("status", PROPOSED, "Kim")
    assert place in lists.choices(seeded, lists.PLACES)


def test_a_known_name_is_not_proposed_twice(seeded):
    result = lists.propose(seeded, lists.PLACES, {**NEW_PLACE, "name": "makerspace"}, "Kim")
    assert not result.created and result.entry.name == "Makerspace" and "gibt es schon" in result.note


def test_proposal_needs_the_details(seeded):
    types = lists.school_types(seeded)
    _, errors = lists.parse(lists.PLACES, {"name": "Neue Schule", "is_school": "ja"}, types, new=True)
    assert set(errors) == {"school_type", "address"}
    _, errors = lists.parse(lists.FORMATS, {"name": "Löten", "duration_hours": "x"}, types, new=True)
    assert set(errors) == {"category", "duration_hours"}
    values, errors = lists.parse(
        lists.FORMATS, {"name": "Löten", "category": "Engineering", "duration_hours": "1,5"}, types, new=True
    )
    assert not errors and str(values["duration_hours"]) == "1.5"


def test_confirm_needs_the_details_and_decides_the_proposal(seeded):
    place = lists.propose(seeded, lists.PLACES, {"name": "Nur ein Name"}, "Kim").entry
    with pytest.raises(lists.ListError, match="Schule"):
        lists.confirm(seeded, lists.PLACES, place, "Alex")
    lists.update(seeded, lists.PLACES, place, {"is_school": False, "address": ""}, "Kim", admin=False)
    lists.confirm(seeded, lists.PLACES, place, "Alex")
    assert place.status == CONFIRMED
    proposal = lists.open_proposal(seeded, place)
    assert (proposal.decision, proposal.decided_by) == ("bestätigt", "Alex") and proposal.decided_at
    assert [h.field for h in _history(seeded, "places", place.id)] == [
        "status",
        "is_school",
        "address",
        "status",
    ]


def test_team_edits_proposed_entries_only(seeded):
    with pytest.raises(lists.ListError, match="Admin"):
        lists.update(seeded, lists.PLACES, _place(seeded, "Makerspace"), {"municipality": "X"}, "Kim", False)
    lists.update(seeded, lists.PLACES, _place(seeded, "Makerspace"), {"municipality": "X"}, "Alex", True)
    assert _place(seeded, "Makerspace").municipality == "X"


def test_reject_needs_a_reason_and_keeps_the_row(seeded):
    place = lists.propose(seeded, lists.PLACES, NEW_PLACE, "Kim").entry
    course = courses.create_course(seeded, by="Kim", place_id=place.id)
    with pytest.raises(lists.ListError, match="Grund"):
        lists.reject(seeded, lists.PLACES, place, "Alex", " ")
    assert lists.reject(seeded, lists.PLACES, place, "Alex", "kein Kursort") == 1
    assert place.status == REJECTED and course.place_id == place.id  # still on its course
    assert lists.open_proposal(seeded, place).decision_reason == "kein Kursort"
    assert place not in lists.choices(seeded, lists.PLACES)
    assert place in lists.choices(seeded, lists.PLACES, selected_id=place.id)  # the form keeps it
    with pytest.raises(lists.ListError, match="abgelehnt"):
        lists.propose(seeded, lists.PLACES, NEW_PLACE, "Kim")
    with pytest.raises(lists.ListError):
        lists.reject(
            seeded, lists.PLACES, _place(seeded, "Makerspace"), "Alex", "confirmed entries are merged"
        )


def test_merge_moves_courses_and_target_groups_and_keeps_the_row(seeded):
    place = lists.propose(seeded, lists.PLACES, {**NEW_PLACE, "name": "GS Musterstadt"}, "Kim").entry
    course = courses.create_course(seeded, by="Kim", place_id=place.id, class_age_text="Kl. 3")
    assert course.target_group == "Kinder & Jugendliche außerschulisch"  # proposed as no school
    target = _place(seeded, "Grundschule Musterstadt")
    assert lists.merge(seeded, lists.PLACES, place, target, "Alex", "gleiche Schule") == 1
    seeded.refresh(course)
    assert course.place_id == target.id and course.target_group == "Schulklasse"
    assert (place.status, place.merged_into_id) == (MERGED, target.id)
    fields = {(h.field, h.old_value, h.new_value) for h in _history(seeded, "courses", course.id)}
    assert ("place_id", str(place.id), str(target.id)) in fields
    assert ("target_group", "Kinder & Jugendliche außerschulisch", "Schulklasse") in fields
    assert all(
        "gleiche Schule" in h.reason
        for h in _history(seeded, "courses", course.id)
        if h.reason != "Kurs angelegt"
    )
    assert lists.open_proposal(seeded, place).decision == "zusammengeführt"
    assert lists.resolve(seeded, place) is target
    # A later proposal or import with the merged name lands on the target.
    assert lists.propose(seeded, lists.PLACES, {**NEW_PLACE, "name": "GS Musterstadt"}, "Kim").entry is target
    assert store._Lists(seeded, None).place("GS Musterstadt") is target
    with pytest.raises(lists.ListError):
        lists.merge(seeded, lists.PLACES, target, place, "Alex", None)  # never into a merged entry


def test_merge_people_moves_lead_and_support(seeded):
    kim = lists.propose(seeded, lists.PEOPLE, {"name": "Kim", "email": "kim@example.org"}, "Lea").entry
    kim2 = lists.propose(seeded, lists.PEOPLE, {"name": "Kim M."}, "Lea").entry
    lea = lists.propose(seeded, lists.PEOPLE, {"name": "Lea"}, "Lea").entry
    course = courses.create_course(seeded, by="Lea")
    first = courses.add_appointment(
        seeded, course, by="Lea", date=TODAY, lead_id=kim2.id, support_ids=(lea.id,)
    )
    second = courses.add_appointment(
        seeded, course, by="Lea", date=TODAY, lead_id=lea.id, support_ids=(kim2.id, kim.id)
    )
    assert lists.merge(seeded, lists.PEOPLE, kim2, kim, "Alex", None) == 2
    assert first.lead_id == kim.id and [p.id for p in first.support] == [lea.id]
    assert second.lead_id == lea.id and [p.id for p in second.support] == [kim.id]


def test_course_target_group_matches_the_imported_rule(db, instance, baseline):
    store.load_baseline(db, baseline, instance, "demo", "0" * 64)
    found = db.scalars(sa.select(Course)).all()
    assert found and all(lists.course_target_group(c) == c.target_group for c in found)


def test_teacher_training_flag_changes_target_groups(seeded):
    context = lists.propose(seeded, lists.CONTEXTS, {"name": "Fortbildung X"}, "Kim").entry
    course = courses.create_course(seeded, by="Kim", context_id=context.id, class_age_text="40 Jahre")
    assert course.target_group == "Erwachsene"
    lists.update(seeded, lists.CONTEXTS, context, {"teacher_training": True}, "Kim", admin=False)
    assert course.target_group == "Lehrkräfte"


# --- Final confirmation waits for the list entries -----------------------------------------------


def _fill_required(db, termin: Appointment) -> None:
    """Everything else the final confirmation needs (WM-113), with confirmed demo entries."""
    course = termin.course
    course.context = course.context or db.scalars(sa.select(Context).order_by(Context.id)).first()
    if course.format is None:
        course.format = Format(name="Calliope-Einsteiger", category="Code", status=CONFIRMED)
    course.class_age_text = course.class_age_text or "Kl. 4"
    termin.start_time, termin.end_time = dt.time(9), dt.time(11)
    termin.participants_female, termin.participants_male = 5, 4
    termin.participants_total = 9
    termin.lead = db.scalars(sa.select(Person).order_by(Person.id)).first()
    db.flush()


def test_final_confirmation_waits_for_confirmed_entries(seeded):
    place = lists.propose(seeded, lists.PLACES, NEW_PLACE, "Kim").entry
    fmt = lists.propose(
        seeded, lists.FORMATS, {"name": "Löten", "category": "Engineering", "duration_hours": 2}, "Kim"
    ).entry
    course = courses.create_course(seeded, by="Kim", place_id=place.id, format_id=fmt.id)
    termin = courses.add_appointment(seeded, course, by="Kim", date=TODAY, stage=DURCHGEFUEHRT)
    _fill_required(seeded, termin)
    assert [p.text for p in lists.pending_entries(termin)] == [
        "Ort „Jugendhaus Seeblick“ wartet auf Bestätigung",
        "Format „Löten (Engineering)“ wartet auf Bestätigung",
    ]
    with pytest.raises(courses.NotReady):
        courses.finally_confirm(seeded, termin, "Kim")
    lists.confirm(seeded, lists.PLACES, place, "Alex")
    lists.reject(seeded, lists.FORMATS, fmt, "Alex", "heißt anders")
    assert not lists.entries_confirmed(termin)
    assert "abgelehnt" in lists.pending_entries(termin)[0].text
    lists.merge(
        seeded,
        lists.FORMATS,
        fmt,
        lists.propose(
            seeded, lists.FORMATS, {"name": "Lötkurs", "category": "Engineering", "duration_hours": 2}, "Kim"
        ).entry,
        "Alex",
        None,
    )
    seeded.refresh(course)
    lists.confirm(seeded, lists.FORMATS, course.format, "Alex")
    courses.finally_confirm(seeded, termin, "Kim")
    assert termin.stage == ABGESCHLOSSEN


# --- Definitions from the contexts ---------------------------------------------------------------


def test_projects_and_teacher_contexts_come_from_the_contexts(seeded, instance):
    assert [(p.slug, p.title, p.contexts) for p in lists.projects(seeded)] == [
        ("projekt-a", "Projekt A", ("Projekt A",))
    ]
    assert lists.teacher_contexts(seeded) == instance.teacher_contexts
    context = seeded.scalars(sa.select(Context).where(Context.name == "Projekt A")).one()
    lists.update(seeded, lists.CONTEXTS, context, {"page_title": "Projekt Alpha"}, "Alex", admin=True)
    extra = lists.propose(seeded, lists.CONTEXTS, {"name": "Projekt A Ferien"}, "Kim").entry
    lists.update(seeded, lists.CONTEXTS, extra, {"page_slug": "projekt-a"}, "Kim", admin=False)
    seeded.commit()
    store.seed_reference(seeded, instance)  # adds only what is missing, never overwrites an edit
    assert lists.project(seeded, "projekt-a").title == "Projekt Alpha"
    assert lists.project(seeded, "projekt-a").contexts == ("Projekt A", "Projekt A Ferien")


def test_place_problems_cover_the_old_location_check(seeded, instance):
    types = lists.school_types(seeded)
    assert lists.place_problems(_place(seeded, "Online"), types, instance.map) == []
    # The location list keeps no address for some places ("" = none on purpose, not unknown).
    assert lists.place_problems(_place(seeded, "Gymnasium Nachbarstadt"), types, instance.map) == []
    # Coordinates in Neudorf, municipality says Musterstadt.
    shifted = Place(
        name="X", is_school=False, address="Weg 2", lat=48.2, lon=11.3, municipality="Musterstadt"
    )
    assert lists.place_problems(shifted, types, instance.map) == [
        "Gemeinde passt nicht zu den Koordinaten (Neudorf)"
    ]
    odd = Place(
        name="Seltsam",
        is_school=True,
        school_type="XX",
        address="Weg 1",
        lat=0.0,
        lon=0.0,
        municipality="Musterstadt",
    )
    assert lists.place_problems(odd, types, instance.map) == [
        "Schulart XX unbekannt",
        "Koordinaten außerhalb der Karte",
    ]
    assert lists.place_problems(Place(name="Neu"), types, None) == ["Schule? offen", "Adresse fehlt"]


# --- Login linked by e-mail ----------------------------------------------------------------------


def test_login_links_the_person_by_email_once(seeded):
    kim = lists.propose(seeded, lists.PEOPLE, {"name": "Kim", "email": "Kim@Example.org"}, "Alex").entry
    lists.propose(seeded, lists.PEOPLE, {"name": "Alex K"}, "Alex")  # same name as a login: never used
    assert lists.link_login(seeded, "sub-1", None) is None
    assert lists.link_login(seeded, "sub-1", "kim@example.org") is kim
    assert kim.login_subject == "sub-1"
    assert lists.link_login(seeded, "sub-1", "other@example.org") is kim  # linked for good
    assert lists.link_login(seeded, "sub-2", "kim@example.org") is None  # already linked to another login
    (row,) = [h for h in _history(seeded, "people", kim.id) if h.field == "login_subject"]
    assert row.changed_by == lists.LOGIN


def test_oidc_login_links_the_person(seeded, monkeypatch):
    seeded.add(Person(name="Alex", email="alex@example.org"))
    seeded.add(Person(name="Alex K"))
    seeded.commit()
    client = _client(monkeypatch, **OIDC)
    seen = _fake_provider(monkeypatch, claims_override={"sub": "kc-1", "email": "alex@example.org"})
    assert _oidc_login(client, seen).status_code == 303
    seeded.expire_all()
    assert seeded.scalars(sa.select(Person).where(Person.login_subject == "kc-1")).one().name == "Alex"

    # An e-mail the provider marks as unverified links nobody.
    seeded.add(Person(name="Lea", email="lea@example.org"))
    seeded.commit()
    seen = _fake_provider(
        monkeypatch, claims_override={"sub": "kc-2", "email": "lea@example.org", "email_verified": False}
    )
    assert _oidc_login(_client(monkeypatch, **OIDC), seen).status_code == 303
    assert seeded.scalars(sa.select(Person).where(Person.login_subject == "kc-2")).first() is None


# --- Pages and the picker endpoint ---------------------------------------------------------------


def _team(monkeypatch):
    monkeypatch.setattr("metrics.web.login.asyncio.sleep", lambda _: _noop())
    client = _client(monkeypatch)
    assert (
        client.post("/login", data={"password": "richtiges-passwort"}, follow_redirects=False).status_code
        == 303
    )
    return client


def _admin(monkeypatch):
    return _client(monkeypatch, auth_mode="off")


def _selected(html: str) -> str:
    return re.search(r'<option value="(\d+)" selected>', html).group(1)


def test_list_pages_search_filter_and_detail(seeded, monkeypatch):
    client = _team(monkeypatch)
    for slug in lists.KINDS:
        # The Personen page is for admins only (WM-105, tests/test_people.py).
        assert client.get(f"/stammdaten/{slug}").status_code == (403 if slug == "personen" else 200)
    html = client.get("/stammdaten/orte?q=muster").text
    assert "Grundschule Musterstadt" in html and "Makerspace" not in html
    seeded.add(Place(name="Turnhalle", status=PROPOSED))  # e.g. from the Slack transfer
    seeded.commit()
    gaps = client.get("/stammdaten/orte?luecken=1").text
    assert "Turnhalle" in gaps and "Adresse fehlt" in gaps and "Makerspace" not in gaps
    place = _place(seeded, "Makerspace")
    detail = client.get(f"/stammdaten/orte?id={place.id}").text
    assert 'id="eintrag"' in detail and "Bestätigte Einträge ändert ein Admin." in detail
    assert f'action="/stammdaten/orte/{place.id}/zusammenfuehren"' not in detail  # admins only
    assert client.get("/stammdaten/unbekannt").status_code == 404
    # A team member cannot change a confirmed entry, even by posting directly.
    response = client.post(f"/stammdaten/orte/{place.id}", data={"name": "Werkstatt", "is_school": "nein"})
    assert response.status_code == 422 and _place(seeded, "Makerspace").name == "Makerspace"

    created = client.post(
        "/stammdaten/kontexte", data={"name": "Ferienprogramm", "reason": "neu"}, follow_redirects=False
    )
    assert created.status_code == 303 and "Ferienprogramm" not in created.headers["location"]
    proposed = client.get("/stammdaten/kontexte?status=vorgeschlagen").text
    assert "Ferienprogramm" in proposed and 'title="1 Kontexte warten auf Bestätigung"' in proposed


def test_picker_endpoint(seeded, monkeypatch):
    client = _team(monkeypatch)
    place = _place(seeded, "Makerspace")
    html = client.get(f"/stammdaten/orte/auswahl?feld=place_id&place_id={place.id}").text
    assert 'id="picker-place_id"' in html and _selected(html) == str(place.id)
    assert "keine Schule · Beispieldorf" in html and "Neu vorschlagen" in html
    form = client.get("/stammdaten/orte/auswahl?feld=place_id&neu=1").text
    assert 'name="neu-place_id-address"' in form and 'name="neu-place_id-is_school"' in form
    assert " required" not in form  # the surrounding form must still submit

    missing = client.post("/stammdaten/orte/auswahl?feld=place_id", data={"neu-place_id-name": "Halle"})
    assert "Pflichtfeld" in missing.text and 'value="Halle"' in missing.text
    assert client.get("/stammdaten/orte/auswahl").status_code == 400
    assert (
        client.get("/stammdaten/formate/auswahl?feld=format_id&neu=1").text.count('name="neu-format_id-') == 3
    )


def test_done_when_a_proposed_place_is_used_merged_and_counted(seeded, monkeypatch):
    """WM-101 Done when: a team member proposes a place through the picker while creating a Termin,
    the Termin uses it at once, the admin merges it, and course, Verlauf and charts follow."""
    team = _team(monkeypatch)
    answer = team.post(
        "/stammdaten/orte/auswahl?feld=place_id&label=Ort",
        data={
            "neu-place_id-name": "Makerspace Beispieldorf",
            "neu-place_id-is_school": "nein",
            "neu-place_id-address": "Werkstattstraße 5, Beispieldorf",
        },
    )
    assert answer.status_code == 200 and "wartet auf Bestätigung" in answer.text
    place_id = int(_selected(answer.text))
    proposed = seeded.get(Place, place_id)
    assert proposed.status == PROPOSED

    # The Termin uses it at once (course and Termin through the service; planning UI is WM-110).
    course = courses.create_course(seeded, by="Team", place_id=place_id, class_age_text="12 Jahre")
    termin = courses.add_appointment(
        seeded, course, by="Team", date=dt.date(2026, 11, 10), stage=DURCHGEFUEHRT
    )
    _fill_required(seeded, termin)
    seeded.commit()
    with pytest.raises(courses.NotReady, match="Makerspace Beispieldorf"):
        courses.finally_confirm(seeded, termin, "Team")

    # The admin merges it into the existing place.
    admin = _admin(monkeypatch)
    target = _place(seeded, "Makerspace")
    merged = admin.post(
        f"/stammdaten/orte/{place_id}/zusammenfuehren",
        data={"ziel": str(target.id), "reason": "derselbe Ort"},
        follow_redirects=False,
    )
    assert merged.status_code == 303 and f"id={target.id}" in merged.headers["location"]
    seeded.expire_all()
    assert seeded.get(Course, course.id).place_id == target.id
    assert seeded.get(Place, place_id).status == MERGED
    reasons = [h.reason for h in _history(seeded, "courses", course.id)]
    assert any("derselbe Ort" in (r or "") for r in reasons)
    detail = admin.get(f"/stammdaten/orte?id={place_id}").text
    assert "Zusammengeführt in „Makerspace“" in detail and "derselbe Ort" in detail

    # Final confirmation now passes, and the analysis counts the Termin at the target place.
    termin = seeded.get(Appointment, termin.id)
    courses.finally_confirm(seeded, termin, "Team")
    seeded.commit()
    rows = analytics.count_by_place(seeded, analytics.Filters(), TODAY, 8)
    assert [(r.place, r.count) for r in rows] == [("Makerspace", 1)]
    data = admin.get("/api/analyse/sessions-over-time").json()["meta"]
    assert (data["count"], data["totals"]["participants"], data["totals"]["locations"]) == (1, 9, 1)


def test_admin_decisions_through_the_pages(seeded, monkeypatch):
    team = _team(monkeypatch)
    team.post(
        "/stammdaten/formate",
        data={"name": "Löten", "category": "Engineering", "duration_hours": "2"},
        follow_redirects=False,
    )
    fmt = seeded.scalars(sa.select(Format).where(Format.name == "Löten")).one()
    edited = team.post(
        f"/stammdaten/formate/{fmt.id}",
        data={"name": "Löten", "category": "Engineering", "duration_hours": "2,5"},
        follow_redirects=False,
    )
    assert edited.status_code == 303  # the team edits a proposed entry
    seeded.add(Format(name="Lötkurs", category="Engineering", duration_hours=2))
    seeded.commit()
    admin = _admin(monkeypatch)
    page = admin.get(f"/stammdaten/formate?id={fmt.id}").text
    assert f'action="/stammdaten/formate/{fmt.id}/bestaetigen"' in page and 'name="ziel"' in page
    no_reason = admin.post(f"/stammdaten/formate/{fmt.id}/ablehnen", data={"reason": ""})
    assert no_reason.status_code == 422 and "Grund" in no_reason.text
    assert admin.post(f"/stammdaten/formate/{fmt.id}/bestaetigen", follow_redirects=False).status_code == 303
    seeded.expire_all()
    fmt = seeded.get(Format, fmt.id)
    assert fmt.status == CONFIRMED and str(fmt.duration_hours) == "2.50"
    assert [(h.field, h.changed_by) for h in _history(seeded, "formats", fmt.id)] == [
        ("status", "Team"),
        ("duration_hours", "Team"),
        ("status", auth.OFF_USER),
    ]


def test_admin_marks_a_format_continuous(seeded, monkeypatch):
    """WM-114: "fortlaufend" is edited on the Formate page like the other details (admin)."""
    fmt = Format(name="Repair Café", category="Engineering", duration_hours=3, status=CONFIRMED)
    seeded.add(fmt)
    seeded.commit()
    admin = _admin(monkeypatch)
    assert 'name="continuous"' in admin.get(f"/stammdaten/formate?id={fmt.id}").text
    values = {"name": "Repair Café", "category": "Engineering", "duration_hours": "3", "continuous": "1"}
    assert admin.post(f"/stammdaten/formate/{fmt.id}", data=values).status_code in (200, 303)
    seeded.expire_all()
    assert seeded.get(Format, fmt.id).continuous is True
    assert [h.field for h in _history(seeded, "formats", fmt.id)] == ["continuous"]
    assert "fortlaufend" in lists.describe(seeded.get(Format, fmt.id), {})
