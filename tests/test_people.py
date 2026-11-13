"""The Personen page (WM-105): admins only, every detail kept by hand, on PostgreSQL with demo data."""

import re

import pytest
import sqlalchemy as sa
from fastapi import HTTPException

from metrics import auth, lists
from metrics.db import CONFIRMED, HistoryEntry, Person
from metrics.ingest import store
from metrics.web import lists as list_pages
from tests.test_lists import _admin, _team


@pytest.fixture
def seeded(db, instance):
    store.seed_reference(db, instance)
    db.commit()
    return db


def _people(db) -> dict[str, Person]:
    db.expire_all()
    return {p.name: p for p in db.scalars(sa.select(Person))}


def _history(db, person_id: int, field: str) -> list[HistoryEntry]:
    return list(
        db.scalars(
            sa.select(HistoryEntry)
            .where(HistoryEntry.table_name == "people", HistoryEntry.row_id == person_id)
            .where(HistoryEntry.field == field)
            .order_by(HistoryEntry.id)
        )
    )


def _names(html: str) -> list[str]:
    return re.findall(r'href="/stammdaten/personen\?id=\d+[^"]*#eintrag"[^>]*>([^<]+)</a>', html)


def _form(person: Person, **changes) -> dict[str, str]:
    return {**lists.form_values(lists.PEOPLE, person), **changes}


@pytest.fixture
def team_people(seeded):
    seeded.add_all(
        [
            Person(name="Kim", email="kim@example.org", slack_id="U0KIM00001", login_subject="kc-kim"),
            Person(name="Lea", email="lea@example.org"),
            Person(name="Hanna"),  # a helper without login or Slack
            Person(name="Max", active=False),  # a legacy first name
        ]
    )
    seeded.commit()
    return _people(seeded)


def test_team_members_cannot_open_the_personen_page(team_people, seeded, monkeypatch):
    team = _team(monkeypatch)
    hanna = team_people["Hanna"]
    assert team.get("/stammdaten/personen").status_code == 403
    assert team.get(f"/stammdaten/personen?id={hanna.id}").status_code == 403
    assert team.post("/stammdaten/personen", data={"name": "Nora"}).status_code == 403
    edited = team.post(f"/stammdaten/personen/{hanna.id}", data=_form(hanna, email="h@example.org"))
    assert edited.status_code == 403
    assert _people(seeded)["Hanna"].email is None
    # Neither the sidebar nor the list tabs offer the page to the team.
    assert 'href="/stammdaten/personen"' not in team.get("/stammdaten/orte").text


def test_the_team_still_picks_and_proposes_people(team_people, seeded, monkeypatch):
    team = _team(monkeypatch)
    kim = team_people["Kim"]
    picked = team.get(f"/stammdaten/personen/auswahl?feld=lead_id&lead_id={kim.id}")
    assert picked.status_code == 200 and f'<option value="{kim.id}" selected>' in picked.text
    proposed = team.post(
        "/stammdaten/personen/auswahl?feld=lead_id",
        data={"neu-lead_id-name": "Nora", "neu-lead_id-email": "nora@example.org"},
    )
    assert proposed.status_code == 200 and "vorgeschlagen" in proposed.text
    assert _people(seeded)["Nora"].email == "nora@example.org"


def test_the_generic_list_routes_never_serve_people():
    with pytest.raises(HTTPException) as error:
        list_pages._kind("personen", people=False)
    assert error.value.status_code == 404
    assert list_pages._kind("personen") is lists.PEOPLE  # picker and admin decisions


def test_filters_without_slack_id_and_login(team_people, monkeypatch):
    admin = _admin(monkeypatch)
    html = admin.get("/stammdaten/personen").text
    assert "Personen" in html and 'href="/stammdaten/personen"' in html  # sidebar entry for admins
    # The example instance seeds one more person, with a Slack ID and without login.
    assert "ohne Slack-ID (3)" in html and "ohne Anmeldung (4)" in html
    without_slack = admin.get("/stammdaten/personen?ohne_slack=1").text
    assert _names(without_slack) == ["Hanna", "Lea", "Max"]
    assert "ohne_slack=1" in re.search(r'href="(/stammdaten/personen\?id=[^"]+)"', without_slack).group(1)
    both = admin.get("/stammdaten/personen?ohne_slack=1&ohne_konto=1&status=alle").text
    assert _names(both) == ["Hanna", "Lea", "Max"]
    # A note (not a block) for active people only; a legacy first name is not asked for a Slack ID.
    assert without_slack.count("</span>ohne Slack-ID</span>") == 2


def test_admin_sets_email_and_slack_id_by_hand(team_people, seeded, monkeypatch):
    admin = _admin(monkeypatch)
    hanna = team_people["Hanna"]
    url = f"/stammdaten/personen/{hanna.id}"
    detail = admin.get(f"/stammdaten/personen?id={hanna.id}").text
    assert 'name="email"' in detail and 'name="slack_id"' in detail

    cases = [
        ({"email": "hanna"}, "Keine gültige E-Mail-Adresse."),
        ({"email": "KIM@example.org"}, "Die E-Mail-Adresse gehört schon zu „Kim“."),
        ({"slack_id": "hanna"}, "Keine gültige Slack-ID"),
        ({"slack_id": "C0CHANNEL1"}, "Keine gültige Slack-ID"),
        ({"slack_id": "u0kim00001"}, "Die Slack-ID gehört schon zu „Kim“."),
    ]
    for changes, message in cases:
        response = admin.post(url, data=_form(hanna, **changes))
        assert response.status_code == 422 and message in response.text, changes
    assert (_people(seeded)["Hanna"].email, _people(seeded)["Hanna"].slack_id) == (None, None)

    saved = admin.post(
        url, data=_form(hanna, email=" hanna@example.org ", slack_id="u0han00001"), follow_redirects=False
    )
    assert saved.status_code == 303
    hanna = _people(seeded)["Hanna"]
    assert (hanna.email, hanna.slack_id) == ("hanna@example.org", "U0HAN00001")
    for field, value in (("email", "hanna@example.org"), ("slack_id", "U0HAN00001")):
        (row,) = _history(seeded, hanna.id, field)
        assert (row.old_value, row.new_value, row.changed_by) == (None, value, auth.OFF_USER)

    # Changing them again and removing the e-mail are recorded the same way.
    admin.post(url, data=_form(hanna, email="", slack_id="W0HAN00001"), follow_redirects=False)
    hanna = _people(seeded)["Hanna"]
    assert (hanna.email, hanna.slack_id) == (None, "W0HAN00001")
    assert [r.new_value for r in _history(seeded, hanna.id, "email")] == ["hanna@example.org", None]


def test_a_person_can_be_confirmed_without_slack_id(seeded, monkeypatch):
    nora = lists.propose(seeded, lists.PEOPLE, {"name": "Nora"}, "Lea").entry
    seeded.commit()
    assert lists.missing_details(lists.PEOPLE, nora) == []
    admin = _admin(monkeypatch)
    confirmed = admin.post(f"/stammdaten/personen/{nora.id}/bestaetigen", follow_redirects=False)
    assert confirmed.status_code == 303 and "/stammdaten/personen?" in confirmed.headers["location"]
    assert _people(seeded)["Nora"].status == CONFIRMED


def test_proposals_from_the_personen_page(seeded, monkeypatch):
    admin = _admin(monkeypatch)
    bad = admin.post("/stammdaten/personen", data={"name": "Nora", "email": "nora@"})
    assert bad.status_code == 422 and "Keine gültige E-Mail-Adresse." in bad.text
    created = admin.post(
        "/stammdaten/personen", data={"name": "Nora", "email": "nora@example.org"}, follow_redirects=False
    )
    assert created.status_code == 303
    assert _people(seeded)["Nora"].email == "nora@example.org"
