"""Workload of the team (WM-131, `PROPOSAL.md` 4.3): estimated working hours and who is free.

Estimated working hours of a Termin, per person on it (`estimate`):

    lead:    course hours × lead factor    + overhead (the Format's own, else the default)  + travel
    support: course hours × support factor + support overhead                               + travel

Course hours are Ende minus Beginn, else the recorded duration, else the Format's usual duration;
a Termin with none of them counts its overhead only (and is counted as "ohne Dauer"). The factors
and default overheads come from `[workload]` in the instance config (`instance.WorkloadRule`),
the Format's overhead from Stammdaten "Formate", the travel time from the place ("Orte"). Every
stage but Abgesagt counts: a held Termin of this week was still work.

`loads` adds the estimates up per person and calendar week (lead and support apart); `table` is
the Team page's table and Mein Bereich's bars, `capacity` the Person's weekly hours (from Kimai
with WM-142, until then entered by hand). The hint "ueber-kapazitaet" (`metrics.hints`) reads the
same sums.

Absences: `absences` is the hook for WM-142 (Kimai). It returns nothing yet; the calendar lanes,
"Wer ist frei?" and the table already read it, so WM-142 only has to fill it.

"Wer ist frei?" (`who_is_free`) uses the team pickers' own check (`workflow.busy_people`) on the
pickers' own people (`lists.choices`), so both always agree.
"""

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from metrics import hints, lists
from metrics.db import ABGESAGT, Appointment, Course, Person
from metrics.instance import WorkloadRule, get_instance
from metrics.planning import workflow
from metrics.planning.lanes import LEAD, SUPPORT

WEEKS = 4  # the default table: this week and the next three


def rule() -> WorkloadRule:
    return get_instance().workload


def monday_of(day: dt.date) -> dt.date:
    return day - dt.timedelta(days=day.weekday())


def number(value: Decimal | float | int) -> str:
    """Hours the German way, at most one decimal: 2,5 or 3."""
    return f"{float(value):.1f}".replace(".", ",").removesuffix(",0")


def course_hours(t: Appointment) -> Decimal | None:
    """Ende minus Beginn, else the recorded duration, else the Format's usual duration."""
    hours = hints.duration(t)
    if hours is None and t.course is not None and t.course.format is not None:
        hours = t.course.format.duration_hours
    return Decimal(hours) if hours is not None else None


@dataclass(frozen=True)
class Estimate:
    """The estimated working hours of one person on one Termin, with its parts."""

    course: Decimal | None  # course hours, None if unknown
    factor: Decimal
    overhead: Decimal
    travel: Decimal

    @property
    def total(self) -> Decimal:
        return (self.course or Decimal(0)) * self.factor + self.overhead + self.travel


def estimate(t: Appointment, role: str, workload: WorkloadRule | None = None) -> Estimate:
    workload = workload or rule()
    fmt = t.course.format if t.course is not None else None
    place = t.course.place if t.course is not None else None
    if role == LEAD:
        factor = workload.lead_factor
        own = fmt.overhead_hours if fmt is not None else None
        overhead = Decimal(own) if own is not None else workload.lead_overhead
    else:
        factor, overhead = workload.support_factor, workload.support_overhead
    travel = (
        Decimal(place.travel_hours) if place is not None and place.travel_hours is not None else Decimal(0)
    )
    return Estimate(course_hours(t), factor, overhead, travel)


def team_of(t: Appointment) -> list[tuple[Person, str]]:
    return ([(t.lead, LEAD)] if t.lead else []) + [(p, SUPPORT) for p in t.support]


@dataclass
class Cell:
    """One person in one calendar week: estimated hours of their Termine, lead and support apart."""

    monday: dt.date
    lead: Decimal = Decimal(0)
    support: Decimal = Decimal(0)
    lead_termine: int = 0
    support_termine: int = 0
    without_hours: int = 0  # Termine without course hours (overhead only)
    absent: list[str] = field(default_factory=list)  # absence kinds that week (WM-142)

    @property
    def number(self) -> int:
        return self.monday.isocalendar().week

    @property
    def total(self) -> Decimal:
        return self.lead + self.support

    @property
    def termine(self) -> int:
        return self.lead_termine + self.support_termine

    def add(self, role: str, hours: Decimal, known: bool) -> None:
        if role == LEAD:
            self.lead += hours
            self.lead_termine += 1
        else:
            self.support += hours
            self.support_termine += 1
        if not known:
            self.without_hours += 1


def counted(appointments: Iterable[Appointment]) -> list[Appointment]:
    return [t for t in appointments if t.date is not None and t.stage != ABGESAGT]


def loads(
    appointments: Iterable[Appointment], workload: WorkloadRule | None = None
) -> dict[tuple[int, dt.date], Cell]:
    """(person ID, Monday) -> the person's estimated hours that week, from these Termine."""
    workload = workload or rule()
    found: dict[tuple[int, dt.date], Cell] = {}
    for t in counted(appointments):
        monday = monday_of(t.date)
        for person, role in team_of(t):
            e = estimate(t, role, workload)
            cell = found.setdefault((person.id, monday), Cell(monday))
            cell.add(role, e.total, e.course is not None)
    return found


def load_stmt(stmt):
    """What `estimate` and the lanes read, loaded with the Termine."""
    return stmt.options(
        selectinload(Appointment.course).selectinload(Course.format),
        selectinload(Appointment.course).selectinload(Course.context),
        selectinload(Appointment.course).selectinload(Course.place),
        selectinload(Appointment.lead),
        selectinload(Appointment.support),
    )


def termine(session: Session, start: dt.date, end: dt.date) -> list[Appointment]:
    """Every Termin from `start` to `end` but the Abgesagt ones."""
    stmt = select(Appointment).where(
        Appointment.date >= start, Appointment.date <= end, Appointment.stage != ABGESAGT
    )
    return list(session.scalars(load_stmt(stmt).order_by(Appointment.date, Appointment.id)))


def absences(session: Session, start: dt.date, end: dt.date) -> dict[int, dict[dt.date, str]]:
    """person ID -> day -> kind of absence (Urlaub, krank, …) from `start` to `end`.

    The hook for WM-142, which reads the absences from Kimai; until then nobody is absent.
    """
    return {}


@dataclass
class Row:
    """One person in the workload table."""

    person: Person
    cells: list[Cell]

    @property
    def capacity(self) -> Decimal | None:
        return Decimal(self.person.weekly_hours) if self.person.weekly_hours else None

    @property
    def total(self) -> Decimal:
        return sum((c.total for c in self.cells), Decimal(0))

    def above(self, cell: Cell) -> bool:
        """Above the weekly hours (the hint "ueber-kapazitaet")."""
        return self.capacity is not None and cell.total > self.capacity


def mondays(start: dt.date, weeks: int) -> list[dt.date]:
    first = monday_of(start)
    return [first + dt.timedelta(weeks=i) for i in range(weeks)]


def table(
    session: Session,
    people: Iterable[Person],
    start: dt.date,
    weeks: int = WEEKS,
    appointments: list[Appointment] | None = None,
) -> list[Row]:
    """Per person and calendar week from the week of `start`: Termine and estimated hours.

    `people` get a row each (the active team), plus anyone else with a Termin in those weeks.
    `appointments` may pass the Termine of those weeks when the caller has them already.
    """
    days = mondays(start, weeks)
    end = days[-1] + dt.timedelta(days=6)
    shown = appointments if appointments is not None else termine(session, days[0], end)
    shown = [t for t in shown if t.date is not None and days[0] <= t.date <= end]
    found = loads(shown)
    everyone = {p.id: p for p in people}
    for t in counted(shown):
        for person, _ in team_of(t):
            everyone.setdefault(person.id, person)
    away = absences(session, days[0], end)
    rows = []
    for person in sorted(everyone.values(), key=lambda p: p.name.casefold()):
        cells = [found.get((person.id, monday)) or Cell(monday) for monday in days]
        for cell in cells:
            kinds = {
                k
                for d, k in away.get(person.id, {}).items()
                if cell.monday <= d <= cell.monday + dt.timedelta(days=6)
            }
            cell.absent = sorted(kinds)
        rows.append(Row(person, cells))
    return rows


def formula(workload: WorkloadRule | None = None) -> list[str]:
    """The rule in German words, for the page."""
    w = workload or rule()
    return [
        f"Kursleitung: Kursstunden × {number(w.lead_factor)} + {number(w.lead_overhead)} Std. Vor- und "
        "Nachbereitung (oder der Wert des Formats)",
        f"Unterstützung: Kursstunden × {number(w.support_factor)} + {number(w.support_overhead)} Std.",
        "Dazu für alle die Fahrtzeit des Orts, falls eingetragen.",
        "Kursstunden: Ende minus Beginn, sonst die eingetragene Dauer, sonst die übliche Dauer des Formats.",
    ]


@dataclass
class Free:
    """ "Wer ist frei?" at one date and time."""

    day: dt.date
    start: dt.time | None
    end: dt.time | None
    free: list[Person]
    busy: list[tuple[Person, str]]  # with the Termin that keeps them busy
    absent: list[tuple[Person, str]]  # with the kind of absence


def who_is_free(session: Session, day: dt.date, start: dt.time | None, end: dt.time | None) -> Free:
    """The team pickers' people (`lists.choices`) without a Termin then (`workflow.busy_people`,
    the pickers' own check) and without an absence that day."""
    team = lists.choices(session, lists.PEOPLE)
    busy = workflow.busy_people(session, day, start, end)
    away = absences(session, day, day)
    result = Free(day, start, end, [], [], [])
    for person in team:
        if kind := away.get(person.id, {}).get(day):
            result.absent.append((person, kind))
        elif person.id in busy:
            result.busy.append((person, busy[person.id]))
        else:
            result.free.append(person)
    return result
