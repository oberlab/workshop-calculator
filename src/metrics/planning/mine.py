"""Mein Bereich (WM-112): what one team member has to do, their next Termine and their workload.

"Mine" is always the Person linked to the login (`people.login_subject`, WM-105); a login without a
linked Person has no Mein Bereich. Who counts:
- Zu erledigen: Termine in planning (Geplant, Bestätigt, Durchgeführt) where the person is the
  Kursleitung, since the lead is the one who completes a Termin. Per Termin one task: participants
  to enter after the course, else its most severe open hint, else "Final bestätigen" once it is
  held. Plus calendar invitations the person has not answered yet (lead or support).
- Meine nächsten Termine and the workload: every Termin where the person is lead or support.

Workload is course hours only for now (Ende minus Beginn, or the recorded duration). The overhead
rule (`PROPOSAL.md` 4.3) and the weekly hours from Kimai come with the Team page (WM-131, WM-142);
until then the capacity is the Person's optional `weekly_hours`, and without it the bars show the
hours alone.
"""

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from metrics import hints
from metrics.db import (
    ABGESAGT,
    DURCHGEFUEHRT,
    Appointment,
    CalendarResponse,
    Course,
    Person,
    StoredHint,
    appointment_support,
)
from metrics.planning import workflow

WEEKS = 4  # the workload shows this week and the next three
UPCOMING_DAYS = 28  # "Meine nächsten Termine": the next four weeks
UPCOMING_LIMIT = 12
LEVEL_ORDER = {level: i for i, level in enumerate(hints.LEVELS)}
UNANSWERED = "NEEDS-ACTION"
# Rules whose text a task replaces with its own words.
PARTICIPANT_LABELS = {"TN weiblich", "TN männlich", "Herkunft"}


def _load(stmt):
    return stmt.options(
        selectinload(Appointment.course).selectinload(Course.format),
        selectinload(Appointment.course).selectinload(Course.context),
        selectinload(Appointment.course).selectinload(Course.place),
        selectinload(Appointment.lead),
        selectinload(Appointment.support),
    )


def involving(person_id: int):
    """SQL: the Termin has the person as lead or support."""
    support = select(appointment_support.c.appointment_id).where(appointment_support.c.person_id == person_id)
    return (Appointment.lead_id == person_id) | Appointment.id.in_(support)


@dataclass(frozen=True)
class Task:
    """One line of "Zu erledigen"."""

    t: Appointment
    level: str  # hint level, for the badge
    badge: str  # the badge's short text
    text: str
    action: str  # the button's label
    more: int = 0  # further open hints on the Termin

    @property
    def href(self) -> str:
        return f"/planung/{self.t.course_id}?termin={self.t.id}"


def _open_hints(session: Session, ids: list[int]) -> dict[int, list[StoredHint]]:
    found: dict[int, list[StoredHint]] = defaultdict(list)
    if not ids:
        return found
    stmt = select(StoredHint).where(
        StoredHint.appointment_id.in_(ids), StoredHint.blocking | ~hints._is_accepted()
    )
    for h in session.scalars(stmt.order_by(StoredHint.id)):
        found[h.appointment_id].append(h)
    for items in found.values():
        items.sort(key=lambda h: LEVEL_ORDER.get(h.level, 9))
    return found


def _participants_missing(t: Appointment) -> bool:
    return t.stage == DURCHGEFUEHRT and bool(PARTICIPANT_LABELS & set(hints.missing(t, hints.FINAL_FIELDS)))


def tasks(session: Session, person: Person, today: dt.date) -> list[Task]:
    """The person's open tasks, most severe first, then by date."""
    led = list(
        session.scalars(
            _load(select(Appointment)).where(
                Appointment.lead_id == person.id, Appointment.stage.in_(workflow.OPEN_STAGES)
            )
        )
    )
    found = _open_hints(session, [t.id for t in led])
    result: list[Task] = []
    for t in led:
        open_hints = found.get(t.id, [])
        if _participants_missing(t):
            gaps = [g for g in hints.missing(t, hints.FINAL_FIELDS) if g in PARTICIPANT_LABELS]
            others = [h for h in open_hints if h.rule != "pflicht-abschluss"]
            text = f"Teilnehmende eintragen ({', '.join(gaps)})"
            result.append(Task(t, hints.KRITISCH, "Teilnehmende", text, "Eintragen", len(others)))
        elif open_hints:
            top = open_hints[0]
            result.append(Task(t, top.level, top.level, top.text, "Ansehen", len(open_hints) - 1))
        elif t.stage == DURCHGEFUEHRT:
            result.append(Task(t, hints.INFO, "Abschluss", "Alles da: final bestätigen", "Abschließen"))
    invited = (
        _load(select(Appointment))
        .join(CalendarResponse, CalendarResponse.appointment_id == Appointment.id)
        .where(
            CalendarResponse.person_id == person.id,
            CalendarResponse.partstat == UNANSWERED,
            Appointment.stage.in_(workflow.OPEN_STAGES),
            Appointment.date >= today,
        )
    )
    for t in session.scalars(invited):
        result.append(Task(t, hints.INFO, "Einladung", "Kalendereinladung noch nicht beantwortet", "Ansehen"))
    result.sort(key=lambda task: (LEVEL_ORDER.get(task.level, 9), task.t.date or dt.date.max, task.t.id))
    return result


def upcoming(session: Session, person: Person, today: dt.date) -> list[Appointment]:
    """The person's Termine in planning from today on, for the next four weeks."""
    end = today + dt.timedelta(days=UPCOMING_DAYS - 1)
    stmt = _load(select(Appointment)).where(
        involving(person.id),
        Appointment.stage.in_(workflow.OPEN_STAGES),
        Appointment.date >= today,
        Appointment.date <= end,
    )
    order = (Appointment.date, Appointment.start_time.nulls_last(), Appointment.id)
    return list(session.scalars(stmt.order_by(*order).limit(UPCOMING_LIMIT)))


@dataclass
class Week:
    """Course hours of one person in one calendar week."""

    monday: dt.date
    lead: Decimal = Decimal(0)
    support: Decimal = Decimal(0)
    termine: int = 0
    without_hours: int = 0  # Termine without times or duration

    @property
    def number(self) -> int:
        return self.monday.isocalendar().week

    @property
    def total(self) -> Decimal:
        return self.lead + self.support


@dataclass
class Workload:
    weeks: list[Week] = field(default_factory=list)
    capacity: Decimal | None = None  # weekly hours of the Person, if entered

    @property
    def scale(self) -> Decimal:
        """What a full bar means without a capacity: the busiest of the weeks."""
        return max([w.total for w in self.weeks] + [Decimal(1)])


def monday_of(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def workload(session: Session, person: Person, today: dt.date, weeks: int = WEEKS) -> Workload:
    """Course hours per week, lead and support apart, for this week and the following ones.
    Every stage but Abgesagt counts: a held Termin of this week was still work."""
    start = monday_of(today)
    result = Workload(
        [Week(start + dt.timedelta(weeks=i)) for i in range(weeks)],
        Decimal(person.weekly_hours) if person.weekly_hours else None,
    )
    end = start + dt.timedelta(weeks=weeks, days=-1)
    stmt = select(Appointment).where(
        involving(person.id),
        Appointment.stage != ABGESAGT,
        Appointment.date >= start,
        Appointment.date <= end,
    )
    for t in session.scalars(stmt.options(selectinload(Appointment.support))):
        week = result.weeks[(t.date - start).days // 7]
        week.termine += 1
        hours = hints.duration(t)
        if hours is None:
            week.without_hours += 1
        elif t.lead_id == person.id:
            week.lead += hours
        else:
            week.support += hours
    return result
