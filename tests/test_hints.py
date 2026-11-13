"""The hint engine (WM-113): one test per rule on in-memory Termine, then storage, decisions, the
final confirmation, the scheduler job and the "Prüfen" page on PostgreSQL. Demo data only."""

import datetime as dt
from decimal import Decimal

import pytest
import sqlalchemy as sa

from metrics import courses, hints, lists, scheduler
from metrics.db import (
    ABGESAGT,
    ABGESCHLOSSEN,
    BESTAETIGT,
    CONFIRMED,
    DURCHGEFUEHRT,
    GEPLANT,
    PROPOSED,
    AlternativeDate,
    Appointment,
    AppointmentOrigin,
    CalendarResponse,
    Context,
    Course,
    Format,
    HintDecision,
    Person,
    Place,
    Proposal,
    StoredHint,
)
from metrics.ingest import store

TODAY = dt.date(2026, 11, 13)
T = dt.time
NINE, ELEVEN = dt.time(9), dt.time(11)

KIM = Person(id=1, name="Kim", status=CONFIRMED)
LEA = Person(id=2, name="Lea", status=CONFIRMED)
SCHOOL = Place(
    id=1, name="Grundschule Am Bach", is_school=True, school_type="GS", address="Bachweg 1", status=CONFIRMED
)
CONTEXT = Context(id=1, name="Schulkooperation", status=CONFIRMED, asks_municipalities=False)
FORMAT = Format(id=1, name="Calliope-Einsteiger", category="Code", status=CONFIRMED)


def termin(
    id: int = 1,
    *,
    stage: str = DURCHGEFUEHRT,
    date: dt.date | None = TODAY,
    start: dt.time | None = NINE,
    end: dt.time | None = ELEVEN,
    lead: Person | None = KIM,
    support: tuple[Person, ...] = (),
    place: Place | None = SCHOOL,
    context: Context | None = CONTEXT,
    format: Format | None = FORMAT,
    class_age_text: str | None = "Kl. 3",
    class_level: int | None = 3,
    age: int | None = None,
    age_band: str | None = None,
    female: int | None = 5,
    male: int | None = 4,
    total: int | None = None,
    number: int | None = 1,
    duration_hours: Decimal | None = None,
    source: str = "app",
    origins: dict[str, int] | None = None,
) -> Appointment:
    """A complete Termin (no hints) unless an argument says otherwise; not added to a session."""
    course = Course(
        id=id,
        format=format,
        format_id=format.id if format else None,
        context=context,
        context_id=context.id if context else None,
        place=place,
        place_id=place.id if place else None,
        class_age_text=class_age_text,
        class_level=class_level,
        age=age,
        age_band=age_band,
        target_group="Schulklasse",
    )
    return Appointment(
        id=id,
        course=course,
        number=number,
        date=date,
        start_time=start,
        end_time=end,
        duration_hours=duration_hours,
        stage=stage,
        lead=lead,
        lead_id=lead.id if lead else None,
        support=list(support),
        participants_female=female,
        participants_male=male,
        participants_total=total if total is not None else (female or 0) + (male or 0),
        source=source,
        origins=[AppointmentOrigin(municipality=m, participants=n) for m, n in (origins or {}).items()],
    )


def found(*appointments: Appointment, day: dt.date = TODAY, names=None) -> list[hints.Found]:
    """Evaluate the first Termin with all given ones as its same-day neighbours."""
    facts = hints.Facts(day, {}, names or {})
    for t in appointments:
        if t.date:
            facts.same_day.setdefault(t.date, []).append(t)
    return hints.evaluate(appointments[0], facts)


def rules_of(result: list[hints.Found]) -> list[str]:
    return [f.rule for f in result]


def only(result: list[hints.Found], rule_id: str) -> hints.Found:
    (hit,) = [f for f in result if f.rule == rule_id]
    return hit


# --- The registry ---------------------------------------------------------------------------


def test_every_rule_is_described_and_tested():
    for r in hints.RULES.values():
        assert r.level in hints.LEVELS and r.title and r.stages
        assert ABGESAGT not in r.stages
        assert f'"{r.id}"' in TEST_SOURCE, f"rule {r.id} needs a test in tests/test_hints.py"


def test_a_complete_termin_has_no_hints():
    assert found(termin()) == []
    assert found(termin(stage=ABGESCHLOSSEN, date=dt.date(2025, 3, 1))) == []


def test_a_cancelled_termin_has_no_hints():
    assert found(termin(stage=ABGESAGT, start=None, lead=None, female=None)) == []


def test_adding_a_rule_is_one_function(monkeypatch):
    monkeypatch.setattr(hints, "RULES", dict(hints.RULES))

    @hints.rule("test-material", hints.KRITISCH, "Material knapp", stages=hints.ACTIVE)
    def equipment(t, facts):
        return ["Calliope mini: 35 benötigt, 30 vorhanden"]

    hit = only(found(termin()), "test-material")
    assert (hit.level, hit.blocking, hit.text) == (
        "Kritisch",
        False,
        "Calliope mini: 35 benötigt, 30 vorhanden",
    )
    with pytest.raises(ValueError):
        hints.rule("test-material", hints.INFO, "doppelt", stages=hints.ALL)(equipment)


# --- Required fields --------------------------------------------------------------------------


def test_rule_pflicht_abschluss_lists_what_is_missing_and_blocks():
    t = termin(start=None, lead=None, female=None, format=Format(id=2, category="Code", status=CONFIRMED))
    hit = only(found(t), "pflicht-abschluss")
    assert hit.text == "Fehlt für den Abschluss: Beginn, Kursname, Dauer, TN weiblich, Kursleitung"
    assert (hit.level, hit.blocking) == ("Kritisch", True)


def test_rule_pflicht_abschluss_asks_for_the_origins_of_a_project():
    project = Context(id=2, name="JugendLab", asks_municipalities=True, status=CONFIRMED)
    assert "Herkunft" in only(found(termin(context=project)), "pflicht-abschluss").text
    assert "pflicht-abschluss" not in rules_of(found(termin(context=project, origins={"Holzkirchen": 9})))


def test_slack_termine_need_no_times_when_they_have_a_duration():
    """The Slack Lists recorded a duration, never times; the duration stands in for them."""
    t = termin(start=None, end=None, source="slack", duration_hours=Decimal("1.5"))
    assert "pflicht-abschluss" not in rules_of(found(t))
    t = termin(start=None, end=None, source="slack")
    assert only(found(t), "pflicht-abschluss").text == "Fehlt für den Abschluss: Beginn, Ende, Dauer"


def test_rule_pflicht_bestaetigt():
    t = termin(stage=BESTAETIGT, date=TODAY + dt.timedelta(days=3), end=None, place=None, female=None)
    hit = only(found(t), "pflicht-bestaetigt")
    assert (hit.text, hit.level, hit.blocking) == ("Bestätigt, aber es fehlt: Ende, Ort", "Wichtig", False)


def test_required_fields_never_apply_to_planned_or_locked_termine():
    bare = {"start": None, "end": None, "lead": None, "female": None, "male": None, "place": None}
    assert found(termin(stage=GEPLANT, date=TODAY + dt.timedelta(days=1), **bare)) == []
    # Abgeschlossen legacy rows never had start and end: no required-field hints.
    assert (
        found(termin(stage=ABGESCHLOSSEN, date=dt.date(2020, 3, 1), source="baseline", **bare, total=12))
        == []
    )


# --- Blocking value rules ---------------------------------------------------------------------


def test_rule_ende_vor_beginn():
    hit = only(found(termin(start=T(11), end=T(9))), "ende-vor-beginn")
    assert hit.blocking and hit.text == "Ende (09:00) liegt nicht nach dem Beginn (11:00)"
    assert "dauer" not in rules_of(found(termin(start=T(11), end=T(9))))


def test_rule_dauer():
    assert only(found(termin(start=T(8), end=T(21))), "dauer").text == (
        "Dauer 13,00 h liegt nicht zwischen 0,25 und 12 Stunden"
    )
    assert "dauer" in rules_of(
        found(termin(start=None, end=None, duration_hours=Decimal("0.1"), source="slack"))
    )
    assert "dauer" not in rules_of(found(termin(start=T(9), end=T(9, 15))))


def test_rule_keine_teilnehmenden():
    hit = only(found(termin(female=0, male=0)), "keine-teilnehmenden")
    assert hit.blocking and hit.level == "Kritisch"
    assert "keine-teilnehmenden" not in rules_of(found(termin(stage=GEPLANT, female=0, male=0)))


def test_rule_terminnummer():
    assert only(found(termin(number=0)), "terminnummer").text == "Terminnummer 0 ist kleiner als 1"


def test_rule_terminnummer_doppelt():
    t = termin(1, number=1)
    facts = hints.Facts(
        TODAY, {}, {}, numbers={1: [(1, 1, TODAY), (1, 7, dt.date(2026, 11, 6)), (2, 8, None)]}
    )
    hit = only(hints.evaluate(t, facts), "terminnummer-doppelt")
    assert (hit.text, hit.other_id, hit.blocking) == (
        "Terminnummer 1 hat auch der Termin am 06.11.2026",
        7,
        True,
    )
    assert "terminnummer-doppelt" not in rules_of(found(t))


def test_rule_klasse_unlesbar():
    t = termin(class_age_text="gemischt", class_level=None)
    assert only(found(t), "klasse-unlesbar").text == (
        "Klasse/Alter „gemischt“ ist weder Klassenstufe, Alter, Altersgruppe noch Lehrkräfte"
    )


def test_rule_alter_erwachsen():
    youth = Place(id=3, name="Jugendhaus", is_school=False, address="Weg 2", status=CONFIRMED)
    t = termin(place=youth, class_age_text="45 Jahre", class_level=None, age=45)
    hit = only(found(t), "alter-erwachsen")
    assert hit.blocking and "45 Jahre" in hit.text
    assert "alter-erwachsen" not in rules_of(found(termin(class_age_text="Kl. 13 / 19 Jahre", age=19)))


def test_rule_herkunft_summe():
    hit = only(found(termin(origins={"Holzkirchen": 5, "Otterfing": 3})), "herkunft-summe")
    assert hit.text == "Herkunft ergibt 8, eingetragen sind 9 Teilnehmende"
    assert "herkunft-summe" not in rules_of(found(termin(origins={"Holzkirchen": 9})))


def test_rule_datum_zukunft():
    hit = only(found(termin(date=TODAY + dt.timedelta(days=1))), "datum-zukunft")
    assert hit.blocking and hit.text == "Durchgeführt, aber das Datum 14.11.2026 liegt in der Zukunft"


# --- Warnings on values -----------------------------------------------------------------------


def test_rule_schule_mit_alter():
    t = termin(class_age_text="12 Jahre", class_level=None, age=12)
    hit = only(found(t), "schule-mit-alter")
    assert (hit.level, hit.blocking) == ("Info", False)
    assert "schule-mit-alter" not in rules_of(found(termin(class_age_text="Lehrkräfte", class_level=None)))


def test_rule_viele_teilnehmende():
    assert (
        only(found(termin(female=30, male=25)), "viele-teilnehmende").text == "55 Teilnehmende: bitte prüfen"
    )


# --- Planning -------------------------------------------------------------------------------


def test_rule_geplant_vorbei():
    hit = only(found(termin(stage=GEPLANT, date=TODAY - dt.timedelta(days=1))), "geplant-vorbei")
    assert hit.level == "Kritisch" and not hit.blocking
    assert found(termin(stage=GEPLANT, date=None)) == []


def _planned(id, start, end, **kwargs):
    return termin(id, stage=BESTAETIGT, date=TODAY + dt.timedelta(days=7), start=start, end=end, **kwargs)


def test_rule_person_doppelt():
    a, b = _planned(1, T(9), T(11)), _planned(2, T(10), T(12), support=(KIM,), lead=LEA)
    hit = only(found(a, b), "person-doppelt")
    assert (hit.text, hit.other_id) == ("Kim gleichzeitig eingeplant mit „Calliope-Einsteiger“ (Termin 2)", 2)
    assert only(found(b, a), "person-doppelt").other_id == 1


def test_rule_person_uhrzeit():
    a, b = _planned(1, T(9), T(11)), _planned(2, None, None)
    assert only(found(a, b), "person-uhrzeit").text == (
        "Kim am selben Tag mit „Calliope-Einsteiger“ (Termin 2), Uhrzeit fehlt"
    )


def test_rule_gleichzeitig():
    a, b = _planned(1, T(9), T(11)), _planned(2, T(10), T(12), lead=LEA)
    assert only(found(a, b), "gleichzeitig").text.startswith("Gleichzeitig am selben Ort")
    other = Place(id=4, name="Bücherei", is_school=False, address="Platz 1", status=CONFIRMED)
    c = _planned(3, T(10), T(12), lead=LEA, place=other)
    assert only(found(a, c), "gleichzeitig").text.startswith("Zur selben Zeit wie")


def test_rule_selber_tag():
    a, b = _planned(1, T(9), T(11)), _planned(2, T(13), T(15), lead=LEA)
    assert only(found(a, b), "selber-tag").level == "Info"


def test_rule_alternativdatum():
    week, fortnight = TODAY + dt.timedelta(days=7), TODAY + dt.timedelta(days=14)
    a = termin(1, stage=GEPLANT, date=week)
    a.alternatives = [AlternativeDate(date=fortnight)]
    b = termin(2, stage=BESTAETIGT, date=fortnight)  # Kim leads both
    c = termin(3, stage=BESTAETIGT, date=fortnight, lead=LEA, start=T(14), end=T(16))
    hits = [f for f in found(a, b, c) if f.rule == "alternativdatum"]
    assert [(h.level, h.other_id) for h in hits] == [("Info", 2), ("Info", 3)]
    assert (
        hits[0].text
        == f"Alternativdatum {fortnight:%d.%m.%Y}: Kim eingeplant bei „Calliope-Einsteiger“ (Termin 2)"
    )
    assert hits[1].text.startswith(f"Alternativdatum {fortnight:%d.%m.%Y}: anderer Kurs")
    # Only the main date counts for the other Termine, and only while Geplant.
    assert "alternativdatum" not in rules_of(found(b, a, c))


def test_conflicts_only_from_today_on():
    past = dict(stage=DURCHGEFUEHRT, date=TODAY - dt.timedelta(days=2))
    a, b = termin(1, **past), termin(2, **past, start=T(10), end=T(12), number=2)
    assert not {"person-doppelt", "gleichzeitig", "selber-tag"} & set(rules_of(found(a, b)))


# --- Places and list entries ------------------------------------------------------------------


def test_rule_ort_angaben():
    no_address = Place(id=5, name="Turnhalle Nord", is_school=False, address=None, status=CONFIRMED)
    assert only(found(termin(place=no_address)), "ort-angaben").text == (
        "Ort „Turnhalle Nord“: Adresse fehlt (Straße, PLZ, Ort)"
    )
    no_school = Place(id=6, name="Halle Süd", is_school=None, address="Weg 3", status=CONFIRMED)
    assert "Schule ja/nein fehlt" in only(found(termin(place=no_school)), "ort-angaben").text
    online = Place(id=7, name="Online", is_school=False, address="", status=CONFIRMED)
    assert "ort-angaben" not in rules_of(found(termin(place=online)))


def test_rule_eintrag_wartet_with_a_similar_known_name():
    proposed = Place(id=8, name="Grundschule am Bach", is_school=True, address="Bachweg 1", status=PROPOSED)
    t = termin(place=proposed)
    hit = only(found(t, names={"orte": {"Grundschule Am Bach"}}), "eintrag-wartet")
    assert hit.text == "Ort „Grundschule am Bach“ wartet auf Bestätigung, ähnlich: „Grundschule Am Bach“"
    assert (hit.level, hit.blocking) == ("Info", False)
    # Formats are compared by name, not by the label with the category.
    fablab = Format(id=9, name="FabLab", category="Engineering", status=PROPOSED)
    hit = only(found(termin(format=fablab), names={"formate": {"Zauberstab"}}), "eintrag-wartet")
    assert hit.text == "Format „FabLab (Engineering)“ wartet auf Bestätigung"
    solar = Format(id=10, name="Solarlampen", category="Engineering", status=PROPOSED)
    hit = only(found(termin(format=solar), names={"formate": {"Solarlampe"}}), "eintrag-wartet")
    assert hit.text.endswith("ähnlich: „Solarlampe“")


# --- Calendar and invitations (WM-110) -------------------------------------------------------


def test_rule_kalender():
    t = _planned(1, T(9), T(11))
    t.calendar_uid = "zahlen-termin-1"
    for state, text in (
        ("missing", "Kalendereintrag fehlt (war eingetragen)"),
        ("outdated", "weicht vom Termin ab"),
    ):
        t.calendar_state = state
        hit = only(found(t), "kalender")
        assert hit.level == "Wichtig" and text in hit.text
    t.calendar_state = "ok"
    assert "kalender" not in rules_of(found(t))
    # Geplant: no event, unless one is still to be cancelled after going back (2026-11-13).
    t.stage = GEPLANT
    assert "kalender" not in rules_of(found(t))
    t.calendar_state = "outdated"
    assert only(found(t), "kalender").text == "Kalendereintrag noch nicht abgesagt (Termin wieder geplant)"


def test_rule_nicht_im_kalender():
    t = _planned(1, T(9), T(11))
    facts = hints.Facts(TODAY, {t.date: [t]}, {}, calendar=True)
    assert [f.rule for f in hints.evaluate(t, facts)] == ["nicht-im-kalender"]
    assert found(t) == []  # without a configured calendar nothing is missing
    t.calendar_uid = "x"
    assert hints.evaluate(t, facts) == []


def test_rule_einladung_abgelehnt():
    kim = Person(id=1, name="Kim", email="kim@example.org", status=CONFIRMED)
    t = _planned(1, T(9), T(11), lead=kim, support=(LEA,))
    t.calendar_responses = [
        CalendarResponse(email="kim@example.org", person_id=1, partstat="DECLINED"),
        CalendarResponse(email="lea@example.org", person_id=2, partstat="ACCEPTED"),
        CalendarResponse(email="weg@example.org", person_id=9, partstat="DECLINED"),  # no longer in the team
    ]
    hit = only(found(t), "einladung-abgelehnt")
    assert (hit.level, hit.text) == ("Wichtig", "Kim hat die Kalendereinladung abgelehnt")


# --- Duplicates -------------------------------------------------------------------------------


def test_rule_doppelt():
    a, b = termin(1), termin(2)
    assert only(found(a, b), "doppelt").text == "Gleiche Angaben wie Termin 2 am selben Tag"
    # A second group of the same class is not a duplicate.
    assert "doppelt" not in rules_of(found(a, termin(2, female=7)))
    assert "doppelt" not in rules_of(found(a, termin(2, start=T(12), end=T(14))))


def test_reviewed_legacy_pairs_are_not_flagged_again():
    """The baseline went through the duplicate review (WM-10, WM-12); its kept pairs stay quiet."""
    legacy = dict(stage=ABGESCHLOSSEN, date=dt.date(2021, 4, 15), source="baseline", start=None, end=None)
    assert found(termin(1, **legacy), termin(2, **legacy)) == []
    assert "doppelt" in rules_of(found(termin(1, **legacy), termin(2, **{**legacy, "source": "app"})))


# --- Locked Termine -------------------------------------------------------------------------


def test_findings_on_abgeschlossen_termine_are_info_and_never_block():
    youth = Place(id=3, name="Jugendhaus", is_school=False, address="Weg 2", status=CONFIRMED)
    t = termin(stage=ABGESCHLOSSEN, date=dt.date(2024, 10, 24), place=youth, class_level=None, age=45)
    hit = only(found(t), "alter-erwachsen")
    assert (hit.level, hit.blocking) == ("Info", False)


# --- PostgreSQL: storage, decisions, final confirmation, job, page ----------------------------


@pytest.fixture
def seeded(db, instance):
    store.seed_reference(db, instance)
    db.commit()
    return db


def _first(db, model):
    return db.scalars(sa.select(model).where(model.status == CONFIRMED).order_by(model.id)).first()


def _complete(db, termin_: Appointment) -> Appointment:
    """Fill every field the final confirmation needs."""
    course = termin_.course
    course.place = db.scalars(sa.select(Place).where(Place.is_school.is_(True)).order_by(Place.id)).first()
    course.context = db.scalars(
        sa.select(Context).where(Context.asks_municipalities.is_(False)).order_by(Context.id)
    ).first()
    course.format = db.scalars(sa.select(Format).where(Format.name.is_not(None))).first() or Format(
        name="Löten", category="Engineering", status=CONFIRMED
    )
    course.class_age_text, course.class_level = "Kl. 4", 4
    termin_.start_time, termin_.end_time = T(9), T(11)
    termin_.participants_female, termin_.participants_male, termin_.participants_total = 6, 5, 11
    termin_.lead = _first(db, Person)
    db.flush()
    return termin_


def test_refresh_stores_hints_and_follows_a_moved_termin(seeded):
    course = courses.create_course(seeded, by="Kim")
    a = courses.add_appointment(seeded, course, by="Kim", date=TODAY + dt.timedelta(days=5), stage=GEPLANT)
    b = courses.add_appointment(seeded, course, by="Kim", date=TODAY + dt.timedelta(days=5), stage=GEPLANT)
    hints.refresh(seeded, [a.id], TODAY)
    stored = seeded.scalars(sa.select(StoredHint)).all()
    assert sorted((h.appointment_id, h.rule, h.other_appointment_id) for h in stored) == [
        (a.id, "doppelt", b.id),
        (a.id, "selber-tag", b.id),
        (b.id, "doppelt", a.id),
        (b.id, "selber-tag", a.id),
    ]
    # Moving one Termin away clears the hints on both, including the one left behind.
    a.date = TODAY + dt.timedelta(days=9)
    hints.refresh(seeded, [a.id], TODAY)
    assert seeded.scalars(sa.select(StoredHint)).all() == []


def test_accepting_a_warning_keeps_it_visible_but_uncounted(seeded):
    course = courses.create_course(seeded, by="Kim")
    t = courses.add_appointment(seeded, course, by="Kim", date=TODAY - dt.timedelta(days=3), stage=GEPLANT)
    hints.refresh(seeded, [t.id], TODAY)
    assert hints.sidebar_counts(seeded) == {"hints_high": 1, "hints_medium": 0}
    with pytest.raises(hints.HintError, match="Grund"):
        hints.accept(seeded, t.id, "geplant-vorbei", "  ", "Alex")
    decision = hints.accept(seeded, t.id, "geplant-vorbei", "Termin wird nachgetragen", "Alex")
    assert (decision.decision, decision.decided_by) == ("akzeptiert", "Alex")
    assert hints.sidebar_counts(seeded) == {"hints_high": 0, "hints_medium": 0}
    rows, total = hints.queue(seeded, hints.QueueFilter(status=hints.ACCEPTED_ONLY))
    assert total == 1 and rows[0].decision.reason == "Termin wird nachgetragen"
    assert hints.queue(seeded, hints.QueueFilter())[1] == 0
    # The decision survives the nightly recomputation.
    hints.refresh_all(seeded, TODAY)
    assert hints.sidebar_counts(seeded)["hints_high"] == 0


def test_an_accepted_warning_covers_only_its_finding(seeded):
    """Alex, 2026-11-13: a new conflict under the same rule on the same Termin is open again."""
    day = TODAY + dt.timedelta(days=5)
    course = courses.create_course(seeded, by="Kim")
    a = courses.add_appointment(seeded, course, by="Kim", date=day, stage=GEPLANT)
    b = courses.add_appointment(seeded, course, by="Kim", date=day, stage=GEPLANT)
    hints.refresh(seeded, [a.id], TODAY)

    def open_on_a(rule_id):
        rows, _ = hints.queue(seeded, hints.QueueFilter(rule=rule_id))
        return [r.hint.other_appointment_id for r in rows if r.appointment.id == a.id]

    assert open_on_a("selber-tag") == [b.id]
    decision = hints.accept(seeded, a.id, "selber-tag", "zwei Gruppen, beide mit Team", "Lea")
    assert decision.finding_key == f"termin:{b.id}" and open_on_a("selber-tag") == []
    c = courses.add_appointment(seeded, course, by="Kim", date=day, stage=GEPLANT)
    hints.refresh(seeded, [c.id], TODAY)
    assert open_on_a("selber-tag") == [c.id]  # the new conflict is open, the accepted one stays accepted
    with pytest.raises(hints.HintError, match="einzeln"):
        hints.accept(seeded, a.id, "doppelt", "x", "Lea")  # two findings: name the one
    hints.accept(seeded, a.id, "doppelt", "verschiedene Gruppen", "Lea", key=f"termin:{c.id}")
    assert [
        h.other_appointment_id
        for h in seeded.scalars(
            sa.select(StoredHint).where(StoredHint.appointment_id == a.id, StoredHint.rule == "doppelt")
        )
    ] == [b.id, c.id]
    # A different text of the same rule (another missing value) is a different finding too.
    t = courses.add_appointment(seeded, course, by="Kim", date=TODAY, stage=BESTAETIGT)
    hints.refresh(seeded, [t.id], TODAY)
    hints.accept(seeded, t.id, "pflicht-bestaetigt", "kommt noch", "Lea")
    t.start_time = T(9)
    hints.refresh(seeded, [t.id], TODAY)
    rows, _ = hints.queue(seeded, hints.QueueFilter(rule="pflicht-bestaetigt"))
    assert [r.decision for r in rows if r.appointment.id == t.id] == [None]


def test_blocking_hints_cannot_be_accepted(seeded):
    course = courses.create_course(seeded, by="Kim")
    t = courses.add_appointment(seeded, course, by="Kim", date=TODAY, stage=DURCHGEFUEHRT)
    hints.refresh(seeded, [t.id], TODAY)
    with pytest.raises(hints.HintError, match="blockierend"):
        hints.accept(seeded, t.id, "pflicht-abschluss", "passt schon", "Alex")
    assert seeded.scalars(sa.select(HintDecision)).all() == []


def test_final_confirmation_refuses_while_a_blocking_hint_is_open(seeded):
    course = courses.create_course(seeded, by="Kim")
    t = courses.add_appointment(
        seeded, course, by="Kim", date=TODAY - dt.timedelta(days=1), stage=DURCHGEFUEHRT
    )
    with pytest.raises(courses.NotReady, match="Fehlt für den Abschluss"):
        courses.finally_confirm(seeded, t, "Kim")
    _complete(seeded, t)
    t.participants_female = t.participants_male = 0
    with pytest.raises(courses.NotReady, match="Keine Teilnehmenden"):
        courses.finally_confirm(seeded, t, "Kim")
    t.participants_female, t.participants_male = 6, 5
    courses.finally_confirm(seeded, t, "Kim")
    assert t.stage == ABGESCHLOSSEN
    # Its stored hints follow the new stage.
    assert all(h.level == "Info" for h in seeded.scalars(sa.select(StoredHint)))


def test_scheduler_job_recomputes_every_hint(seeded):
    course = courses.create_course(seeded, by="Kim")
    t = courses.add_appointment(seeded, course, by="Kim", date=dt.date(2026, 11, 1), stage=GEPLANT)
    seeded.commit()
    job = next(j for j in scheduler.JOBS if j.name == "hints")
    message = job.run(dt.datetime(2026, 11, 13, 0, 30, tzinfo=dt.UTC))
    assert message == "1 hints on 1 Termine (1 Kritisch, 0 Wichtig, 0 Info)"
    seeded.expire_all()
    assert [h.rule for h in seeded.scalars(sa.select(StoredHint))] == ["geplant-vorbei"]
    assert t.id


def test_list_decisions_refresh_the_hints(seeded, monkeypatch):
    from tests.test_lists import _admin

    place = lists.propose(
        seeded, lists.PLACES, {"name": "Jugendhaus Seeblick", "is_school": False, "address": "See 2"}, "Kim"
    ).entry
    course = courses.create_course(seeded, by="Kim", place_id=place.id)
    t = courses.add_appointment(seeded, course, by="Kim", date=TODAY + dt.timedelta(days=4), stage=GEPLANT)
    seeded.commit()
    assert [h.rule for h in seeded.scalars(sa.select(StoredHint))] == ["eintrag-wartet"]
    admin = _admin(monkeypatch)
    assert (
        admin.post(f"/stammdaten/orte/{place.id}/bestaetigen", data={}, follow_redirects=False).status_code
        == 303
    )
    seeded.expire_all()
    assert seeded.scalars(sa.select(StoredHint).where(StoredHint.appointment_id == t.id)).all() == []


def test_pruefen_page_lists_proposals_corrections_and_hints(seeded, monkeypatch):
    from tests.test_lists import _admin, _team

    place = lists.propose(
        seeded, lists.PLACES, {"name": "Jugendhaus Seeblick", "is_school": False, "address": "See 2"}, "Kim"
    ).entry
    course = courses.create_course(seeded, by="Kim", place_id=place.id)
    t = courses.add_appointment(seeded, course, by="Kim", date=dt.date(2026, 10, 1), stage=GEPLANT)
    hints.refresh(seeded, [t.id])
    seeded.commit()

    assert _team(monkeypatch).get("/pruefen", follow_redirects=False).status_code == 403
    admin = _admin(monkeypatch)
    page = admin.get("/pruefen").text
    assert "Jugendhaus Seeblick" in page and "Vorgeschlagene Korrekturen" in page
    assert "Datum vorbei, nie bestätigt" in page and "wartet auf Bestätigung" in page
    assert 'href="/pruefen"' in page  # in the sidebar now
    only_info = admin.get("/pruefen?stufe=Info").text
    assert "nie bestätigt" not in only_info and "wartet auf Bestätigung" in only_info
    assert "nie bestätigt" not in admin.get("/pruefen?von=2026-10-02").text
    assert "nie bestätigt" not in admin.get("/pruefen?regel=eintrag-wartet").text

    refused = admin.post(
        "/pruefen/akzeptieren", data={"termin": str(t.id), "regel": "geplant-vorbei", "grund": ""}
    )
    assert refused.status_code == 422 and "Bitte einen Grund angeben" in refused.text
    accepted = admin.post(
        "/pruefen/akzeptieren",
        data={"termin": str(t.id), "regel": "geplant-vorbei", "grund": "kommt in WM-110"},
        follow_redirects=False,
    )
    assert accepted.status_code == 303
    assert "nie bestätigt" not in admin.get("/pruefen").text
    shown = admin.get("/pruefen?status=akzeptiert").text
    assert "nie bestätigt" in shown and "kommt in WM-110" in shown


def test_pruefen_shows_corrections_once_wm132_adds_them(seeded, monkeypatch):
    from tests.test_lists import _admin

    seeded.add(
        Proposal(
            kind="korrektur",
            target_table="appointments",
            target_id=1,
            proposed_values={"participants_female": 7},
            reason="Liste nachgezählt",
            proposed_by="Kim",
        )
    )
    seeded.commit()
    assert "Liste nachgezählt" in _admin(monkeypatch).get("/pruefen").text


with open(__file__, encoding="utf-8") as _source:
    TEST_SOURCE = _source.read()
