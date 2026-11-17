"""WM-135: course values stay editable after a Termin is Abgeschlossen; a change applies to the open
Termine only, and the Abgeschlossen (and Abgesagt) ones keep the values they were counted with."""

import datetime as dt
from urllib.parse import urlencode

import pytest
import sqlalchemy as sa

from metrics import courses, lists, metrics
from metrics.db import (
    ABGESAGT,
    ABGESCHLOSSEN,
    CONFIRMED,
    Appointment,
    Course,
    HistoryEntry,
    Place,
)
from metrics.planning import corrections, series, workflow
from metrics.web import public
from metrics.web.common import today
from tests.test_workflow import World, form

ALL = metrics.Filters()
CHARTS = [
    "/api/sessions-over-time",
    "/api/analyse/sessions-over-time",
    "/api/analyse/key-figures",
    "/api/analyse/schools",
    "/api/analyse/municipalities",
    "/api/analyse/origins",
    "/api/analyse/cumulative",
    "/api/analyse/places",
    "/api/analyse/by/kontext",
    "/api/analyse/by/format",
    "/api/analyse/by/schule",
    "/api/analyse/by/alter",
    "/tabelle.csv",
]


@pytest.fixture
def world(db, instance):
    from metrics.ingest import store

    store.seed_reference(db, instance)
    db.flush()
    return World(db)


def _charts(client, world) -> dict[str, str]:
    """Every chart, key figure and the table, for the whole time and for each filter that applies."""
    public.clear_cache()  # the front page caches its data
    filters = [
        {},
        {"context": world.context.name},
        {"category": world.format.category},
        {"target_group": "Schulklasse"},
        {"municipality": world.school.municipality or ""},
        {"spalte": ["datum", "ort", "kontext", "zielgruppe", "klasse", "tn_gezaehlt"]},
        {"gruppe": "ort", "ansicht": "summen"},
    ]
    found = {}
    for url in CHARTS:
        for params in filters:
            query = urlencode({"period": "all", **params}, doseq=True)
            response = client.get(f"{url}?{query}")
            assert response.status_code == 200, url
            found[f"{url}?{query}"] = response.text
    return found


def _series_with_first_closed(world, n: int = 3) -> list[Appointment]:
    """A weekly series of `n` Termine at the school whose first Termin is Abgeschlossen."""
    db = world.db
    first = today() - dt.timedelta(days=14)
    t = series.create(
        db, world.basics(date=first.isoformat(), reihe_rhythmus="woechentlich", reihe_anzahl=n), "Kim"
    )
    workflow.confirm(db, t, None, "Kim")
    workflow.mark_done(db, t, "Kim")
    workflow.save(
        db, t, world.basics(date=first.isoformat(), participants_female=6, participants_male=5), "Kim"
    )
    workflow.finalize(db, t, "Kim", checked=True, today=today())
    assert t.stage == ABGESCHLOSSEN
    db.commit()
    return courses.in_order(t.course.appointments)


def _move_to(world, t: Appointment, **changes) -> list[str]:
    """Save the course form of an open Termin with its own date and some course values changed."""
    return workflow.save(world.db, t, world.basics(date=t.date.isoformat(), **changes), "Kim")


def _history(db, table: str, row_id: int) -> list[HistoryEntry]:
    stmt = sa.select(HistoryEntry).where(HistoryEntry.table_name == table, HistoryEntry.row_id == row_id)
    return list(db.scalars(stmt.order_by(HistoryEntry.id)))


def test_done_when_changing_the_place_moves_the_open_termine_only(world, monkeypatch):
    """Done when: in a series with Termin 1 Abgeschlossen, changing the Ort moves Termine 2 and later
    to the new place while Termin 1, its key figures and every chart stay as before."""
    from tests.test_lists import _team

    db = world.db
    one, two, three = _series_with_first_closed(world)
    course = one.course
    client = _team(monkeypatch)
    figures = metrics.key_figures(db, ALL, today(), 9)
    charts = _charts(client, world)
    assert figures.sessions == 1 and figures.participants == 11 and figures.places == 1

    # The course page of an open Termin offers the course fields, with the note.
    page = client.get(f"/planung/{course.id}?termin={two.id}").text
    assert 'name="place_id"' in page
    assert "Änderungen gelten für die offenen Termine; abgeschlossene behalten ihre Angaben." in page

    changed = _move_to(world, two, place_id=world.makerspace.id)
    db.commit()
    assert "place" in changed
    assert (two.place, three.place, course.place) == (world.makerspace,) * 3
    assert one.place is world.school and one.has_frozen_values
    assert (one.frozen_context, one.frozen_format, one.frozen_class_level) == (world.context, world.format, 3)
    assert one.target_group == "Schulklasse"

    # Termin 1, its key figures and every chart stay as they were.
    assert metrics.key_figures(db, ALL, today(), 9) == figures
    assert _charts(client, world) == charts

    # The Verlauf: the course's change, and that Termin 1 keeps its values.
    assert ("place", world.school.name) in [
        (h.field, h.old_value) for h in _history(db, "courses", course.id)
    ]
    kept = [h for h in _history(db, "appointments", one.id) if h.field == courses.KEPT_FIELD]
    assert len(kept) == 1 and f"Ort {world.school.name}" in kept[0].new_value
    assert kept[0].reason == courses.KEPT_REASON.format(day=f"am {one.date:%d.%m.%Y}")
    assert not [h for h in _history(db, "appointments", two.id) if h.field == courses.KEPT_FIELD]

    # Termin 1's page shows its own values and what the course has now; the list marks it.
    page = client.get(f"/planung/{course.id}?termin={one.id}").text
    assert f"<dd>{world.school.name}</dd>" in page and 'name="place_id"' not in page
    assert f"Der Kurs hat inzwischen: Ort „{lists.label(world.makerspace)}“" in page
    assert "eigene Angaben" in page

    # A second change does not freeze Termin 1 again; Termin 2 counts with the new place once held.
    _move_to(world, two, place_id=world.makerspace.id, class_level=4)
    assert len([h for h in _history(db, "appointments", one.id) if h.field == courses.KEPT_FIELD]) == 1
    assert (one.class_level, two.class_level) == (3, 4)
    workflow.mark_done(db, two, "Kim")
    workflow.save(
        db,
        two,
        world.basics(
            date=two.date.isoformat(),
            participants_female=1,
            participants_male=1,
            place_id=world.makerspace.id,
            class_level=4,
        ),
        "Kim",
    )
    workflow.finalize(db, two, "Kim", checked=True, today=today())
    db.commit()
    after = metrics.key_figures(db, ALL, today(), 9)
    assert (after.sessions, after.participants, after.places) == (2, 11, 2)


def test_abgesagt_termine_keep_their_values_and_closed_courses_stay_locked(world):
    db = world.db
    one, two, three = _series_with_first_closed(world)
    workflow.confirm(db, three, None, "Kim")
    workflow.cancel(db, three, "Schule geschlossen", "Kim")
    _move_to(world, two, context_id=world.youth.id, origin_name=[], origin_count=[])
    assert (one.context, three.context, two.context) == (world.context, world.context, world.youth)
    assert one.has_frozen_values and three.has_frozen_values and not two.has_frozen_values
    assert courses.has_open(two.course) and courses.has_closed(two.course)

    # Abgeschlossen and Abgesagt Termine themselves never offer the course fields.
    assert workflow.COURSE not in workflow.editable(one) | workflow.editable(three)
    assert workflow.COURSE in workflow.editable(two)
    assert three.stage == ABGESAGT


def test_a_correction_changes_the_termin_only(world):
    """WM-132 on a series: a correction of Ort or Klasse/Alter of an Abgeschlossen Termin changes what
    that Termin keeps, never the course or its other Termine; the stale check reads effective values."""
    db = world.db
    one, two, _ = _series_with_first_closed(world)
    course = one.course
    proposal = corrections.propose(db, one, form(place=world.makerspace.id), "anderer Raum", "Kim")
    # The course changes meanwhile: Termin 1 keeps the school, so the proposal is not stale.
    _move_to(world, two, class_level=5)
    assert one.has_frozen_values and one.class_level == 3
    corrections.confirm(db, proposal.id, "Admin")
    assert one.place is world.makerspace
    assert (course.place, two.place) == (world.school, world.school) and course.class_level == 5
    rows = [(h.field, h.new_value) for h in _history(db, "appointments", one.id)]
    assert ("place", lists.label(world.makerspace)) in rows

    # A Termin that still follows its course keeps the course's values first, then the correction.
    fresh = _series_with_first_closed(world, n=2)[0]
    proposal = corrections.propose(db, fresh, form(class_age_text="Lehrkräfte"), "Fortbildung", "Kim")
    corrections.confirm(db, proposal.id, "Admin")
    assert fresh.has_frozen_values and fresh.target_group == "Lehrkräfte"
    assert fresh.course.class_age_text == "Kl. 3" and fresh.course.target_group == "Schulklasse"
    assert fresh.place is world.school  # kept from the course
    figures = metrics.key_figures(db, metrics.Filters(target_groups=["Lehrkräfte"]), today(), 9)
    assert figures.sessions == 1


def test_list_decisions_reach_kept_values(world):
    """A merge says two entries are one: Termine that keep a merged place move as well, and a changed
    school flag reaches their target group like a course's."""
    db = world.db
    one, two, _ = _series_with_first_closed(world)
    _move_to(world, two, place_id=world.makerspace.id)
    twin = Place(name="GS Musterstadt", is_school=True, address="", status=CONFIRMED)
    db.add(twin)
    db.flush()
    usage = lists.usage(db, lists.PLACES, [world.school.id, world.makerspace.id])
    assert (usage[world.school.id].final, usage[world.makerspace.id].final) == (1, 0)
    lists.merge(db, lists.PLACES, world.school, twin, "Admin", "dieselbe Schule")
    assert one.place is twin and one.frozen_place_id == twin.id
    assert one.course.place is world.makerspace
    assert lists.frozen_using(db, lists.PLACES, twin.id) == [one]
    lists.update(db, lists.PLACES, twin, {"is_school": False}, "Admin", admin=True)
    assert one.target_group != "Schulklasse"
    db.commit()
    # SQL and Python agree on the effective place.
    stmt = (
        sa.select(Appointment.id, Appointment.place_id)
        .join(Course)
        .where(Appointment.id.in_([one.id, two.id]))
    )
    assert dict(db.execute(stmt).all()) == {one.id: twin.id, two.id: world.makerspace.id}
