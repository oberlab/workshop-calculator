"""Courses and Termine on the new model: the few steps WM-101 needs (WM-110 builds planning on them).

`create_course` and `add_appointment` write a course and its Termine with a Verlauf row each;
`finally_confirm` moves a held Termin to Abgeschlossen (what the charts count), but only once all
its list entries are confirmed (`lists.pending_entries`).
"""

import datetime as dt

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from metrics import lists
from metrics.db import (
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
    """Final confirmation refused; `pending` names what is missing."""

    def __init__(self, pending: list[lists.Pending]):
        self.pending = pending
        super().__init__("; ".join(p.text for p in pending))


def _log(
    session: Session, table: str, row_id: int, field: str | None, old, new, by: str, reason=None
) -> None:
    lists._log(session, table, row_id, field, old, new, by, reason)


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
    """A new Termin of the course, numbered after the existing ones."""
    count = session.scalar(select(func.count()).where(Appointment.course_id == course.id)) or 0
    appointment = Appointment(
        course=course,
        number=count + 1,
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
    return appointment


def finally_confirm(session: Session, appointment: Appointment, by: str) -> None:
    """Durchgeführt -> Abgeschlossen, once every list entry of the Termin is confirmed."""
    if appointment.stage != DURCHGEFUEHRT:
        raise ValueError(f"Termin {appointment.id} is {appointment.stage}, not {DURCHGEFUEHRT}")
    session.refresh(appointment)
    if pending := lists.pending_entries(appointment):
        raise NotReady(pending)
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
