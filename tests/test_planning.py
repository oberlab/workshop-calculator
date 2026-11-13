"""Planning pages (WM-110): course list, course page, new course, actions, availability. PostgreSQL,
demo data only; the workflow rules themselves are tested in tests/test_workflow.py."""

import datetime as dt
import re

import pytest
import sqlalchemy as sa

from metrics import hints
from metrics.db import (
    ABGESCHLOSSEN,
    BESTAETIGT,
    DURCHGEFUEHRT,
    GEPLANT,
    Appointment,
    HintDecision,
    HistoryEntry,
)
from metrics.planning import workflow
from metrics.web.common import today
from tests.test_lists import _admin, _team
from tests.test_workflow import World, form

DAY = today() + dt.timedelta(days=7)


@pytest.fixture
def world(db, instance):
    from metrics.ingest import store

    store.seed_reference(db, instance)
    w = World(db)
    db.commit()
    return w


def _basics(w: World, **extra) -> dict:
    values = dict(
        format_id=w.format.id,
        context_id=w.context.id,
        place_id=w.school.id,
        class_kind="klasse",
        class_level=3,
        date=DAY.isoformat(),
        start_time="09:00",
        end_time="11:00",
        lead=w.kim.id,
    )
    return values | extra


def _termin(db, course_id: int) -> Appointment:
    db.expire_all()
    return db.scalars(sa.select(Appointment).where(Appointment.course_id == course_id)).one()


def _create(client, w, **extra) -> int:
    response = client.post("/planung/neu", data=_basics(w, **extra), follow_redirects=False)
    assert response.status_code == 303, response.text
    return int(re.search(r"/planung/(\d+)", response.headers["location"]).group(1))


def test_a_course_from_geplant_to_abgeschlossen_in_the_pages(world, monkeypatch):
    client = _team(monkeypatch)
    db = world.db
    new = client.get(f"/planung/neu?date={DAY.isoformat()}")
    assert new.status_code == 200 and f'value="{DAY.isoformat()}"' in new.text
    assert 'id="picker-place_id"' in new.text and "Neu vorschlagen" in new.text
    # Geplant: a day without times is fine.
    response = client.post("/planung/neu", data={"date": DAY.isoformat()}, follow_redirects=False)
    assert response.status_code == 303
    day_only = int(re.search(r"/planung/(\d+)", response.headers["location"]).group(1))
    assert _termin(db, day_only).stage == GEPLANT

    yesterday = (today() - dt.timedelta(days=1)).isoformat()  # held, so it can be finally confirmed
    course_id = _create(client, world, date=yesterday)
    page = client.get(f"/planung/{course_id}").text
    assert "Löten" in page and 'value="confirm"' in page and "Was fehlt noch" in page
    t = _termin(db, course_id)
    # The stage bar's main button saves the form, then confirms.
    data = _basics(world, termin=t.id, dann="confirm", end_time="11:30", date=yesterday)
    assert client.post(f"/planung/{course_id}", data=data, follow_redirects=False).status_code == 303
    t = _termin(db, course_id)
    assert (t.stage, t.end_time) == (BESTAETIGT, dt.time(11, 30))
    data = _basics(
        world, termin=t.id, dann="done", participants_female=4, participants_male=3, date=yesterday
    )
    assert client.post(f"/planung/{course_id}", data=data, follow_redirects=False).status_code == 303
    t = _termin(db, course_id)
    assert (t.stage, t.participants_total) == (DURCHGEFUEHRT, 7)
    page = client.get(f"/planung/{course_id}").text
    assert 'value="final"' in page and "Abrechnung" in page
    data = _basics(
        world, termin=t.id, dann="final", participants_female=4, participants_male=3, date=yesterday
    )
    response = client.post(f"/planung/{course_id}", data=data, follow_redirects=False)
    assert response.status_code == 303, response.text
    assert _termin(db, course_id).stage == ABGESCHLOSSEN
    page = client.get(f"/planung/{course_id}").text
    assert "Gesperrt bis auf „Abgerechnet“" in page and f'action="/planung/{course_id}/aktion"' in page
    billed = client.post(
        f"/planung/{course_id}/aktion",
        data={"action": "billed", "termin": t.id, "billed": "1"},
        follow_redirects=False,
    )
    assert billed.status_code == 303 and _termin(db, course_id).billed is True
    history = db.scalars(sa.select(HistoryEntry.field).where(HistoryEntry.row_id == t.id)).all()
    assert "billed" in history and "stage" in history


def test_errors_keep_the_entered_values(world, monkeypatch):
    client = _team(monkeypatch)
    course_id = _create(client, world)
    t = _termin(world.db, course_id)
    data = _basics(world, termin=t.id, dann="confirm", place_id="", note="Bitte Laptops")
    response = client.post(f"/planung/{course_id}", data=data)
    assert response.status_code == 400
    assert "Zum Bestätigen fehlt: Ort" in response.text and "Bitte Laptops" in response.text
    assert _termin(world.db, course_id).stage == GEPLANT


def test_cancel_and_delete_in_the_pages(world, monkeypatch):
    client = _team(monkeypatch)
    db = world.db
    course_id = _create(client, world)
    t = _termin(db, course_id)
    refused = client.post(f"/planung/{course_id}/aktion", data={"action": "delete", "termin": t.id})
    assert refused.status_code == 400 and "Bitte das Löschen bestätigen" in refused.text
    deleted = client.post(
        f"/planung/{course_id}/aktion",
        data={"action": "delete", "termin": t.id, "really": "1"},
        follow_redirects=False,
    )
    assert deleted.status_code == 303 and client.get(f"/planung/{course_id}").status_code == 404

    course_id = _create(client, world)
    t = _termin(db, course_id)
    client.post(f"/planung/{course_id}", data=_basics(world, termin=t.id, dann="confirm"))
    cancelled = client.post(
        f"/planung/{course_id}/aktion",
        data={"action": "cancel", "termin": t.id, "reason": "Schule zu"},
        follow_redirects=False,
    )
    assert cancelled.status_code == 303
    assert "Abgesagt: Schule zu" in client.get(f"/planung/{course_id}").text


def test_team_members_accept_a_warning_on_the_course_page(world, monkeypatch):
    client = _team(monkeypatch)
    db = world.db
    first = _create(client, world)
    second = _create(client, world, lead=world.lea.id, start_time="13:00", end_time="14:00")
    t = _termin(db, first)
    page = client.get(f"/planung/{first}").text
    assert "Anderer Kurs am selben Tag" in page and f'action="/planung/{first}/akzeptieren"' in page
    hint = db.scalars(
        sa.select(hints.StoredHint).where(
            hints.StoredHint.appointment_id == t.id, hints.StoredHint.rule == "selber-tag"
        )
    ).one()
    without = client.post(
        f"/planung/{first}/akzeptieren",
        data={"termin": t.id, "regel": "selber-tag", "schluessel": hint.key, "grund": ""},
    )
    assert without.status_code == 400 and "Bitte einen Grund angeben" in without.text
    response = client.post(
        f"/planung/{first}/akzeptieren",
        data={"termin": t.id, "regel": "selber-tag", "schluessel": hint.key, "grund": "zwei Gruppen"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    decision = db.scalars(sa.select(HintDecision)).one()
    assert (decision.decided_by, decision.finding_key) == ("Team", f"termin:{_termin(db, second).id}")
    assert "Akzeptiert von Team" in client.get(f"/planung/{first}").text


def test_course_list_groups_by_week_and_filters(world, monkeypatch):
    client = _team(monkeypatch)
    db = world.db
    a = _create(client, world)
    b = _create(
        client,
        world,
        date=(DAY + dt.timedelta(days=7)).isoformat(),
        lead=world.lea.id,
        place_id=world.makerspace.id,
        class_kind="alter",
        child_age=10,
    )
    html = client.get("/planung").text
    assert html.count('class="week"') == 2 and f"KW {DAY.isocalendar().week}" in html
    assert 'class="course-cards cards"' in html and 'class="card stage-geplant"' in html  # phones
    assert f"/planung/{a}?termin=" in html and f"/planung/{b}?termin=" in html
    only_lea = client.get(f"/planung?person={world.lea.id}").text
    assert f"/planung/{b}?termin=" in only_lea and f"/planung/{a}?termin=" not in only_lea
    at_school = client.get(f"/planung?ort={world.school.id}").text
    assert f"/planung/{a}?termin=" in at_school and f"/planung/{b}?termin=" not in at_school
    assert "Keine Termine" in client.get("/planung?phase=Abgeschlossen").text
    assert "Keine Termine" in client.get("/planung?kategorie=Gibt-es-nicht").text
    assert "(Anmeldung nicht verknüpft)" in html  # the password login has no person
    assert client.get("/planung?phase=Unsinn").status_code == 400
    # Meine Kurse: the person linked to the login (OIDC subject), nobody without a link.
    world.kim.login_subject = "off"
    db.commit()
    mine = _admin(monkeypatch).get("/planung?person=ich").text
    assert f"/planung/{a}?termin=" in mine and f"/planung/{b}?termin=" not in mine
    assert "Keine Termine" in client.get("/planung?hinweise=kritisch").text
    assert db.scalar(sa.select(sa.func.count()).select_from(Appointment)) == 2


def test_availability_endpoint(world, monkeypatch):
    client = _team(monkeypatch)
    course_id = _create(client, world, support=[str(world.lea.id)])
    t = _termin(world.db, course_id)
    busy = client.get(f"/api/planung/belegt?date={DAY.isoformat()}&start=10:00&end=12:00").json()
    assert busy == {str(world.kim.id): "Löten (09:00–11:00)", str(world.lea.id): "Löten (09:00–11:00)"}
    assert (
        client.get(f"/api/planung/belegt?date={DAY.isoformat()}&start=10:00&end=12:00&exclude={t.id}").json()
        == {}
    )
    assert client.get("/api/planung/belegt?date=kein-datum").json() == {}


def test_termin_link_and_sidebar_counts(world, monkeypatch):
    client = _team(monkeypatch)
    db = world.db
    course_id = _create(client, world, date=(today() - dt.timedelta(days=2)).isoformat())
    t = _termin(db, course_id)
    response = client.get(f"/planung/termin/{t.id}", follow_redirects=False)
    assert response.headers["location"] == f"/planung/{course_id}?termin={t.id}"
    assert workflow.sidebar_counts(db) == {"courses_high": 1, "courses_medium": 0}  # Geplant, Datum vorbei
    assert 'title="1 Kurse mit kritischem Hinweis"' in client.get("/planung").text
    assert client.get("/planung/termin/999999").status_code == 404


def test_participants_form_in_geplant(world, monkeypatch):
    client = _team(monkeypatch)
    course_id = _create(client, world, participants_female=12, participants_male=10, participants_diverse=1)
    t = _termin(world.db, course_id)
    assert (t.stage, t.participants_total) == (GEPLANT, 23)
    page = client.get(f"/planung/{course_id}").text
    assert 'name="participants_unspecified"' in page and "auch vorab eintragbar" in page


def test_form_helper():
    assert form(a=1, b=["x", "y"], c=None) == {"a": ["1"], "b": ["x", "y"]}
