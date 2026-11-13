"""Planning on the new model (WM-110): saving a course and its Termin with Verlauf, stage changes,
availability and what is still missing. Ported from the prototype's `service.py` (WM-90).

The stage lives on the Termin (`Appointment.stage`). The shared fields (format, context, place,
class/age, note) are on the course; date, times, alternatives, team, participants, Herkunft and
`billed` on the Termin. A course has one Termin for now; series (WM-111) add more, and every
function here takes the Termin it acts on, so they work per Termin of a series as well.

Rules (decisions of WM-90, `PLANUNG_REGELN.md`):
- Geplant enforces nothing (a day without times is fine) and allows alternative dates;
  "Bestätigen" asks which date it is.
- Bestätigt requires date, start, end, context, class/age and place, also on every later save.
- Teilnehmende and Herkunft can be entered in every stage (Alex, 2026-11-13, replacing the WM-90
  rule "only from Durchgeführt"); they are required for the final confirmation only, and no hint
  looks at them before Durchgeführt. Abrechnung from Durchgeführt on.
- "Final bestätigen" (`finalize`) goes through `courses.finally_confirm` (confirmed list entries,
  no blocking hint); open warnings must be ticked as checked, which the Verlauf records.
- Abgeschlossen is locked except `billed`; a course's shared fields are locked as soon as one of
  its Termine is Abgeschlossen (they are counted in the charts).
- Only Bestätigt Termine can be cancelled (with a reason); only Geplant ones deleted, which the
  Verlauf keeps (the summary of the deleted Termin). Nothing else is ever deleted.

Every change is a Verlauf row (`history`, tables `courses` and `appointments`) and refreshes the
stored hints (`hints.refresh`). The calendar is updated by the web layer after a save
(`metrics.planning.caldav`), so a calendar that is down never loses a change.
"""

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from metrics import courses, hints, lists
from metrics.db import (
    ABGESAGT,
    ABGESCHLOSSEN,
    ACTIVE_STATUSES,
    BESTAETIGT,
    DURCHGEFUEHRT,
    GEPLANT,
    AlternativeDate,
    Appointment,
    AppointmentOrigin,
    Context,
    Course,
    Format,
    HistoryEntry,
    Person,
    Place,
    StoredHint,
)
from metrics.ingest.records import TEACHERS, parse_class_age
from metrics.instance import clean_name

SYSTEM = "System (automatisch)"
CLASS_KINDS = {
    "klasse": "Schulklasse",
    "lehrkraefte": "Lehrkräfte",
    "alter": "Durchschnittsalter (Kinder)",
    "band": "Altersgruppe (Erwachsene)",
}
AT_SCHOOL = ("klasse", "lehrkraefte")  # the only kinds offered when the place is a school
AGE_BANDS = ["19 - 24 Jahre", "25 - 34 Jahre", "35 - 49 Jahre", "50 - 64 Jahre", "über 65 Jahre"]
OPEN_STAGES = (GEPLANT, BESTAETIGT, DURCHGEFUEHRT)  # "Alle offenen"

# What each stage of a Termin may change: everything from Geplant on, participants and Herkunft too
# (Alex, 2026-11-13: the numbers often come from the teachers in advance; they are required only for
# the final confirmation); Abrechnung from Durchgeführt; Abgeschlossen only `billed`.
COURSE = "course"  # format, context, place, class/age, note
TIMES = "times"  # date, start, end
ALTERNATIVES = "alternatives"
TEAM = "team"
HELD = "held"  # participants, Herkunft
BILLED = "billed"
EDITABLE = {
    GEPLANT: {COURSE, TIMES, ALTERNATIVES, TEAM, HELD},
    BESTAETIGT: {COURSE, TIMES, TEAM, HELD},
    DURCHGEFUEHRT: {COURSE, TIMES, TEAM, HELD, BILLED},
    ABGESCHLOSSEN: {BILLED},
    ABGESAGT: set(),
}

# German names of the fields in the Verlauf.
LABELS = {
    "format": "Format",
    "context": "Kontext",
    "place": "Ort",
    "class_age_text": "Klasse/Alter",
    "note": "Notiz",
    "target_group": "Zielgruppe",
    "date": "Datum",
    "start_time": "Beginn",
    "end_time": "Ende",
    "duration_hours": "Dauer",
    "alternatives": "Alternativdaten",
    "lead": "Kursleitung",
    "support": "Unterstützung",
    "participants_female": "TN weiblich",
    "participants_male": "TN männlich",
    "participants_diverse": "TN divers",
    "participants_unspecified": "TN keine Angabe",
    "origins": "Herkunft",
    "billed": "Abgerechnet",
    "stage": "Phase",
    "cancel_reason": "Absagegrund",
    "calendar_uid": "Kalender",
    "status": "Status",
}
PARTICIPANT_FIELDS = (
    ("participants_female", "weiblich"),
    ("participants_male", "männlich"),
    ("participants_diverse", "divers"),
    ("participants_unspecified", "keine Angabe"),
)


class FormError(Exception):
    """Input or a step the rules do not allow; the messages are shown to the person (German)."""

    def __init__(self, messages: list[str] | str):
        self.messages = [messages] if isinstance(messages, str) else list(messages)
        super().__init__("; ".join(self.messages))


# --- Reading -------------------------------------------------------------------------------------


def title(t: Appointment) -> str:
    return hints.title(t)


def class_kind(course: Course) -> str | None:
    """Which Klasse/Alter kind the stored values are (for the form)."""
    if clean_name(course.class_age_text) == TEACHERS:
        return "lehrkraefte"
    if course.class_level is not None:
        return "klasse"
    if course.age is not None:
        return "alter"
    if course.age_band:
        return "band"
    return None


def course_locked(course: Course) -> bool:
    """The shared fields of a course are locked once one of its Termine is counted."""
    return any(t.stage == ABGESCHLOSSEN for t in course.appointments)


def editable(t: Appointment) -> set[str]:
    allowed = set(EDITABLE[t.stage])
    if course_locked(t.course):
        allowed.discard(COURSE)
    return allowed


def participants_total(t: Appointment) -> int | None:
    counts = [getattr(t, name) for name, _ in PARTICIPANT_FIELDS]
    return sum(c or 0 for c in counts) if any(c is not None for c in counts) else None


def asks_municipalities(t: Appointment) -> bool:
    return bool(t.course.context and t.course.context.asks_municipalities)


# --- Snapshots for the Verlauf -------------------------------------------------------------------


def _show(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "ja" if value else "nein"
    if isinstance(value, dt.time):
        return value.strftime("%H:%M")
    if isinstance(value, dt.date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, Decimal):
        # 1.50 from the database and 1.5 computed are the same duration.
        return f"{value.normalize():f}".replace(".", ",")
    return str(value)


def _entry(entry) -> str:
    return lists.label(entry) if entry is not None else ""


def course_snapshot(course: Course) -> dict[str, str]:
    return {
        "format": _entry(course.format),
        "context": _entry(course.context),
        "place": _entry(course.place),
        "class_age_text": _show(course.class_age_text),
        "note": _show(course.note),
        "target_group": _show(course.target_group),
    }


def appointment_snapshot(t: Appointment) -> dict[str, str]:
    return {
        "date": _show(t.date),
        "start_time": _show(t.start_time),
        "end_time": _show(t.end_time),
        "duration_hours": _show(t.duration_hours),
        "alternatives": ", ".join(_show(a.date) for a in t.alternatives),
        "lead": _entry(t.lead),
        "support": ", ".join(sorted(p.name for p in t.support)),
        **{name: _show(getattr(t, name)) for name, _ in PARTICIPANT_FIELDS},
        "origins": "; ".join(f"{o.municipality} {o.participants}" for o in t.origins),
        "billed": _show(t.billed),
    }


def _record(session: Session, table: str, row_id: int, before: dict, after: dict, by: str) -> list[str]:
    changed = []
    for name, new in after.items():
        old = before.get(name, "")
        if old != new:
            lists._log(session, table, row_id, name, old or None, new or None, by)
            changed.append(name)
    return changed


def log(session: Session, t: Appointment, field: str | None, old, new, by: str, reason=None) -> None:
    lists._log(session, "appointments", t.id, field, old, new, by, reason)


# --- Parsing the form ----------------------------------------------------------------------------

Form = dict[str, list[str]]


def one(form: Form, name: str) -> str:
    return clean_name((form.get(name) or [""])[0])


def parse_date(value: str, label: str, errors: list) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        errors.append(f"{label}: kein gültiges Datum")
        return None


def _time(value: str, label: str, errors: list) -> dt.time | None:
    if not value:
        return None
    try:
        return dt.time.fromisoformat(value)
    except ValueError:
        errors.append(f"{label}: keine gültige Uhrzeit")
        return None


def _count(value: str, label: str, errors: list) -> int | None:
    if not value:
        return None
    try:
        number = int(value)
    except ValueError:
        errors.append(f"{label}: keine ganze Zahl")
        return None
    if number < 0:
        errors.append(f"{label}: darf nicht negativ sein")
        return None
    return number


def _pick(session: Session, model, value: str, label: str, errors: list, current=None):
    """A list entry chosen in a picker: an active one, or the one already set (whatever its status)."""
    if not value:
        return None
    if not value.isdigit():
        errors.append(f"{label}: ungültige Auswahl")
        return current
    entry = session.get(model, int(value))
    if entry is None or (entry is not current and entry.status not in ACTIVE_STATUSES):
        errors.append(f"{label}: nicht (mehr) auswählbar")
        return current
    return entry


def _class_age(form: Form, errors: list) -> str | None:
    kind = one(form, "class_kind")
    if kind == "klasse":
        level = _count(one(form, "class_level"), "Klassenstufe", errors)
        if level is not None and not 1 <= level <= 13:
            errors.append("Klassenstufe: zwischen 1 und 13")
        return f"Kl. {level}" if level else None
    if kind == "alter":
        age = _count(one(form, "child_age"), "Durchschnittsalter", errors)
        return f"{age} Jahre" if age else None
    if kind == "band":
        band = one(form, "age_band")
        return band if band in AGE_BANDS else None
    if kind == "lehrkraefte":
        return TEACHERS
    return None


def _apply_course(session: Session, course: Course, form: Form, errors: list) -> None:
    course.format = _pick(session, Format, one(form, "format_id"), "Format", errors, course.format)
    course.context = _pick(session, Context, one(form, "context_id"), "Kontext", errors, course.context)
    course.place = _pick(session, Place, one(form, "place_id"), "Ort", errors, course.place)
    if "class_kind" in form:
        text = _class_age(form, errors)
        parsed = parse_class_age(text)
        course.class_age_text = text
        course.class_level, course.age, course.age_band = parsed.class_level, parsed.age, parsed.age_band
    course.note = (form.get("note") or [""])[0].strip() or None
    course.target_group = lists.course_target_group(course)


def _duration(t: Appointment) -> Decimal | None:
    """Dauer is always end minus start: breaks are not recorded (decided 2026-11-11)."""
    if t.start_time and t.end_time:
        start = dt.datetime.combine(dt.date.min, t.start_time)
        hours = (dt.datetime.combine(dt.date.min, t.end_time) - start).total_seconds() / 3600
        return Decimal(str(round(hours, 2))) if hours > 0 else None
    # The Slack Lists recorded a duration without times: keep it until times are entered.
    return t.duration_hours if t.source in hints.WITHOUT_TIMES else None


def _apply_appointment(session: Session, t: Appointment, form: Form, allowed: set, errors: list) -> None:
    if TIMES in allowed:
        t.date = parse_date(one(form, "date"), "Datum", errors)
        t.start_time = _time(one(form, "start_time"), "Beginn", errors)
        t.end_time = _time(one(form, "end_time"), "Ende", errors)
        t.duration_hours = _duration(t)
    if ALTERNATIVES in allowed:
        dates = {
            d for v in form.get("alt_date", []) if (d := parse_date(clean_name(v), "Alternativdatum", errors))
        }
        dates.discard(t.date)
        t.alternatives = [AlternativeDate(date=d) for d in sorted(dates)]
    if TEAM in allowed:
        lead = _pick(session, Person, one(form, "lead"), "Kursleitung", errors, t.lead)
        t.lead = lead
        support: list[Person] = []
        current = {str(p.id): p for p in t.support}
        for value in dict.fromkeys(clean_name(v) for v in form.get("support", [])):
            person = _pick(session, Person, value, "Unterstützung", errors, current.get(value))
            if person is not None and person is not lead and person not in support:
                support.append(person)
        t.support = support
    if HELD in allowed:
        for name, label in PARTICIPANT_FIELDS:
            setattr(t, name, _count(one(form, name), f"TN {label}", errors))
        t.participants_total = participants_total(t)
        if "origin_name" in form:
            origins: dict[str, int] = {}
            for name, value in zip(form.get("origin_name", []), form.get("origin_count", []), strict=False):
                name = clean_name(name)
                n = _count(clean_name(value), f"Herkunft {name}", errors)
                if name and n:
                    origins[name] = origins.get(name, 0) + n
            existing = {o.municipality: o for o in t.origins}
            for m, n in origins.items():
                if m in existing:
                    existing[m].participants = n
            t.origins = [
                existing.get(m) or AppointmentOrigin(municipality=m, participants=n)
                for m, n in sorted(origins.items())
            ]
    if BILLED in allowed:
        t.billed = "billed" in form


def _required_for_stage(t: Appointment) -> None:
    if t.stage in (BESTAETIGT, DURCHGEFUEHRT) and (gaps := hints.missing(t, hints.CONFIRM_FIELDS)):
        raise FormError(f"Ab „Bestätigt“ sind Pflicht: {', '.join(gaps)}")


# --- Creating and saving -------------------------------------------------------------------------


def create(session: Session, form: Form, by: str) -> Appointment:
    """A new course with one Termin in Geplant, from the course form."""
    errors: list[str] = []
    course = courses.create_course(session, by=by)
    _apply_course(session, course, form, errors)
    t = courses.add_appointment(session, course, by=by, date=None, stage=GEPLANT)
    _apply_appointment(session, t, form, EDITABLE[GEPLANT], errors)
    if errors:
        raise FormError(errors)
    session.flush()
    _record(session, "courses", course.id, {}, course_snapshot(course), by)
    _record(session, "appointments", t.id, {}, appointment_snapshot(t), by)
    hints.refresh(session, [t.id])
    return t


def save(session: Session, t: Appointment, form: Form, by: str) -> list[str]:
    """Copy what the Termin's stage allows from the form; returns the names of the changed fields."""
    allowed = editable(t)
    if not allowed:
        raise FormError(f"Ein Termin in „{t.stage}“ kann nicht mehr geändert werden.")
    course = t.course
    before_course, before = course_snapshot(course), appointment_snapshot(t)
    errors: list[str] = []
    if COURSE in allowed:
        _apply_course(session, course, form, errors)
    _apply_appointment(session, t, form, allowed, errors)
    if errors:
        raise FormError(errors)
    _required_for_stage(t)
    changed = _record(session, "courses", course.id, before_course, course_snapshot(course), by)
    changed += _record(session, "appointments", t.id, before, appointment_snapshot(t), by)
    hints.refresh(session, [a.id for a in course.appointments])
    return changed


def set_billed(session: Session, t: Appointment, billed: bool, by: str) -> None:
    """Abgerechnet: the one field that stays editable when a Termin is Abgeschlossen."""
    if BILLED not in EDITABLE[t.stage]:
        raise FormError("Abgerechnet gibt es erst ab „Durchgeführt“.")
    if t.billed != billed:
        log(session, t, "billed", _show(t.billed), _show(billed), by)
        t.billed = billed


# --- Stage changes -------------------------------------------------------------------------------


def _move(session: Session, t: Appointment, stage: str, by: str, reason: str | None = None) -> None:
    log(session, t, "stage", t.stage, stage, by, reason)
    t.stage = stage
    session.flush()
    hints.refresh(session, [t.id])


def confirm(session: Session, t: Appointment, chosen: dt.date | None, by: str) -> None:
    """Geplant -> Bestätigt; with alternative dates, `chosen` says which date it is."""
    if t.stage != GEPLANT:
        raise FormError("Nur geplante Termine können bestätigt werden.")
    chosen = chosen or t.date
    if t.alternatives:
        options = {t.date, *(a.date for a in t.alternatives)} - {None}
        if chosen not in options:
            raise FormError("Bitte das Datum wählen, an dem der Termin stattfindet.")
        dropped = ", ".join(_show(d) for d in sorted(options - {chosen}))
        if chosen != t.date:
            log(session, t, "date", _show(t.date), _show(chosen), by)
            t.date = chosen
        log(session, t, "alternatives", dropped, None, by, "Alternativdaten verworfen")
        t.alternatives = []
    if gaps := hints.missing(t, hints.CONFIRM_FIELDS):
        raise FormError(f"Zum Bestätigen fehlt: {', '.join(gaps)}")
    _move(session, t, BESTAETIGT, by)


def back_to_planned(session: Session, t: Appointment, by: str) -> None:
    if t.stage != BESTAETIGT:
        raise FormError("Nur bestätigte Termine können zurück auf „Geplant“.")
    _move(session, t, GEPLANT, by)


def mark_done(session: Session, t: Appointment, by: str) -> None:
    if t.stage not in (GEPLANT, BESTAETIGT):
        raise FormError("Dieser Termin kann nicht als durchgeführt markiert werden.")
    _move(session, t, DURCHGEFUEHRT, by)


def back_to_confirmed(session: Session, t: Appointment, by: str, today: dt.date) -> None:
    if t.stage != DURCHGEFUEHRT or not t.date or t.date < today:
        raise FormError("Zurück auf „Bestätigt“ geht nur, solange das Datum nicht vorbei ist.")
    _move(session, t, BESTAETIGT, by)


def cancel(session: Session, t: Appointment, reason: str, by: str) -> None:
    """Bestätigt -> Abgesagt, with a reason; planned Termine are deleted instead."""
    if t.stage != BESTAETIGT:
        raise FormError("Nur bestätigte Termine können abgesagt werden; geplante werden gelöscht.")
    reason = clean_name(reason)
    if not reason:
        raise FormError("Bitte einen Grund für die Absage angeben.")
    t.cancel_reason = reason
    log(session, t, "cancel_reason", None, reason, by)
    _move(session, t, ABGESAGT, by, reason)


def summary(t: Appointment) -> str:
    """One line that describes a Termin, kept in the Verlauf when it is deleted."""
    course = t.course
    parts = [
        title(t),
        _show(t.date) or "ohne Datum",
        f"{_show(t.start_time)}–{_show(t.end_time)}" if t.start_time else "",
        course.place.name if course.place else "ohne Ort",
        f"Kontext {course.context.name}" if course.context else "",
        f"Leitung {t.lead.name}" if t.lead else "",
        f"Unterstützung {', '.join(p.name for p in t.support)}" if t.support else "",
        f"Kalender {t.calendar_uid}" if t.calendar_uid else "",
    ]
    return ", ".join(p for p in parts if p)


def delete(session: Session, t: Appointment, by: str, really: bool) -> int:
    """Delete a Geplant Termin (and its course when it was the last Termin), after an extra
    confirmation. The Verlauf keeps what it was. Returns the course ID."""
    if t.stage != GEPLANT:
        raise FormError("Nur geplante Termine können gelöscht werden.")
    if not really:
        raise FormError("Bitte das Löschen bestätigen.")
    course = t.course
    log(session, t, None, summary(t), None, by, "Termin gelöscht")
    others = [a.id for a in course.appointments if a.id != t.id]
    neighbours = (
        session.scalars(
            select(Appointment.id).where(Appointment.date == t.date, Appointment.id != t.id)
        ).all()
        if t.date
        else []
    )
    session.delete(t)
    session.flush()
    session.expire(course, ["appointments"])
    if not others:
        lists._log(session, "courses", course.id, None, title_of_course(course), None, by, "Kurs gelöscht")
        session.delete(course)
        session.flush()
    hints.refresh(session, [*others, *neighbours])
    return course.id


def title_of_course(course: Course) -> str:
    if course.format and course.format.name:
        return course.format.name
    return course.context.name if course.context else f"Kurs {course.id}"


def open_warnings(session: Session, t: Appointment, today: dt.date | None = None) -> list[hints.Found]:
    """The hints of a Termin that do not block but are not accepted either."""
    return [
        f for f in hints.compute(session, [t], today) if not f.blocking and not hints.is_accepted(session, f)
    ]


def finalize(session: Session, t: Appointment, by: str, checked: bool, today: dt.date | None = None) -> None:
    """Durchgeführt -> Abgeschlossen: what the charts count. Open warnings must be ticked as checked."""
    if t.stage != DURCHGEFUEHRT:
        raise FormError("Nur durchgeführte Termine können final bestätigt werden.")
    session.flush()
    pending = lists.pending_entries(t)
    blocking = hints.open_blocking(session, t, today)
    if pending or blocking:
        raise FormError([p.text for p in pending] + [h.text for h in blocking])
    warnings = open_warnings(session, t, today)
    if warnings and not checked:
        raise FormError("Bitte die Hinweise prüfen und „Hinweise geprüft“ ankreuzen.")
    if warnings:
        log(session, t, None, None, "; ".join(w.text for w in warnings), by, "Hinweise geprüft")
    try:
        courses.finally_confirm(session, t, by, today)
    except courses.NotReady as error:  # pragma: no cover - checked just above
        raise FormError([p.text for p in error.pending] + [h.text for h in error.blocking]) from error


# --- Availability --------------------------------------------------------------------------------


def busy_people(
    session: Session,
    day: dt.date,
    start: dt.time | None,
    end: dt.time | None,
    exclude: int | None = None,
) -> dict[int, str]:
    """person ID -> the Termin that keeps them busy at that time (for the team pickers).

    Termine in planning on that day (Geplant, Bestätigt, Durchgeführt); with times on both sides only
    the overlapping ones count, without a time every Termin of the day does.
    """
    busy: dict[int, str] = {}
    stmt = (
        select(Appointment)
        .where(Appointment.date == day, Appointment.stage.in_(OPEN_STAGES))
        .order_by(Appointment.start_time.nulls_last(), Appointment.id)
    )
    for other in session.scalars(stmt):
        if other.id == exclude:
            continue
        if start and end and other.start_time and other.end_time:
            if not (start < other.end_time and other.start_time < end):
                continue
        label = title(other)
        if other.start_time:
            label += f" ({other.start_time:%H:%M}" + (f"–{other.end_time:%H:%M})" if other.end_time else ")")
        for person in ([other.lead] if other.lead else []) + list(other.support):
            busy.setdefault(person.id, label)
    return busy


# --- What is still missing -----------------------------------------------------------------------


@dataclass(frozen=True)
class Check:
    """One line of "Was fehlt noch": done or not, and how severe an open item is."""

    done: bool
    text: str
    level: str | None = None  # hint level of an open item (Kritisch, Wichtig, Info)
    href: str | None = None


def what_is_missing(t: Appointment, stored: list, accepted: set[int]) -> list[Check]:
    """The checklist of a Termin: required fields of its next step, list entries, open hints.
    `stored`: its stored hints; `accepted`: the IDs of those that are accepted."""
    if t.stage in (ABGESAGT, ABGESCHLOSSEN):
        return []
    checks = []
    absent = set(hints.missing(t, hints.FINAL_FIELDS))
    groups = [
        ("Datum, Beginn, Ende", ["Datum", "Beginn", "Ende"]),
        ("Kontext, Ort, Klasse/Alter", ["Kontext", "Ort", "Klasse/Alter"]),
        ("Format (Kursname und Kategorie)", ["Kursname", "Kurskategorie"]),
        ("Kursleitung", ["Kursleitung"]),
    ]
    if t.stage == DURCHGEFUEHRT:
        groups.append(("Teilnehmende (weiblich, männlich)", ["TN weiblich", "TN männlich"]))
        if asks_municipalities(t):
            groups.append(("Herkunft der Teilnehmenden", ["Herkunft"]))
    needed_now = set(hints.CONFIRM_FIELDS) if t.stage in (BESTAETIGT, DURCHGEFUEHRT) else set()
    for text, labels in groups:
        gaps = [label for label in labels if label in absent]
        level = (
            hints.KRITISCH
            if t.stage == DURCHGEFUEHRT
            else (hints.WICHTIG if needed_now & set(gaps) else None)
        )
        checks.append(
            Check(
                not gaps,
                text + (f": fehlt {', '.join(gaps)}" if gaps and len(labels) > 1 else ""),
                None if not gaps else level,
            )
        )
    for pending in lists.pending_entries(t):
        checks.append(
            Check(False, pending.text, hints.INFO, f"/stammdaten/{pending.kind.slug}?id={pending.entry.id}")
        )
    seen = {p.text for p in lists.pending_entries(t)}
    for h in stored:
        if h.rule in ("pflicht-bestaetigt", "pflicht-abschluss", "eintrag-wartet") or h.text in seen:
            continue  # already a line above
        if h.id in accepted and not h.blocking:
            continue
        href = None
        if h.other_appointment_id:
            href = f"/planung/termin/{h.other_appointment_id}"
        checks.append(Check(False, h.text, h.level, href))
    if t.stage == GEPLANT:
        checks.append(Check(False, "Noch nicht bestätigt", None))
    return checks


# --- Verlauf -------------------------------------------------------------------------------------


def history(session: Session, course: Course) -> list[HistoryEntry]:
    """The Verlauf of a course and its Termine, newest first."""
    ids = [t.id for t in course.appointments]
    stmt = select(HistoryEntry).where(
        ((HistoryEntry.table_name == "courses") & (HistoryEntry.row_id == course.id))
        | ((HistoryEntry.table_name == "appointments") & HistoryEntry.row_id.in_(ids or [-1]))
    )
    return list(session.scalars(stmt.order_by(HistoryEntry.changed_at.desc(), HistoryEntry.id.desc())))


def sidebar_counts(session: Session) -> dict[str, int]:
    """Termine in planning with an open Kritisch or Wichtig hint, for the "Kurse" entry."""
    rows = session.execute(
        select(StoredHint.appointment_id, func.min(StoredHint.level))
        .join(Appointment, Appointment.id == StoredHint.appointment_id)
        .where(Appointment.stage.in_(OPEN_STAGES), StoredHint.blocking | ~hints._is_accepted())
        .where(StoredHint.level.in_((hints.KRITISCH, hints.WICHTIG)))
        .group_by(StoredHint.appointment_id)
    ).all()
    # "Kritisch" < "Wichtig" alphabetically: the minimum is the most severe level.
    levels = [level for _, level in rows]
    return {"courses_high": levels.count(hints.KRITISCH), "courses_medium": levels.count(hints.WICHTIG)}
