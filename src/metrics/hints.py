"""Hints on Termine: the planning rules and the data-quality checks (WM-113, PROPOSAL.md 4.9).

Every rule is a plain function registered with `@rule`: an ID, a level (Kritisch, Wichtig, Info),
a German title, the stages it applies to and whether it blocks the final confirmation. It gets one
Termin and the `Facts` of the run (today, the other Termine on the same day, the confirmed list
names) and returns the German texts of its findings (or `Finding`s naming another Termin), nothing
when all is well. The rules and their cases are listed in `PLANUNG_REGELN.md`.

Adding a rule (equipment WM-140, absences WM-142, capacity WM-131):

    @rule("material-knapp", KRITISCH, "Material doppelt verplant", stages=ACTIVE)
    def equipment_short(t: Appointment, facts: Facts):
        ...  # return [] or ["Calliope mini: 35 benötigt, 30 vorhanden"]

plus one test in `tests/test_hints.py`. If the rule needs more than the Termin and its same-day
neighbours, add the data to `Facts` (loaded once per run in `facts_for`). Nothing else changes:
the queue, the filters, the counts and the final confirmation read `RULES`.

Levels and stages:
- A blocking rule keeps a Termin from "Final bestätigen" (`courses.finally_confirm`); its hint
  cannot be accepted. Any other hint can be accepted with a reason (`accept`, by team members on
  the course page and admins on "Prüfen"): it then no longer counts but stays visible as accepted.
  A decision covers the one finding it was taken on (`finding_key`: the other Termin of a
  conflict, else the text), so a new finding of the same rule on the Termin is open again.
- Abgesagt Termine get no hints. Abgeschlossen Termine (the legacy data and finally confirmed
  ones) are locked and already counted: only the value checks apply to them, never the
  required-field, planning or list rules, and every finding there is Info and never blocking: a
  data-quality note to fix through a correction (WM-132) or to accept.

Stored, not computed per request: the findings are written to `hints` (`refresh`), so the sidebar
counts, the "Prüfen" queue and later Mein Bereich are plain queries instead of running every rule
over every Termin on each page. `refresh(session, ids)` recomputes the given Termine and their
same-day neighbours after a change (WM-110 calls it after saving); the scheduler job `hints`
recomputes everything nightly, since some rules depend on the date ("Geplant, Datum vorbei").
"""

import datetime as dt
from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import case, delete, func, select
from sqlalchemy.orm import Session, selectinload

from metrics import lists
from metrics.config import get_settings
from metrics.db import (
    ABGESAGT,
    ABGESCHLOSSEN,
    BESTAETIGT,
    CAL_MISSING,
    CAL_OUTDATED,
    CONFIRMED,
    DURCHGEFUEHRT,
    GEPLANT,
    PROPOSED,
    AlternativeDate,
    Appointment,
    Course,
    HintDecision,
    StoredHint,
    appointment_support,
)
from metrics.ingest.records import ADULT_MIN_AGE, TEACHERS, parse_class_age
from metrics.instance import clean_name
from metrics.names_check import suggestion

LOCAL = ZoneInfo("Europe/Berlin")

KRITISCH = "Kritisch"
WICHTIG = "Wichtig"
INFO = "Info"
LEVELS = (KRITISCH, WICHTIG, INFO)
# The `hint_badge` macro's names for the levels.
BADGE = {KRITISCH: "high", WICHTIG: "medium", INFO: "low"}

ACCEPTED = "akzeptiert"  # hint_decisions.decision

# Stage sets for `@rule(stages=...)`. Abgesagt never gets hints.
PLANNED = (GEPLANT, BESTAETIGT)
ACTIVE = (GEPLANT, BESTAETIGT, DURCHGEFUEHRT)  # not yet finally confirmed
HELD = (DURCHGEFUEHRT, ABGESCHLOSSEN)
ALL = (GEPLANT, BESTAETIGT, DURCHGEFUEHRT, ABGESCHLOSSEN)

# Sources whose records never had start and end times (only a duration): the Slack Lists.
WITHOUT_TIMES = ("slack",)
# Sources reviewed for duplicates before they were frozen (WM-10, WM-12).
REVIEWED = ("baseline",)

MIN_HOURS, MAX_HOURS = Decimal("0.25"), Decimal("12")
MANY_PARTICIPANTS = 50  # more is allowed, but worth a look (the legacy data's maximum is below 40)


def today() -> dt.date:
    return dt.datetime.now(LOCAL).date()


@dataclass(frozen=True)
class Finding:
    """What a rule found; `other_id` names the other Termin of a conflict or duplicate."""

    text: str
    other_id: int | None = None


@dataclass(frozen=True)
class Rule:
    id: str
    level: str
    title: str  # short German name for filters and docs
    stages: tuple[str, ...]
    blocking: bool
    check: Callable[["Appointment", "Facts"], Iterable["Finding | str"]]


RULES: dict[str, Rule] = {}


def rule(rule_id: str, level: str, title: str, *, stages: tuple[str, ...], blocking: bool = False):
    """Register a rule function (see the module docstring)."""
    if level not in LEVELS:
        raise ValueError(level)

    def register(check):
        if rule_id in RULES:
            raise ValueError(f"rule {rule_id} registered twice")
        RULES[rule_id] = Rule(rule_id, level, title, stages, blocking, check)
        return check

    return register


@dataclass
class Facts:
    """What the rules may look at besides the Termin itself, loaded once per run."""

    today: dt.date
    same_day: dict[dt.date, list[Appointment]] = field(default_factory=dict)  # not Abgesagt
    confirmed_names: dict[str, set[str]] = field(default_factory=dict)  # list slug -> names
    calendar: bool = False  # a course calendar is configured (CALDAV_URL)
    # course ID -> (Terminnummer, Termin ID, date) of its numbered Termine that are not Abgesagt
    numbers: dict[int, list[tuple[int, int, dt.date | None]]] = field(default_factory=dict)


@dataclass(frozen=True)
class Found:
    """One hint of a computed run: the rule's finding with its effective level."""

    appointment_id: int
    rule: str
    level: str
    blocking: bool
    text: str
    other_id: int | None = None

    @property
    def key(self) -> str:
        return finding_key(self.text, self.other_id)


def finding_key(text: str, other_id: int | None) -> str:
    """What an accepted warning is tied to: the other Termin of a conflict, else the finding's text.

    A decision covers only the finding it was taken on (Alex, 2026-11-13): when the same rule later
    finds something else on the Termin (another Termin, another missing value), it is open again.
    """
    return f"termin:{other_id}" if other_id is not None else text


# --- Small readers --------------------------------------------------------------------------


def duration(t: Appointment) -> Decimal | None:
    """Dauer: Ende minus Beginn when both are set, else the recorded duration."""
    if t.start_time and t.end_time:
        start = dt.datetime.combine(dt.date.min, t.start_time)
        end = dt.datetime.combine(dt.date.min, t.end_time)
        return Decimal((end - start).total_seconds()) / 3600
    return t.duration_hours


def participants(t: Appointment) -> int | None:
    """Teilnehmende: the sum of the counts that are entered, else the recorded total."""
    counts = [
        t.participants_female,
        t.participants_male,
        t.participants_diverse,
        t.participants_unspecified,
    ]
    if any(c is not None for c in counts):
        return sum(c or 0 for c in counts)
    return t.participants_total


def _has_class_age(course: Course) -> bool:
    return bool(
        clean_name(course.class_age_text)
        or course.class_level is not None
        or course.age is not None
        or course.age_band
    )


def _team(t: Appointment) -> dict[int, str]:
    people = ([t.lead] if t.lead else []) + list(t.support)
    return {p.id: p.name for p in people}


def _title(t: Appointment) -> str:
    course = t.course
    if course.format and course.format.name:
        return course.format.name
    return course.context.name if course.context else f"Termin {t.id}"


# --- Required fields (PLANUNG_REGELN.md, "Pflichtfelder je Phase") --------------------------

CONFIRM_FIELDS: dict[str, Callable[[Appointment], bool]] = {
    "Datum": lambda t: t.date is not None,
    "Beginn": lambda t: t.start_time is not None,
    "Ende": lambda t: t.end_time is not None,
    "Kontext": lambda t: t.course.context is not None,
    "Klasse/Alter": lambda t: _has_class_age(t.course),
    "Ort": lambda t: t.course.place is not None,
}
FINAL_FIELDS: dict[str, Callable[[Appointment], bool]] = {
    **CONFIRM_FIELDS,
    "Kursname": lambda t: bool(t.course.format and t.course.format.name),
    "Kurskategorie": lambda t: bool(t.course.format and t.course.format.category),
    "Dauer": lambda t: duration(t) is not None,
    "Terminnummer": lambda t: t.number is not None,
    "TN weiblich": lambda t: t.participants_female is not None,
    "TN männlich": lambda t: t.participants_male is not None,
    "Kursleitung": lambda t: t.lead is not None,
    "Herkunft": lambda t: not (t.course.context and t.course.context.asks_municipalities) or bool(t.origins),
}


def missing(t: Appointment, fields: dict[str, Callable[[Appointment], bool]]) -> list[str]:
    absent = [label for label, filled in fields.items() if not filled(t)]
    if t.source in WITHOUT_TIMES and t.duration_hours is not None:
        # The Slack Lists recorded a duration but never times: the duration stands in for them.
        absent = [label for label in absent if label not in ("Beginn", "Ende")]
    return absent


@rule("pflicht-bestaetigt", WICHTIG, "Bestätigt, Pflichtangaben fehlen", stages=(BESTAETIGT,))
def confirmed_fields(t: Appointment, facts: Facts):
    if absent := missing(t, CONFIRM_FIELDS):
        return [f"Bestätigt, aber es fehlt: {', '.join(absent)}"]
    return []


@rule(
    "pflicht-abschluss",
    KRITISCH,
    "Durchgeführt, Pflichtangaben fehlen",
    stages=(DURCHGEFUEHRT,),
    blocking=True,
)
def final_fields(t: Appointment, facts: Facts):
    if absent := missing(t, FINAL_FIELDS):
        return [f"Fehlt für den Abschluss: {', '.join(absent)}"]
    return []


# --- Blocking value rules (PLANUNG_REGELN.md, "Blockierende Regeln") ------------------------


@rule("ende-vor-beginn", KRITISCH, "Ende nicht nach dem Beginn", stages=ALL, blocking=True)
def end_before_start(t: Appointment, facts: Facts):
    if t.start_time and t.end_time and t.end_time <= t.start_time:
        return [f"Ende ({t.end_time:%H:%M}) liegt nicht nach dem Beginn ({t.start_time:%H:%M})"]
    return []


@rule("dauer", KRITISCH, "Dauer nicht zwischen 0,25 und 12 Stunden", stages=ALL, blocking=True)
def duration_range(t: Appointment, facts: Facts):
    hours = duration(t)
    if hours is None or (t.start_time and t.end_time and t.end_time <= t.start_time):
        return []  # an end before the start is its own rule
    if not MIN_HOURS <= hours <= MAX_HOURS:
        shown = f"{hours:.2f}".replace(".", ",")
        return [f"Dauer {shown} h liegt nicht zwischen 0,25 und 12 Stunden"]
    return []


@rule("keine-teilnehmenden", KRITISCH, "Keine Teilnehmenden", stages=HELD, blocking=True)
def no_participants(t: Appointment, facts: Facts):
    n = participants(t)
    return ["Keine Teilnehmenden eingetragen (0)"] if n is not None and n <= 0 else []


@rule("terminnummer", KRITISCH, "Terminnummer kleiner als 1", stages=ALL, blocking=True)
def session_number(t: Appointment, facts: Facts):
    return [f"Terminnummer {t.number} ist kleiner als 1"] if t.number is not None and t.number < 1 else []


@rule("terminnummer-doppelt", KRITISCH, "Terminnummer im Kurs doppelt", stages=ALL, blocking=True)
def duplicate_number(t: Appointment, facts: Facts):
    """Two Termine of a course with the same number would both count (WM-111): this happens only
    when a Termin is dated before one that is already counted, whose number is frozen. A course of
    a continuous format counts every Termin anyway (WM-114), so a shared number does no harm."""
    if t.number is None or t.course is None or (t.course.format is not None and t.course.format.continuous):
        return []
    return [
        Finding(
            f"Terminnummer {t.number} hat auch der Termin am {day:%d.%m.%Y}"
            if day
            else f"Terminnummer {t.number} hat auch Termin {other}",
            other,
        )
        for number, other, day in facts.numbers.get(t.course.id, [])
        if number == t.number and other != t.id
    ]


@rule("klasse-unlesbar", KRITISCH, "Klasse/Alter nicht lesbar", stages=ALL, blocking=True)
def class_age_unreadable(t: Appointment, facts: Facts):
    course = t.course
    text = clean_name(course.class_age_text)
    if text and not parse_class_age(text).parsed and course.class_level is None and course.age is None:
        return [f"Klasse/Alter „{text}“ ist weder Klassenstufe, Alter, Altersgruppe noch Lehrkräfte"]
    return []


@rule("alter-erwachsen", KRITISCH, "Durchschnittsalter 19 oder mehr", stages=ALL, blocking=True)
def adult_average_age(t: Appointment, facts: Facts):
    course = t.course
    if course.age is not None and course.age >= ADULT_MIN_AGE and course.class_level is None:
        return [f"Durchschnittsalter (Kinder) {course.age} Jahre: für Erwachsene eine Altersgruppe wählen"]
    return []


@rule("herkunft-summe", KRITISCH, "Herkunft passt nicht zu den Teilnehmenden", stages=HELD, blocking=True)
def origins_sum(t: Appointment, facts: Facts):
    if not t.origins:
        return []
    total, n = sum(o.participants for o in t.origins), participants(t) or 0
    return [f"Herkunft ergibt {total}, eingetragen sind {n} Teilnehmende"] if total != n else []


@rule("datum-zukunft", KRITISCH, "Durchgeführt mit Datum in der Zukunft", stages=HELD, blocking=True)
def held_in_future(t: Appointment, facts: Facts):
    if t.date and t.date > facts.today:
        return [f"{t.stage}, aber das Datum {t.date:%d.%m.%Y} liegt in der Zukunft"]
    return []


# --- Warnings on values ---------------------------------------------------------------------


@rule("schule-mit-alter", INFO, "Schule, aber Alter statt Klasse", stages=ALL)
def school_with_age(t: Appointment, facts: Facts):
    course = t.course
    teachers = clean_name(course.class_age_text) == TEACHERS
    if (
        course.place
        and course.place.is_school
        and course.class_level is None
        and not teachers
        and (course.age is not None or course.age_band)
    ):
        return [f"„{course.place.name}“ ist eine Schule: Schulklasse oder Lehrkräfte wählen"]
    return []


@rule("viele-teilnehmende", INFO, "Ungewöhnlich viele Teilnehmende", stages=HELD)
def many_participants(t: Appointment, facts: Facts):
    n = participants(t)
    return [f"{n} Teilnehmende: bitte prüfen"] if n is not None and n > MANY_PARTICIPANTS else []


# --- Planning (PLANUNG_REGELN.md, "Hinweise an Kursen") -------------------------------------


@rule("geplant-vorbei", KRITISCH, "Geplant, Datum vorbei", stages=(GEPLANT,))
def planned_in_past(t: Appointment, facts: Facts):
    if t.date and t.date < facts.today:
        return ["Datum vorbei, nie bestätigt: durchgeführt oder löschen?"]
    return []


def _in_planning(t: Appointment, today: dt.date) -> bool:
    """Conflicts matter from today on, and for a held Termin only on its own day."""
    if t.date is None or t.date < today:
        return False
    return t.stage in PLANNED or (t.stage == DURCHGEFUEHRT and t.date == today)


def _overlap(a: Appointment, b: Appointment) -> bool | None:
    """True/False if both have start and end, None if a time is missing."""
    if not (a.start_time and a.end_time and b.start_time and b.end_time):
        return None
    return a.start_time < b.end_time and b.start_time < a.end_time


def _conflicts(t: Appointment, facts: Facts) -> Iterable[tuple[str, Finding]]:
    """(rule ID, finding) for every other Termin in planning on the same day."""
    if not _in_planning(t, facts.today):
        return
    team = _team(t)
    for other in facts.same_day.get(t.date, []):
        if other.id == t.id or not _in_planning(other, facts.today):
            continue
        shared = ", ".join(sorted(name for pid, name in _team(other).items() if pid in team))
        overlap = _overlap(t, other)
        title = f"„{_title(other)}“ (Termin {other.id})"
        if shared and overlap:
            yield "person-doppelt", Finding(f"{shared} gleichzeitig eingeplant mit {title}", other.id)
        elif shared and overlap is None:
            yield "person-uhrzeit", Finding(f"{shared} am selben Tag mit {title}, Uhrzeit fehlt", other.id)
        elif overlap and t.course.place_id and t.course.place_id == other.course.place_id:
            yield "gleichzeitig", Finding(f"Gleichzeitig am selben Ort mit {title}", other.id)
        elif overlap:
            yield "gleichzeitig", Finding(f"Zur selben Zeit wie {title}", other.id)
        elif shared:
            yield "selber-tag", Finding(f"{shared} am selben Tag mit {title}, ohne Überschneidung", other.id)
        else:
            yield "selber-tag", Finding(f"Anderer Kurs am selben Tag: {title}", other.id)


def _conflict_rule(rule_id: str):
    def check(t: Appointment, facts: Facts):
        return [finding for found, finding in _conflicts(t, facts) if found == rule_id]

    return check


rule("person-doppelt", KRITISCH, "Person gleichzeitig in zwei Kursen", stages=ACTIVE)(
    _conflict_rule("person-doppelt")
)
rule("person-uhrzeit", WICHTIG, "Person zweimal am Tag, Uhrzeit fehlt", stages=ACTIVE)(
    _conflict_rule("person-uhrzeit")
)
rule("gleichzeitig", WICHTIG, "Zeiten überschneiden sich", stages=ACTIVE)(_conflict_rule("gleichzeitig"))
rule("selber-tag", INFO, "Anderer Kurs am selben Tag", stages=ACTIVE)(_conflict_rule("selber-tag"))


@rule("alternativdatum", INFO, "Überschneidung an einem Alternativdatum", stages=(GEPLANT,))
def alternative_date_conflicts(t: Appointment, facts: Facts):
    """A Geplant Termin's alternative dates: who or what else is planned then (main dates only)."""
    found = []
    team = _team(t)
    for alternative in t.alternatives:
        day = alternative.date
        if day < facts.today:
            continue
        for other in facts.same_day.get(day, []):
            if other.id == t.id or not _in_planning(other, facts.today):
                continue
            shared = ", ".join(sorted(name for pid, name in _team(other).items() if pid in team))
            title = f"„{_title(other)}“ (Termin {other.id})"
            what = f"{shared} eingeplant bei {title}" if shared else f"anderer Kurs: {title}"
            found.append(Finding(f"Alternativdatum {day:%d.%m.%Y}: {what}", other.id))
    return found


# --- Places and list entries ----------------------------------------------------------------


@rule("ort-angaben", WICHTIG, "Ort ohne Adresse oder Schulangabe", stages=ACTIVE)
def place_details(t: Appointment, facts: Facts):
    place = t.course.place
    if place is None:
        return []
    if place.address is None:  # "" is a deliberate "no address" (online, private places)
        return [f"Ort „{place.name}“: Adresse fehlt (Straße, PLZ, Ort)"]
    if place.address and place.is_school is None:
        return [f"Ort „{place.name}“: Schule ja/nein fehlt (Zielgruppe unbekannt)"]
    return []


@rule("eintrag-wartet", INFO, "Listeneintrag nicht bestätigt", stages=ACTIVE)
def entries_waiting(t: Appointment, facts: Facts):
    found = []
    for pending in lists.pending_entries(t):
        text = pending.text
        if pending.entry.status == PROPOSED:
            # Names only: a format's category in its label would make every name look alike.
            name, known = pending.entry.name, facts.confirmed_names.get(pending.kind.slug, set())
            similar = suggestion(name, known, {}) if name and known else None
            if similar and similar != name:
                text += f", ähnlich: „{similar}“"
        found.append(text)
    return found


# --- Calendar and invitations (WM-110) -------------------------------------------------------


@rule("kalender", WICHTIG, "Kalendereintrag fehlt oder weicht ab", stages=PLANNED)
def calendar_event(t: Appointment, facts: Facts):
    """Only Bestätigt Termine are in the calendar (2026-11-13); a Geplant one with an outdated event
    went back to Geplant while the calendar could not be written: the event is still to be
    cancelled. Other Geplant Termine linked to an event keep it untouched until confirmed."""
    if t.stage == GEPLANT:
        if t.calendar_uid and t.calendar_state == CAL_OUTDATED:
            return ["Kalendereintrag noch nicht abgesagt (Termin wieder geplant)"]
        return []
    if t.calendar_uid and t.calendar_state == CAL_MISSING:
        return ["Kalendereintrag fehlt (war eingetragen)"]
    if t.calendar_uid and t.calendar_state == CAL_OUTDATED:
        return ["Kalendereintrag weicht vom Termin ab: „Kalender aktualisieren“"]
    return []


@rule("nicht-im-kalender", INFO, "Bestätigt, aber nicht im Kalender", stages=(BESTAETIGT,))
def not_in_calendar(t: Appointment, facts: Facts):
    return ["Bestätigt, aber noch nicht im Kalender"] if facts.calendar and not t.calendar_uid else []


@rule("einladung-abgelehnt", WICHTIG, "Einladung abgelehnt", stages=PLANNED)
def invitation_declined(t: Appointment, facts: Facts):
    """A person of the team declined the calendar invitation (PARTSTAT=DECLINED, read by the check)."""
    team = {p.id: p.name for p in ([t.lead] if t.lead else []) + list(t.support)}
    emails = {
        (p.email or "").lower(): p.name for p in ([t.lead] if t.lead else []) + list(t.support) if p.email
    }
    found = []
    for r in t.calendar_responses:
        if r.partstat != "DECLINED":
            continue
        name = team.get(r.person_id) or emails.get(r.email.lower())
        if name:  # still in the team
            found.append(f"{name} hat die Kalendereinladung abgelehnt")
    return found


# --- Likely duplicates ----------------------------------------------------------------------


def _same_record(a: Appointment, b: Appointment) -> bool:
    ca, cb = a.course, b.course
    same_course = (ca.format_id, ca.context_id, ca.place_id) == (cb.format_id, cb.context_id, cb.place_id)
    same_class = (ca.class_level, ca.age, ca.age_band, clean_name(ca.class_age_text)) == (
        cb.class_level,
        cb.age,
        cb.age_band,
        clean_name(cb.class_age_text),
    )
    na, nb = participants(a), participants(b)
    return (
        same_course
        and same_class
        and a.start_time == b.start_time
        and a.lead_id == b.lead_id
        and (na is None or nb is None or na == nb)
    )


@rule("doppelt", WICHTIG, "Möglicherweise doppelt erfasst", stages=ALL)
def likely_duplicate(t: Appointment, facts: Facts):
    if t.date is None:
        return []
    found = []
    for other in facts.same_day.get(t.date, []):
        if other.id == t.id or (t.source in REVIEWED and other.source in REVIEWED):
            continue
        if _same_record(t, other):
            found.append(Finding(f"Gleiche Angaben wie Termin {other.id} am selben Tag", other.id))
    return found


# --- Running the rules ----------------------------------------------------------------------


def evaluate(t: Appointment, facts: Facts) -> list[Found]:
    """Every rule that applies to the Termin's stage, with the levels of PLANUNG_REGELN.md."""
    if t.stage == ABGESAGT:
        return []
    locked = t.stage == ABGESCHLOSSEN
    found = []
    for r in RULES.values():
        if t.stage not in r.stages:
            continue
        for item in r.check(t, facts):
            finding = Finding(item) if isinstance(item, str) else item
            found.append(
                Found(
                    t.id,
                    r.id,
                    INFO if locked else r.level,
                    r.blocking and not locked,
                    finding.text,
                    finding.other_id,
                )
            )
    return found


def _load(session: Session, where) -> list[Appointment]:
    stmt = (
        select(Appointment)
        .where(where)
        .options(
            selectinload(Appointment.course).selectinload(Course.format),
            selectinload(Appointment.course).selectinload(Course.context),
            selectinload(Appointment.course).selectinload(Course.place),
            selectinload(Appointment.lead),
            selectinload(Appointment.support),
            selectinload(Appointment.origins),
            selectinload(Appointment.alternatives),
            selectinload(Appointment.calendar_responses),
        )
        .order_by(Appointment.id)
    )
    return list(session.scalars(stmt))


def _confirmed_names(session: Session) -> dict[str, set[str]]:
    return {
        kind.slug: set(
            session.scalars(
                select(kind.model.name).where(kind.model.status == CONFIRMED, kind.model.name.is_not(None))
            )
        )
        for kind in lists.KINDS.values()
    }


def _calendar_on() -> bool:
    return bool(get_settings().caldav_url)


def facts_for(session: Session, appointments: list[Appointment], day: dt.date) -> Facts:
    """The facts the rules need for these Termine: their same-day neighbours and the list names."""
    dates = {t.date for t in appointments if t.date} | {a.date for t in appointments for a in t.alternatives}
    same_day: dict[dt.date, list[Appointment]] = defaultdict(list)
    if dates:
        for t in _load(session, Appointment.date.in_(dates) & (Appointment.stage != ABGESAGT)):
            same_day[t.date].append(t)
    numbers: dict[int, list[tuple[int, int, dt.date | None]]] = defaultdict(list)
    course_ids = {t.course_id for t in appointments}
    if course_ids:
        rows = session.execute(
            select(Appointment.course_id, Appointment.number, Appointment.id, Appointment.date).where(
                Appointment.course_id.in_(course_ids),
                Appointment.number.is_not(None),
                Appointment.stage != ABGESAGT,
            )
        )
        for course_id, number, appointment_id, date in rows:
            numbers[course_id].append((number, appointment_id, date))
    return Facts(day, dict(same_day), _confirmed_names(session), _calendar_on(), dict(numbers))


def compute(session: Session, appointments: list[Appointment], day: dt.date | None = None) -> list[Found]:
    """The hints of these Termine, computed now (nothing is stored)."""
    facts = facts_for(session, appointments, day or today())
    return [f for t in appointments for f in evaluate(t, facts)]


def _store(session: Session, found: list[Found], ids: set[int]) -> None:
    session.execute(delete(StoredHint).where(StoredHint.appointment_id.in_(ids)))
    now = dt.datetime.now(dt.UTC)
    session.add_all(
        StoredHint(
            appointment_id=f.appointment_id,
            rule=f.rule,
            level=f.level,
            blocking=f.blocking,
            text=f.text,
            other_appointment_id=f.other_id,
            key=f.key,
            computed_at=now,
        )
        for f in found
    )
    session.flush()


def refresh(session: Session, appointment_ids: Iterable[int], day: dt.date | None = None) -> int:
    """Recompute and store the hints of these Termine after a change (WM-110 calls it after saving).

    Also recomputes their same-day neighbours and every Termin whose stored hints name one of them
    (a conflict on the old date after a move). Returns the number of Termine recomputed.
    """
    ids = set(appointment_ids)
    if not ids:
        return 0
    session.flush()
    linked = session.scalars(
        select(StoredHint.appointment_id).where(StoredHint.other_appointment_id.in_(ids))
    )
    ids |= set(linked)
    dates = set(
        session.scalars(
            select(Appointment.date).where(Appointment.id.in_(ids), Appointment.date.is_not(None))
        )
    )
    if dates:
        ids |= set(session.scalars(select(Appointment.id).where(Appointment.date.in_(dates))))
        # Geplant Termine with one of these days as an alternative date (rule "alternativdatum").
        ids |= set(
            session.scalars(select(AlternativeDate.appointment_id).where(AlternativeDate.date.in_(dates)))
        )
    appointments = _load(session, Appointment.id.in_(ids))
    _store(session, compute(session, appointments, day), ids)
    return len(ids)


def refresh_all(session: Session, day: dt.date | None = None) -> list[Found]:
    """Recompute and store the hints of every Termin (the nightly job and `metrics hints`)."""
    appointments = _load(session, Appointment.id.is_not(None))
    facts = Facts(day or today(), defaultdict(list), _confirmed_names(session), _calendar_on())
    for t in appointments:
        if t.date and t.stage != ABGESAGT:
            facts.same_day[t.date].append(t)
    found = [f for t in appointments for f in evaluate(t, facts)]
    session.execute(delete(StoredHint))
    _store(session, found, set())
    return found


def appointments_using(session: Session, kind: lists.Kind, entry_ids: Iterable[int]) -> set[int]:
    """The Termine whose course or team uses one of these list entries (to refresh after a decision)."""
    ids = list(entry_ids)
    if kind is lists.PEOPLE:
        lead = select(Appointment.id).where(Appointment.lead_id.in_(ids))
        support = select(appointment_support.c.appointment_id).where(appointment_support.c.person_id.in_(ids))
        return set(session.scalars(lead)) | set(session.scalars(support))
    column = {
        lists.PLACES: Course.place_id,
        lists.CONTEXTS: Course.context_id,
        lists.FORMATS: Course.format_id,
    }[kind]
    return set(session.scalars(select(Appointment.id).join(Course).where(column.in_(ids))))


# --- Decisions and the final confirmation ---------------------------------------------------


class HintError(ValueError):
    """A request the rules do not allow; the message is shown to the person (German)."""


def _accepted(session: Session, appointment_id: int, rule_id: str, key: str | None) -> HintDecision | None:
    """The decision that accepted this finding (or, for decisions taken before findings had keys,
    the rule on this Termin)."""
    return session.scalars(
        select(HintDecision)
        .where(
            HintDecision.appointment_id == appointment_id,
            HintDecision.rule == rule_id,
            HintDecision.decision == ACCEPTED,
            HintDecision.finding_key.is_(None) | (HintDecision.finding_key == key),
        )
        .order_by(HintDecision.id.desc())
    ).first()


def is_accepted(session: Session, f: "Found | StoredHint") -> bool:
    """Whether a non-blocking finding is accepted (blocking ones never are)."""
    if f.blocking:
        return False
    return _accepted(session, f.appointment_id, f.rule, f.key) is not None


def accept(
    session: Session, appointment_id: int, rule_id: str, reason: str, by: str, key: str | None = None
) -> HintDecision:
    """Accept one warning on a Termin with a reason; `key` names the finding when the rule found
    more than one. Blocking hints cannot be accepted. Team members and admins may accept."""
    stored = list(
        session.scalars(
            select(StoredHint).where(StoredHint.appointment_id == appointment_id, StoredHint.rule == rule_id)
        )
    )
    if key is not None:
        stored = [h for h in stored if h.key == key]
    if not stored:
        raise HintError("Diesen Hinweis gibt es nicht (mehr).")
    if any(h.blocking for h in stored):
        raise HintError("Ein blockierender Hinweis kann nicht akzeptiert werden; die Angaben müssen stimmen.")
    if len({h.key for h in stored}) > 1:
        raise HintError("Bitte den Hinweis einzeln akzeptieren.")
    if not clean_name(reason):
        raise HintError("Bitte einen Grund angeben.")
    hint = stored[0]
    if _accepted(session, appointment_id, rule_id, hint.key):
        raise HintError("Schon akzeptiert.")
    decision = HintDecision(
        rule=rule_id,
        appointment_id=appointment_id,
        finding_key=hint.key,
        decision=ACCEPTED,
        reason=clean_name(reason),
        decided_by=by,
    )
    session.add(decision)
    session.flush()
    return decision


def open_blocking(session: Session, appointment: Appointment, day: dt.date | None = None) -> list[Found]:
    """The blocking hints of a Termin, computed now (for `courses.finally_confirm`)."""
    return [f for f in compute(session, [appointment], day) if f.blocking]


# --- Reading the stored hints ---------------------------------------------------------------


def _is_accepted():
    """SQL: the stored hint has a decision that accepted its finding (see `finding_key`)."""
    return (
        select(HintDecision.id)
        .where(
            HintDecision.appointment_id == StoredHint.appointment_id,
            HintDecision.rule == StoredHint.rule,
            HintDecision.decision == ACCEPTED,
            HintDecision.finding_key.is_(None) | (HintDecision.finding_key == StoredHint.key),
        )
        .exists()
    )


def sidebar_counts(session: Session) -> dict[str, int]:
    """Open (not accepted) Kritisch and Wichtig hints, for the "Prüfen" entry."""
    rows = session.execute(
        select(StoredHint.level, func.count())
        .where(~_is_accepted() | StoredHint.blocking)
        .group_by(StoredHint.level)
    ).all()
    by_level = dict(rows)
    return {"hints_high": by_level.get(KRITISCH, 0), "hints_medium": by_level.get(WICHTIG, 0)}


OPEN, ACCEPTED_ONLY, EVERY = "offen", "akzeptiert", "alle"


@dataclass(frozen=True)
class QueueFilter:
    level: str | None = None
    rule: str | None = None
    context_id: int | None = None
    date_from: dt.date | None = None
    date_to: dt.date | None = None
    status: str = OPEN


@dataclass(frozen=True)
class QueueRow:
    hint: StoredHint
    appointment: Appointment
    decision: HintDecision | None


def queue(session: Session, f: QueueFilter, limit: int = 200) -> tuple[list[QueueRow], int]:
    """Stored hints for the "Prüfen" page, most severe and newest first; also the total found."""
    stmt = select(StoredHint).join(Appointment, Appointment.id == StoredHint.appointment_id)
    if f.level:
        stmt = stmt.where(StoredHint.level == f.level)
    if f.rule:
        stmt = stmt.where(StoredHint.rule == f.rule)
    if f.context_id:
        stmt = stmt.join(Course, Course.id == Appointment.course_id).where(Course.context_id == f.context_id)
    if f.date_from:
        stmt = stmt.where(Appointment.date >= f.date_from)
    if f.date_to:
        stmt = stmt.where(Appointment.date <= f.date_to)
    accepted = _is_accepted() & ~StoredHint.blocking
    if f.status == OPEN:
        stmt = stmt.where(~accepted)
    elif f.status == ACCEPTED_ONLY:
        stmt = stmt.where(accepted)
    total = session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    order = [
        case({level: i for i, level in enumerate(LEVELS)}, value=StoredHint.level),
        Appointment.date.desc().nulls_last(),
        StoredHint.appointment_id,
        StoredHint.id,
    ]
    hints = list(session.scalars(stmt.order_by(*order).limit(limit)))
    ids = {h.appointment_id for h in hints}
    appointments = {t.id: t for t in _load(session, Appointment.id.in_(ids))} if ids else {}
    decisions = {}
    if ids:
        for d in session.scalars(
            select(HintDecision)
            .where(HintDecision.appointment_id.in_(ids), HintDecision.decision == ACCEPTED)
            .order_by(HintDecision.id)
        ):
            decisions[(d.appointment_id, d.rule, d.finding_key)] = d
    rows = [
        QueueRow(
            h,
            appointments[h.appointment_id],
            None
            if h.blocking
            else decisions.get((h.appointment_id, h.rule, h.key))
            or decisions.get((h.appointment_id, h.rule, None)),
        )
        for h in hints
    ]
    return rows, total


def counts_by_rule(session: Session) -> dict[str, tuple[int, int]]:
    """rule -> (open, accepted) stored hints, for the rule filter."""
    accepted = _is_accepted() & ~StoredHint.blocking
    rows = session.execute(
        select(StoredHint.rule, func.count().filter(~accepted), func.count().filter(accepted)).group_by(
            StoredHint.rule
        )
    ).all()
    return {rule_id: (n_open, n_accepted) for rule_id, n_open, n_accepted in rows}


def title(t: Appointment) -> str:
    """How a Termin is named in the queue: its course name, else its context."""
    return _title(t)
