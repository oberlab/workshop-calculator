"""Courses and Termine on the new model: the few steps WM-101 needs (WM-110 builds planning on them).

`create_course` and `add_appointment` write a course and its Termine with a Verlauf row each;
`renumber` computes the Terminnummer of a course's Termine (WM-111); `finally_confirm` moves a held
Termin to Abgeschlossen (what the charts count), but only once all its list entries are confirmed
(`lists.pending_entries`) and no blocking hint is open (`hints.open_blocking`). Both refresh the
stored hints of the Termin (`hints.refresh`).

Kept course values (WM-135): a course's format, context, place and class/age stay editable while it
has open Termine, and a change applies only to the Termine that are not Abgeschlossen or Abgesagt.
Before the course changes, those Termine keep the values they had (`keep_values`): they are copied
into the Termin's `frozen_*` columns, once, with a Verlauf row. A Termin becoming Abgeschlossen needs
nothing: it follows its course until the course changes. Every reader takes a Termin's effective
values (`Appointment.place` and so on, see `metrics.db`).
"""

import datetime as dt

from sqlalchemy.orm import Session

from metrics import hints, lists
from metrics.db import (
    ABGESAGT,
    ABGESCHLOSSEN,
    DURCHGEFUEHRT,
    GEPLANT,
    Appointment,
    Context,
    Course,
    Format,
    Person,
    Place,
)
from metrics.ingest.records import UNKNOWN, parse_class_age, to_text


class NotReady(Exception):
    """Final confirmation refused: `pending` names the unconfirmed list entries, `blocking` the
    blocking hints."""

    def __init__(self, pending: list[lists.Pending], blocking: list[hints.Found] = ()):
        self.pending = pending
        self.blocking = list(blocking)
        super().__init__("; ".join([p.text for p in pending] + [h.text for h in self.blocking]))


def _log(
    session: Session, table: str, row_id: int, field: str | None, old, new, by: str, reason=None
) -> None:
    lists._log(session, table, row_id, field, old, new, by, reason)


# --- Kept course values (WM-135) -------------------------------------------------------------------

# The course values a Termin keeps (`Appointment.frozen_<name>`), and the stages whose Termine keep
# them when their course changes: Abgeschlossen ones are counted, Abgesagt ones are history.
VALUES = ("format", "context", "place", "class_age_text", "class_level", "age", "age_band", "target_group")
KEEP_STAGES = (ABGESCHLOSSEN, ABGESAGT)
KEPT_FIELD = "kept_values"  # the Verlauf field of the row that records it
KEPT_REASON = "Kurs geändert: gilt nur für die offenen Termine; der Termin {day} behält seine Angaben"


def course_values(course: Course) -> dict:
    """The course's values a Termin can keep (list entries as objects)."""
    return {name: getattr(course, name) for name in VALUES}


def describe(values: dict) -> str:
    """Kept values (as `course_values`) in one line for the Verlauf."""
    parts = [
        ("Format", lists.label(values["format"]) if values["format"] is not None else ""),
        ("Kontext", lists.label(values["context"]) if values["context"] is not None else ""),
        ("Ort", lists.label(values["place"]) if values["place"] is not None else ""),
        ("Klasse/Alter", values["class_age_text"] or ""),
        ("Zielgruppe", values["target_group"] or ""),
    ]
    return "; ".join(f"{label} {value or 'leer'}" for label, value in parts)


def keep(session: Session, t: Appointment, values: dict, by: str, reason: str | None = None) -> bool:
    """The Termin keeps these course values from now on; False if it keeps its own already."""
    if t.frozen_at is not None:
        return False
    for name in VALUES:
        setattr(t, f"frozen_{name}", values[name])
    t.frozen_at = dt.datetime.now(dt.UTC)
    day = f"am {t.date:%d.%m.%Y}" if t.date else "ohne Datum"
    _log(
        session,
        "appointments",
        t.id,
        KEPT_FIELD,
        None,
        describe(values),
        by,
        reason or KEPT_REASON.format(day=day),
    )
    return True


def keep_values(session: Session, course: Course, before: dict, by: str) -> list[Appointment]:
    """After a change of the course (`before`: its `course_values` beforehand): its Abgeschlossen and
    Abgesagt Termine that still followed the course keep the values they had. Returns those Termine."""
    if course_values(course) == before:
        return []
    return [t for t in course.appointments if t.stage in KEEP_STAGES and keep(session, t, before, by)]


def has_open(course: Course) -> bool:
    """Whether the course has a Termin that is neither Abgeschlossen nor Abgesagt: its values can be
    changed then (on the page of such a Termin)."""
    return any(t.stage not in KEEP_STAGES for t in course.appointments)


def has_closed(course: Course) -> bool:
    """Whether some of the course's Termine are Abgeschlossen (a change leaves them as they are)."""
    return any(t.stage == ABGESCHLOSSEN for t in course.appointments)


def create_course(
    session: Session,
    *,
    by: str,
    format_id: int | None = None,
    context_id: int | None = None,
    place_id: int | None = None,
    class_age_text: str | None = None,
    note: str | None = None,
) -> Course:
    """A new course; its target group follows from class/age, context and place."""
    text = to_text(class_age_text)
    class_age = parse_class_age(text)
    course = Course(
        format=session.get(Format, format_id) if format_id else None,
        context=session.get(Context, context_id) if context_id else None,
        place=session.get(Place, place_id) if place_id else None,
        class_age_text=text,
        class_level=class_age.class_level,
        age=class_age.age,
        age_band=class_age.age_band,
        target_group=UNKNOWN,
        note=note,
    )
    course.target_group = lists.course_target_group(course)
    session.add(course)
    session.flush()
    _log(session, "courses", course.id, None, None, None, by, "Kurs angelegt")
    return course


def add_appointment(
    session: Session,
    course: Course,
    *,
    by: str,
    date: dt.date | None,
    start_time: dt.time | None = None,
    end_time: dt.time | None = None,
    lead_id: int | None = None,
    support_ids: tuple[int, ...] = (),
    stage: str = GEPLANT,
) -> Appointment:
    """A new Termin of the course; the Terminnummer follows from its date (`renumber`)."""
    appointment = Appointment(
        course=course,
        date=date,
        start_time=start_time,
        end_time=end_time,
        stage=stage,
        lead=session.get(Person, lead_id) if lead_id else None,
        support=[p for i in support_ids if (p := session.get(Person, i)) is not None],
    )
    session.add(appointment)
    session.flush()
    _log(session, "appointments", appointment.id, "stage", None, stage, by, "Termin angelegt")
    renumber(session, course, by)
    hints.refresh(session, [t.id for t in course.appointments])
    return appointment


# --- Terminnummer (WM-111) -------------------------------------------------------------------------


def in_order(appointments) -> list[Appointment]:
    """The Termine of a course by date and start time; Termine without a date come last."""
    return sorted(
        appointments,
        key=lambda t: (t.date is None, t.date or dt.date.max, t.start_time or dt.time.min, t.id or 0),
    )


def frozen(t: Appointment) -> bool:
    """A Termin whose Terminnummer never changes: counted ones (Abgeschlossen) and imported ones
    (legacy and Slack rows keep the recorded number)."""
    return t.stage == ABGESCHLOSSEN or t.source != "app"


def renumber(session: Session, course: Course, by: str) -> list[Appointment]:
    """Compute the Terminnummer of the course's Termine; returns those whose number changed.

    The number is the position among the course's Termine that are not cancelled, by date and start
    time (the mockup: "Die Nummer zählt die durchgeführten Termine"). A cancelled Termin has no
    number, so the first Termin that takes place is number 1 and its participants are the ones
    counted (`analytics.totals`; a continuous format counts every Termin). Frozen Termine
    (`frozen`) keep theirs; if a Termin is later dated before a counted one, both can end up with
    the same number, which the blocking hint "terminnummer-doppelt" catches before the second is
    counted (not for continuous formats). A changed number of an existing Termin is a Verlauf row
    ("neu berechnet").
    """
    session.flush()
    session.expire(course, ["appointments"])
    changed = []
    position = 0
    for t in in_order(course.appointments):
        if t.stage != ABGESAGT:
            position += 1
        if frozen(t):
            continue
        number = position if t.stage != ABGESAGT else None
        if t.number != number:
            if t.number is not None:
                _log(session, "appointments", t.id, "number", t.number, number, by, "neu berechnet")
            t.number = number
            changed.append(t)
    session.flush()
    return changed


def finally_confirm(
    session: Session, appointment: Appointment, by: str, today: dt.date | None = None
) -> None:
    """Durchgeführt -> Abgeschlossen, once every list entry of the Termin is confirmed and no
    blocking hint is open."""
    if appointment.stage != DURCHGEFUEHRT:
        raise ValueError(f"Termin {appointment.id} is {appointment.stage}, not {DURCHGEFUEHRT}")
    session.flush()
    session.refresh(appointment)
    pending = lists.pending_entries(appointment)
    blocking = hints.open_blocking(session, appointment, today)
    if pending or blocking:
        raise NotReady(pending, blocking)
    _log(
        session,
        "appointments",
        appointment.id,
        "stage",
        appointment.stage,
        ABGESCHLOSSEN,
        by,
        "final bestätigt",
    )
    appointment.stage = ABGESCHLOSSEN
    session.flush()
    hints.refresh(session, [appointment.id], today)
