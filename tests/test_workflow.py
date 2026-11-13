"""Planning on the new model (WM-110): the workflow service on PostgreSQL with demo data only."""

import datetime as dt

import pytest
import sqlalchemy as sa

from metrics import analytics, hints
from metrics.db import (
    ABGESAGT,
    ABGESCHLOSSEN,
    BESTAETIGT,
    CONFIRMED,
    DURCHGEFUEHRT,
    GEPLANT,
    PROPOSED,
    Appointment,
    Context,
    Course,
    Format,
    HistoryEntry,
    Person,
    Place,
)
from metrics.ingest import store
from metrics.planning import workflow
from metrics.planning.workflow import FormError

TODAY = dt.date(2026, 11, 13)
NEXT_WEEK = TODAY + dt.timedelta(days=7)


def form(**values) -> dict[str, list[str]]:
    """A submitted form: lists stay lists, everything else becomes one string value."""
    return {k: v if isinstance(v, list) else [str(v)] for k, v in values.items() if v is not None}


class World:
    def __init__(self, db):
        self.db = db
        one = lambda model, **kw: db.scalars(sa.select(model).filter_by(**kw)).one()  # noqa: E731
        self.school = one(Place, name="Grundschule Musterstadt")
        self.makerspace = one(Place, name="Makerspace")
        self.context = one(Context, name="Projekt A")
        self.youth = Context(name="JugendLab", is_project=True, asks_municipalities=True, status=CONFIRMED)
        self.format = Format(name="Löten", category="Engineering", status=CONFIRMED)
        self.kim = Person(name="Kim", email="kim@example.org", status=CONFIRMED)
        self.lea = Person(name="Lea", email="lea@example.org", status=CONFIRMED)
        self.sam = Person(name="Sam", email="sam@example.org", status=CONFIRMED)
        db.add_all([self.youth, self.format, self.kim, self.lea, self.sam])
        db.flush()

    def basics(self, **extra) -> dict[str, list[str]]:
        """What "Bestätigen" needs, plus format and lead."""
        values = dict(
            format_id=self.format.id,
            context_id=self.context.id,
            place_id=self.school.id,
            class_kind="klasse",
            class_level=3,
            date=NEXT_WEEK.isoformat(),
            start_time="09:00",
            end_time="11:15",
            lead=self.kim.id,
        )
        return form(**(values | extra))


@pytest.fixture
def world(db, instance):
    store.seed_reference(db, instance)
    db.flush()
    return World(db)


def _history(db, t: Appointment) -> list[HistoryEntry]:
    return list(
        db.scalars(
            sa.select(HistoryEntry)
            .where(HistoryEntry.table_name == "appointments", HistoryEntry.row_id == t.id)
            .order_by(HistoryEntry.id)
        )
    )


def test_geplant_takes_anything_and_bestaetigt_needs_the_basics(world):
    db = world.db
    t = workflow.create(db, form(date=NEXT_WEEK.isoformat()), "Kim")
    assert (t.stage, t.number, t.course.target_group) == (GEPLANT, 1, "Unbekannt")
    with pytest.raises(FormError, match="Zum Bestätigen fehlt: Beginn, Ende, Kontext, Klasse/Alter, Ort"):
        workflow.confirm(db, t, None, "Kim")
    changed = workflow.save(db, t, world.basics(), "Kim")
    assert {"place", "context", "class_age_text", "start_time", "lead"} <= set(changed)
    assert t.duration_hours == pytest.approx(2.25) and t.course.target_group == "Schulklasse"
    assert t.course.class_age_text == "Kl. 3" and t.course.class_level == 3
    workflow.confirm(db, t, None, "Kim")
    assert t.stage == BESTAETIGT
    # Every later save keeps the basics.
    with pytest.raises(FormError, match="Ab „Bestätigt“ sind Pflicht: Ort"):
        workflow.save(db, t, world.basics(place_id=""), "Kim")
    rows = [(h.field, h.old_value, h.new_value) for h in _history(db, t)]
    assert ("stage", GEPLANT, BESTAETIGT) in rows and ("start_time", None, "09:00") in rows


def test_confirm_picks_one_of_the_alternative_dates(world):
    db = world.db
    later = NEXT_WEEK + dt.timedelta(days=7)
    t = workflow.create(db, world.basics(alt_date=[later.isoformat(), NEXT_WEEK.isoformat(), ""]), "Kim")
    assert [a.date for a in t.alternatives] == [later]  # the main date is no alternative
    with pytest.raises(FormError, match="Bitte das Datum wählen"):
        workflow.confirm(db, t, TODAY, "Kim")
    workflow.confirm(db, t, later, "Kim")
    assert (t.stage, t.date, t.alternatives) == (BESTAETIGT, later, [])
    rows = [(h.field, h.reason) for h in _history(db, t)]
    assert ("alternatives", "Alternativdaten verworfen") in rows and ("date", None) in rows
    # Alternatives only while Geplant.
    workflow.save(db, t, world.basics(date=later.isoformat(), alt_date=[NEXT_WEEK.isoformat()]), "Kim")
    assert t.alternatives == []


def test_cancel_only_confirmed_with_a_reason_and_delete_only_planned(world):
    db = world.db
    t = workflow.create(db, world.basics(), "Kim")
    with pytest.raises(FormError, match="Nur bestätigte"):
        workflow.cancel(db, t, "krank", "Kim")
    with pytest.raises(FormError, match="Löschen bestätigen"):
        workflow.delete(db, t, "Kim", really=False)
    workflow.confirm(db, t, None, "Kim")
    with pytest.raises(FormError, match="Nur geplante"):
        workflow.delete(db, t, "Kim", really=True)
    with pytest.raises(FormError, match="Grund"):
        workflow.cancel(db, t, "  ", "Kim")
    workflow.cancel(db, t, "Lehrerin krank", "Kim")
    assert (t.stage, t.cancel_reason) == (ABGESAGT, "Lehrerin krank")
    with pytest.raises(FormError, match="nicht mehr geändert"):
        workflow.save(db, t, world.basics(), "Kim")

    planned = workflow.create(db, world.basics(), "Lea")
    course_id, appointment_id = planned.course.id, planned.id
    assert workflow.delete(db, planned, "Lea", really=True) == course_id
    assert db.get(Appointment, appointment_id) is None and db.get(Course, course_id) is None
    (gone,) = [h for h in _history(db, planned) if h.reason == "Termin gelöscht"]
    assert (
        gone.changed_by == "Lea" and "Löten" in gone.old_value and "Grundschule Musterstadt" in gone.old_value
    )
    assert db.scalar(sa.select(sa.func.count()).where(HistoryEntry.reason == "Kurs gelöscht")) == 1


def test_participants_in_every_stage_billing_from_durchgefuehrt(world):
    """Alex, 2026-11-13: the numbers often come in advance; only the final confirmation needs them."""
    db = world.db
    t = workflow.create(
        db, form(date=NEXT_WEEK.isoformat(), participants_female=5, participants_male=0, billed="1"), "Kim"
    )
    assert (t.participants_female, t.participants_male, t.participants_total, t.billed) == (5, 0, 5, False)
    db.flush()
    stored = db.scalars(sa.select(hints.StoredHint.rule).where(hints.StoredHint.appointment_id == t.id)).all()
    assert not {"keine-teilnehmenden", "herkunft-summe", "viele-teilnehmende"} & set(stored)
    workflow.save(db, t, world.basics(participants_female=0, participants_male=0), "Kim")
    workflow.confirm(db, t, None, "Kim")
    assert "keine-teilnehmenden" not in {f.rule for f in hints.compute(db, [t], TODAY)}
    workflow.mark_done(db, t, "Kim")
    workflow.save(
        db,
        t,
        world.basics(
            participants_female=5,
            participants_male=4,
            participants_diverse=1,
            participants_unspecified="",
            billed="1",
        ),
        "Kim",
    )
    assert (t.participants_total, t.participants_unspecified, t.billed) == (10, None, True)
    with pytest.raises(FormError, match="TN weiblich: darf nicht negativ"):
        workflow.save(db, t, world.basics(participants_female=-1), "Kim")


def test_herkunft_for_contexts_that_ask_and_must_add_up(world):
    db = world.db
    t = workflow.create(db, world.basics(context_id=world.youth.id, place_id=world.makerspace.id), "Kim")
    t.stage = DURCHGEFUEHRT
    t.date = TODAY - dt.timedelta(days=1)
    counts = dict(participants_female=3, participants_male=4)
    workflow.save(
        db,
        t,
        world.basics(
            context_id=world.youth.id,
            place_id=world.makerspace.id,
            date=t.date.isoformat(),
            class_kind="alter",
            child_age=12,
            **counts,
        ),
        "Kim",
    )
    assert workflow.asks_municipalities(t)
    assert "Herkunft" in hints.missing(t, hints.FINAL_FIELDS)
    values = dict(
        context_id=world.youth.id,
        place_id=world.makerspace.id,
        date=t.date.isoformat(),
        class_kind="alter",
        child_age=12,
        origin_name=["Musterstadt", "Beispieldorf", "Musterstadt", ""],
        origin_count=["2", "3", "1", "4"],
        **counts,
    )
    workflow.save(db, t, world.basics(**values), "Kim")
    assert sorted((o.municipality, o.participants) for o in t.origins) == [
        ("Beispieldorf", 3),
        ("Musterstadt", 3),
    ]
    with pytest.raises(FormError, match="Herkunft ergibt 6, eingetragen sind 7 Teilnehmende"):
        workflow.finalize(db, t, "Kim", checked=True, today=TODAY)
    workflow.save(db, t, world.basics(**(values | {"origin_count": ["2", "3", "2", ""]})), "Kim")
    workflow.finalize(db, t, "Kim", checked=True, today=TODAY)
    assert t.stage == ABGESCHLOSSEN


def test_final_confirmation_counts_once_and_locks_all_but_billed(world):
    db = world.db
    before = analytics.totals(db, analytics.Filters(), TODAY, 9)
    t = workflow.create(db, world.basics(), "Kim")
    workflow.confirm(db, t, None, "Kim")
    workflow.mark_done(db, t, "Kim")
    with pytest.raises(FormError, match="Fehlt für den Abschluss: TN weiblich, TN männlich"):
        workflow.finalize(db, t, "Kim", checked=False, today=NEXT_WEEK)
    workflow.save(db, t, world.basics(participants_female=6, participants_male=5), "Kim")
    # A proposed list entry keeps it from the final confirmation.
    world.format.status = PROPOSED
    with pytest.raises(FormError, match="Format „Löten \\(Engineering\\)“ wartet auf Bestätigung"):
        workflow.finalize(db, t, "Kim", checked=False, today=NEXT_WEEK)
    world.format.status = CONFIRMED
    # A warning (another Termin the same day at the same place) must be ticked as checked.
    other = workflow.create(db, world.basics(lead=world.lea.id, start_time="13:00", end_time="14:00"), "Lea")
    with pytest.raises(FormError, match="Hinweise geprüft"):
        workflow.finalize(db, t, "Kim", checked=False, today=NEXT_WEEK)
    assert t.stage == DURCHGEFUEHRT
    workflow.finalize(db, t, "Kim", checked=True, today=NEXT_WEEK)
    assert t.stage == ABGESCHLOSSEN
    reasons = [h.reason for h in _history(db, t)]
    assert "Hinweise geprüft" in reasons and "final bestätigt" in reasons
    db.flush()
    after = analytics.totals(db, analytics.Filters(), TODAY + dt.timedelta(days=30), 9)
    assert (after.sessions - before.sessions, after.participants - before.participants) == (1, 11)
    # Locked: only Abgerechnet changes; the course's shared fields too.
    assert workflow.editable(t) == {workflow.BILLED}
    with pytest.raises(FormError):
        workflow.confirm(db, t, None, "Kim")
    workflow.save(db, t, world.basics(place_id=world.makerspace.id, billed="1"), "Kim")
    assert (t.course.place, t.billed) == (world.school, True)
    workflow.set_billed(db, t, False, "Kim")
    assert t.billed is False and other.stage == GEPLANT


def test_back_and_forth_between_stages(world):
    db = world.db
    t = workflow.create(db, world.basics(), "Kim")
    workflow.confirm(db, t, None, "Kim")
    workflow.back_to_planned(db, t, "Kim")
    assert t.stage == GEPLANT
    workflow.mark_done(db, t, "Kim")
    workflow.back_to_confirmed(db, t, "Kim", TODAY)
    assert t.stage == BESTAETIGT
    workflow.mark_done(db, t, "Kim")
    with pytest.raises(FormError, match="solange das Datum nicht vorbei"):
        workflow.back_to_confirmed(db, t, "Kim", NEXT_WEEK + dt.timedelta(days=1))


def test_class_age_kinds_and_team(world):
    db = world.db
    cases = {
        ("lehrkraefte", ()): ("Lehrkräfte", None, None, None),
        ("alter", (("child_age", 12),)): ("12 Jahre", None, 12, None),
        ("band", (("age_band", "25 - 34 Jahre"),)): ("25 - 34 Jahre", None, None, "25 - 34 Jahre"),
        ("band", (("age_band", "erfunden"),)): (None, None, None, None),
    }
    for (kind, extra), expected in cases.items():
        t = workflow.create(
            db, world.basics(class_kind=kind, place_id=world.makerspace.id, **dict(extra)), "Kim"
        )
        c = t.course
        assert (c.class_age_text, c.class_level, c.age, c.age_band) == expected
        assert workflow.class_kind(c) == (kind if expected[0] else None)
    # One lead; the lead is never support as well; support any number.
    t = workflow.create(
        db,
        world.basics(lead=world.kim.id, support=[str(world.kim.id), str(world.lea.id), str(world.sam.id)]),
        "Kim",
    )
    assert t.lead == world.kim and [p.name for p in t.support] == ["Lea", "Sam"]
    with pytest.raises(FormError, match="Kursleitung: ungültige Auswahl"):
        workflow.save(db, t, world.basics(lead="x"), "Kim")


def test_busy_people_respects_the_time(world):
    db = world.db
    morning = workflow.create(db, world.basics(lead=world.kim.id, support=[str(world.lea.id)]), "Kim")
    workflow.create(db, world.basics(lead=world.sam.id, start_time="", end_time=""), "Sam")
    busy = workflow.busy_people(db, NEXT_WEEK, dt.time(10), dt.time(12))
    assert busy[world.kim.id] == "Löten (09:00–11:15)" and busy[world.lea.id] == "Löten (09:00–11:15)"
    assert busy[world.sam.id] == "Löten"  # no time: busy all day
    afternoon = workflow.busy_people(db, NEXT_WEEK, dt.time(13), dt.time(15))
    assert set(afternoon) == {world.sam.id}
    assert world.kim.id not in workflow.busy_people(
        db, NEXT_WEEK, dt.time(10), dt.time(12), exclude=morning.id
    )


def test_what_is_missing_lists_the_next_step(world):
    db = world.db
    t = workflow.create(db, form(format_id=world.format.id), "Kim")
    stored = list(db.scalars(sa.select(hints.StoredHint).where(hints.StoredHint.appointment_id == t.id)))
    checks = workflow.what_is_missing(t, stored, set())
    texts = {c.text: c.done for c in checks}
    assert texts["Format (Kursname und Kategorie)"] is True
    assert texts["Kursleitung"] is False and texts["Noch nicht bestätigt"] is False
    assert texts["Datum, Beginn, Ende: fehlt Datum, Beginn, Ende"] is False
