"""The phone form after the course (WM-133). PostgreSQL, demo data only.

Done when: a lead enters the participants of yesterday's course on a phone in under a minute. The
browser check is in the ticket report; here: it shows only what is missing, saves through the same
rules as the course page (Verlauf, hints) and the task in Mein Bereich leads to it and disappears.
"""

import datetime as dt
import re
from pathlib import Path

import pytest
import sqlalchemy as sa

from metrics.db import ABGESCHLOSSEN, Appointment, HistoryEntry, StoredHint
from metrics.planning import after, mine, workflow
from metrics.web.common import today
from tests.test_mine import _as, _held, _termin, world  # noqa: F401 (fixture)
from tests.test_web import _client

STATIC = Path(after.__file__).parent.parent / "web" / "static"
TODAY = today()
YESTERDAY = TODAY - dt.timedelta(days=1)


def _url(t) -> str:
    return f"/mein-bereich/nach-dem-kurs/{t.id}"


@pytest.fixture
def kim(world, monkeypatch):  # noqa: F811
    return _as(monkeypatch, "Kim", "kc-kim")


def _yesterday(w, **extra):
    t = _held(w, _termin(w, YESTERDAY, w.kim, [w.lea], **extra))
    w.db.commit()
    return t


def _history(db, t) -> dict[str, HistoryEntry]:
    rows = db.scalars(
        sa.select(HistoryEntry).where(HistoryEntry.table_name == "appointments", HistoryEntry.row_id == t.id)
    )
    return {(h.field or h.reason): h for h in rows}


def _rules(db, t) -> set[str]:
    return set(db.scalars(sa.select(StoredHint.rule).where(StoredHint.appointment_id == t.id)))


COUNTS = {
    "participants_female": "11",
    "participants_male": "11",
    "participants_diverse": "1",
    "participants_unspecified": "0",
}


def test_mein_bereich_links_to_the_form_and_the_task_disappears(world, kim):  # noqa: F811
    w = world
    t = _yesterday(w)
    home = kim.get("/mein-bereich").text
    assert f'href="{_url(t)}"' in home and "Teilnehmende eintragen" in home

    form = kim.get(_url(t))
    assert form.status_code == 200
    html = form.text
    assert all(f'name="{name}"' in html for name, _ in workflow.PARTICIPANT_FIELDS)
    assert "Herkunft" not in html  # Projekt A does not ask for municipalities
    assert 'name="csrf_token"' in html and "after.js" in html
    assert 'data-step="1" hidden' in html  # without JavaScript plain number fields
    assert re.search(r'aria-current="page"[^>]*>\s*<span>Mein Bereich', html)  # the sidebar entry

    response = kim.post(_url(t), data=COUNTS | {"note": "Ein Kind kam später."}, follow_redirects=False)
    assert (response.status_code, response.headers["location"].split("?")[0]) == (303, "/mein-bereich")

    w.db.expire_all()
    t = w.db.get(Appointment, t.id)
    assert (t.participants_female, t.participants_male, t.participants_diverse) == (11, 11, 1)
    assert t.participants_total == 23
    history = _history(w.db, t)
    assert (
        history["participants_female"].new_value == "11"
        and history["participants_female"].changed_by == "Kim"
    )
    assert history[after.NOTE_REASON].new_value == "Ein Kind kam später."
    assert "pflicht-abschluss" not in _rules(w.db, t)  # hints refreshed like on the course page
    assert mine.tasks(w.db, w.kim, TODAY)[0].text == "Alles da: final bestätigen"
    assert "Alles eingetragen" in kim.get(_url(t)).text


def test_only_what_is_missing_with_herkunft(world, kim):  # noqa: F811
    w = world
    t = _yesterday(w, context_id=w.youth.id)
    other = _yesterday(w, context_id=w.youth.id)
    workflow.save(
        w.db,
        other,
        {"participants_female": ["3"], "participants_male": ["2"], "origin_name": ["Seedorf", "Musterstadt"],
         "origin_count": ["4", "1"]},
        "Test",
        only={workflow.HELD},
    )  # fmt: skip
    w.db.commit()

    html = kim.get(_url(t)).text
    assert 'name="participants_female"' in html and "Herkunft" in html
    # Nothing entered yet: a neutral hint instead of "0 von 0, es fehlen 0" in warning colour.
    assert 'class="after-sum is-neutral"' in html and "0 von 0" not in html
    assert (
        "is-neutral" in (STATIC / "after.js").read_text()
        and ".after-sum.is-neutral" in (STATIC / "style.css").read_text()
    )
    # Municipalities used before in the same context are offered as rows.
    assert 'name="origin_name" value="Seedorf"' in html and 'name="origin_name" value="Musterstadt"' in html

    # Counts first: the form then shows Herkunft only, with the total fixed.
    response = kim.post(_url(t), data=COUNTS, follow_redirects=False)
    assert response.status_code == 303 and response.headers["location"].startswith(_url(t))
    html = kim.get(_url(t)).text
    assert 'name="participants_female"' not in html and 'data-fixed-total="23"' in html
    assert "is-neutral" not in html and 'von <b class="mono">23</b>' in html

    # Herkunft alone keeps the counts; a sum that does not match keeps the form open.
    data = {"origin_name": ["Seedorf", "Musterstadt", "", ""], "origin_count": ["17", "", "", ""]}
    kim.post(_url(t), data=data, follow_redirects=False)
    w.db.expire_all()
    t = w.db.get(Appointment, t.id)
    assert t.participants_total == 23 and [(o.municipality, o.participants) for o in t.origins] == [
        ("Seedorf", 17)
    ]
    assert "herkunft-summe" in _rules(w.db, t) and after.gaps(t).origins
    assert 'value="17"' in kim.get(_url(t)).text

    data = {"origin_name": ["Seedorf", "Musterstadt", "Neudorf", ""], "origin_count": ["17", "", "6", ""]}
    response = kim.post(_url(t), data=data, follow_redirects=False)
    assert response.headers["location"].startswith("/mein-bereich?")
    w.db.expire_all()
    t = w.db.get(Appointment, t.id)
    assert {o.municipality: o.participants for o in t.origins} == {"Seedorf": 17, "Neudorf": 6}
    assert not after.gaps(t).any and "herkunft-summe" not in _rules(w.db, t)


def test_refused_input_is_kept_and_nothing_is_saved(world, kim):  # noqa: F811
    w = world
    t = _yesterday(w)
    zeros = dict.fromkeys(COUNTS, "0")
    response = kim.post(_url(t), data=zeros | {"note": "Regen"})
    assert response.status_code == 400 and "Alle vier Zahlen sind 0" in response.text
    assert ">Regen</textarea>" in response.text
    bad = kim.post(_url(t), data=COUNTS | {"participants_male": "-2"})
    assert bad.status_code == 400 and "darf nicht negativ sein" in bad.text
    assert 'value="11"' in bad.text  # what was entered stays in the fields
    w.db.expire_all()
    assert w.db.get(Appointment, t.id).participants_female is None
    assert after.NOTE_REASON not in _history(w.db, t)


def test_a_closed_termin_has_nothing_to_enter(world, kim):  # noqa: F811
    w = world
    t = _yesterday(w)
    t.stage = ABGESCHLOSSEN
    w.db.commit()
    html = kim.get(_url(t)).text
    assert "Hier ist nichts mehr einzutragen" in html and '<form method="post" action="/mein' not in html
    assert kim.post(_url(t), data=COUNTS).status_code == 400
    assert kim.get("/mein-bereich/nach-dem-kurs/999999").status_code == 404


def test_the_form_needs_a_login(monkeypatch):
    client = _client(monkeypatch)
    response = client.get("/mein-bereich/nach-dem-kurs/1", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/login")
    assert client.post("/mein-bereich/nach-dem-kurs/1", data=COUNTS).status_code == 401
