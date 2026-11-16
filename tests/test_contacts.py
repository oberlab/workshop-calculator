"""Contact persons at places (WM-141). PostgreSQL, demo data and made-up contacts only.

The Done-when check: a contact is added from the course page and the course's lead sees it. Contacts
are personal data of people outside the team: they appear on the course page and at the place only,
never in an address, the Verlauf text, the calendar, Slack, a chart or another page, and a deletion on
request leaves no copy of the values in any table.
"""

import datetime as dt
import logging
import re

import pytest
import sqlalchemy as sa

from metrics import contacts, lists, messages
from metrics.db import Base, Contact, Course, HistoryEntry, Place
from metrics.planning import caldav, workflow
from metrics.web.common import today
from tests.test_lists import _team
from tests.test_mine import _as
from tests.test_web import OIDC, _client
from tests.test_workflow import World

DAY = today() + dt.timedelta(days=7)
# A made-up contact; each value is unusual enough to be found anywhere it might leak.
BERGER = {
    "name": "Frau Zettelberger",
    "role": "Klassenleitung",
    "email": "zettel@schule.example",
    "phone": "0123 987654",
}
TRACES = ("Zettelberger", "zettel@schule.example", "987654")


@pytest.fixture
def world(db, instance):
    from metrics.ingest import store

    store.seed_reference(db, instance)
    w = World(db)
    w.kim.login_subject, w.sam.login_subject = "kc-kim", "kc-sam"
    db.commit()
    return w


def _course(w: World, **extra) -> Course:
    values = {"date": DAY.isoformat(), "start_time": "09:00", "end_time": "11:00", "lead": w.kim.id}
    t = workflow.create(w.db, w.basics(**(values | extra)), "Test")
    w.db.commit()
    return t.course


def _rows_with(db, text: str) -> dict[str, int]:
    """Tables that hold `text` anywhere in a row (what a dump of the database would carry)."""
    found = {}
    for table in Base.metadata.sorted_tables:
        n = db.scalar(
            sa.text(f'SELECT count(*) FROM "{table.name}" AS r WHERE r::text ILIKE :p'), {"p": f"%{text}%"}
        )
        if n:
            found[table.name] = n
    return found


def _history(db, table: str, row_id: int) -> list[HistoryEntry]:
    stmt = sa.select(HistoryEntry).where(HistoryEntry.table_name == table, HistoryEntry.row_id == row_id)
    return list(db.scalars(stmt.order_by(HistoryEntry.id)))


def test_a_contact_added_on_the_course_page_is_seen_by_the_lead(world, monkeypatch, caplog):
    w = world
    course = _course(w)
    caplog.set_level(logging.DEBUG)
    # Sam plans the course and adds the class teacher from the course page.
    sam = _as(monkeypatch, "Sam", "kc-sam", **OIDC)
    page = sam.get(f"/planung/{course.id}").text
    assert 'id="kontakt"' in page and "Noch keine Kontaktperson gewählt." in page
    response = sam.post(
        f"/planung/{course.id}/kontakt",
        data={"action": "neu", "termin": "", **BERGER},
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/planung/{course.id}?") and not any(x in location for x in TRACES)
    # Kim, the lead, opens the course and sees name, role, phone and e-mail.
    kim = _as(monkeypatch, "Kim", "kc-kim", first=False, **OIDC)
    html = kim.get(f"/planung/{course.id}").text
    assert "Frau Zettelberger" in html and "Klassenleitung" in html
    assert 'href="tel:0123987654"' in html and 'href="mailto:zettel@schule.example"' in html
    db = w.db
    db.expire_all()
    contact = db.scalars(sa.select(Contact)).one()
    assert contact.place_id == w.school.id and db.get(Course, course.id).contact_id == contact.id
    # The Verlauf says what happened, never the values.
    reasons = [h.reason for h in _history(db, "courses", course.id) if h.field is None]
    assert reasons[-1] == contacts.CHOSEN
    assert _rows_with(db, "Zettelberger") == {"contacts": 1}
    assert not any(x in caplog.text for x in TRACES)


def test_contacts_appear_nowhere_else(world, monkeypatch):
    w = world
    course = _course(w)
    contact = contacts.add(w.db, w.school, BERGER, "Test")
    contacts.choose(w.db, course, contact, "Test")
    w.db.commit()
    client = _team(monkeypatch)
    pages = [
        "/planung",
        "/planung?phase=alle",
        "/kalender",
        f"/kalender/termin/{course.appointments[0].id}",
        "/mein-bereich",
        "/team",
        "/analyse",
        "/api/analyse/sessions-over-time",
        "/api/analyse/schools",
        "/api/analyse/municipalities",
        "/stammdaten/orte",
        "/",
        "/api/sessions-over-time",
    ]
    for path in pages:
        # The calendar's side panel is an htmx partial; opened directly it leads to the course page.
        response = client.get(path, headers={"HX-Request": "true"})
        assert response.status_code == 200, path
        assert not any(x in response.text for x in TRACES), path
    # The place search does not look into contacts.
    assert "Grundschule Musterstadt" not in client.get("/stammdaten/orte?q=Klassenleitung").text
    # The place page and the course page do show it.
    assert "Frau Zettelberger" in client.get(f"/stammdaten/orte?id={w.school.id}").text
    assert "Frau Zettelberger" in client.get(f"/planung/{course.id}").text
    # Neither the calendar event nor the Slack texts carry it.
    t = course.appointments[0]
    event = caldav.expected_event(t, "https://zahlen.example.org", None)
    assert not any(x in caldav.to_ics(event) for x in TRACES)
    assert not any(x in messages.reminder_text(t) + messages.after_course_text(t, 1) for x in TRACES)


def test_contacts_need_a_login(world, monkeypatch):
    w = world
    course = _course(w)
    client = _client(monkeypatch)  # a browser without login
    for url in (
        f"/planung/{course.id}/kontakt",
        f"/stammdaten/orte/{w.school.id}/kontakte",
        f"/stammdaten/orte/{w.school.id}/kontakte/1",
        f"/stammdaten/orte/{w.school.id}/kontakte/1/loeschen",
    ):
        assert client.post(url, data={"action": "neu", **BERGER}).status_code == 401
    assert w.db.scalar(sa.select(sa.func.count()).select_from(Contact)) == 0


def test_choose_only_contacts_of_the_course_place(world):
    w = world
    course = _course(w)
    other = contacts.add(w.db, w.makerspace, {"name": "Herr Werkbank"}, "Test")
    with pytest.raises(contacts.ContactError):
        contacts.choose(w.db, course, other, "Test")
    mine = contacts.add(w.db, w.school, BERGER, "Test")
    assert contacts.choose(w.db, course, mine, "Test")
    assert not contacts.choose(w.db, course, mine, "Test")  # nothing changed, no Verlauf row
    assert [c.name for c in contacts.for_place(w.db, w.school)] == ["Frau Zettelberger"]
    # Another place on the course drops the contact of the old one, with a Verlauf row.
    t = course.appointments[0]
    workflow.save(w.db, t, w.basics(place_id=w.makerspace.id, class_kind="alter", child_age=10), "Kim")
    assert course.contact_id is None
    assert _history(w.db, "courses", course.id)[-1].reason == contacts.OTHER_PLACE


def test_parse_checks_the_values():
    values, errors = contacts.parse({"name": "  ", "email": "kein-at", "phone": "abc"})
    assert set(errors) == {"name", "email", "phone"}
    values, errors = contacts.parse(BERGER | {"role": ""})
    assert not errors and values["role"] is None and values["phone"] == "0123 987654"


def test_an_edit_records_the_field_but_not_the_values(world):
    w = world
    contact = contacts.add(w.db, w.school, BERGER, "Test")
    changed = contacts.update(w.db, contact, BERGER | {"phone": "0999 111222"}, "Kim")
    assert changed == ["phone"]
    rows = _history(w.db, "contacts", contact.id)
    assert [(h.field, h.old_value, h.new_value, h.reason) for h in rows] == [
        (None, None, None, contacts.ADDED),
        ("phone", None, None, contacts.CHANGED),
    ]
    w.db.flush()
    assert _rows_with(w.db, "987654") == {}  # the old number is gone, the Verlauf never had it


def test_a_deletion_on_request_leaves_only_kontakt_geloescht(world):
    w = world
    course = _course(w)
    second = _course(w)
    contact = contacts.add(w.db, w.school, BERGER, "Test")
    contacts.choose(w.db, course, contact, "Test")
    contacts.choose(w.db, second, contact, "Test")
    contacts.update(w.db, contact, BERGER | {"role": "Schulleitung"}, "Test")
    w.db.commit()
    with pytest.raises(contacts.ContactError):
        contacts.delete(w.db, contact, "Kim", really=False)
    contact_id = contact.id
    assert contacts.delete(w.db, contact, "Kim", really=True) == 2
    w.db.commit()
    w.db.expire_all()
    assert w.db.get(Contact, contact_id) is None
    assert w.db.get(Course, course.id).contact_id is None and w.db.get(Course, second.id).contact_id is None
    for table, row_id in (("courses", course.id), ("courses", second.id), ("places", w.school.id)):
        last = _history(w.db, table, row_id)[-1]
        assert (last.reason, last.changed_by, last.old_value, last.new_value) == (
            contacts.DELETED,
            "Kim",
            None,
            None,
        )
        assert last.changed_at is not None
    # No table keeps any of the values, so the next dump has none of them.
    for trace in (*TRACES, "Schulleitung", "Klassenleitung"):
        assert _rows_with(w.db, trace) == {}, trace


def test_a_merged_place_hands_its_contacts_on(world):
    w = world
    copy = Place(name="GS Musterstadt", is_school=True, school_type="GS", address="", status="bestätigt")
    w.db.add(copy)
    w.db.flush()
    course = _course(w, place_id=copy.id)
    contact = contacts.add(w.db, copy, BERGER, "Test")
    contacts.choose(w.db, course, contact, "Test")
    lists.merge(w.db, lists.PLACES, copy, w.school, "Admin", None)
    assert contact.place_id == w.school.id and course.place_id == w.school.id
    assert course.contact_id == contact.id
    assert contacts.for_place(w.db, w.school) == [contact]


def test_the_place_page_adds_changes_and_deletes_contacts(world, monkeypatch):
    w = world
    course = _course(w)
    client = _team(monkeypatch)
    base = f"/stammdaten/orte/{w.school.id}/kontakte"
    refused = client.post(base, data={"name": "", "email": "falsch"})
    assert refused.status_code == 422 and "Pflichtfeld" in refused.text and 'value="falsch"' in refused.text
    added = client.post(base, data=BERGER, follow_redirects=False)
    assert added.status_code == 303 and not any(x in added.headers["location"] for x in TRACES)
    contact = w.db.scalars(sa.select(Contact)).one()
    contact_id = contact.id
    page = client.get(f"/stammdaten/orte?id={w.school.id}").text
    assert 'id="kontakte"' in page and "Frau Zettelberger" in page
    assert f'action="{base}/{contact.id}/loeschen"' in page
    # A contact of another place cannot be changed through this place.
    assert (
        client.post(f"/stammdaten/orte/{w.makerspace.id}/kontakte/{contact.id}", data=BERGER).status_code
        == 404
    )
    edited = client.post(
        f"{base}/{contact.id}", data=BERGER | {"role": "Sekretariat"}, follow_redirects=False
    )
    assert edited.status_code == 303
    w.db.expire_all()
    assert w.db.get(Contact, contact.id).role == "Sekretariat"
    # Chosen on the course page, then deleted on request at the place.
    chosen = client.post(
        f"/planung/{course.id}/kontakt",
        data={"action": "waehlen", "kontakt": contact.id},
        follow_redirects=False,
    )
    assert chosen.status_code == 303
    unconfirmed = client.post(f"{base}/{contact.id}/loeschen", data={})
    assert unconfirmed.status_code == 422 and w.db.get(Contact, contact.id) is not None
    deleted = client.post(f"{base}/{contact.id}/loeschen", data={"really": "1"}, follow_redirects=False)
    assert deleted.status_code == 303
    w.db.expire_all()
    assert w.db.get(Contact, contact_id) is None
    html = client.get(f"/planung/{course.id}").text
    assert "Kontakt gelöscht" in html and "Frau Zettelberger" not in html
    assert "Kontakt gelöscht" in client.get(f"/stammdaten/orte?id={w.school.id}").text


def test_the_course_page_without_place_and_with_errors(world, monkeypatch):
    w = world
    client = _team(monkeypatch)
    response = client.post("/planung/neu", data={"date": DAY.isoformat()}, follow_redirects=False)
    course_id = int(re.search(r"/planung/(\d+)", response.headers["location"]).group(1))
    page = client.get(f"/planung/{course_id}").text
    assert "bitte zuerst oben einen Ort wählen" in page
    refused = client.post(f"/planung/{course_id}/kontakt", data={"action": "neu", **BERGER})
    assert refused.status_code == 400 and "Bitte zuerst einen Ort wählen." in refused.text
    course = _course(w)
    bad = client.post(f"/planung/{course.id}/kontakt", data={"action": "neu", "name": "X", "email": "falsch"})
    assert (
        bad.status_code == 400
        and "Keine gültige E-Mail-Adresse." in bad.text
        and 'value="falsch"' in bad.text
    )
    assert client.post(f"/planung/{course.id}/kontakt", data={"action": "loeschen"}).status_code == 400
    removed = client.post(
        f"/planung/{course.id}/kontakt", data={"action": "entfernen"}, follow_redirects=False
    )
    assert removed.status_code == 303  # nothing chosen: nothing to remove, no harm
