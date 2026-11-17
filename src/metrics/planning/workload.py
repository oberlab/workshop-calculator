"""Workload of the team (WM-131, `PROPOSAL.md` 4.3): estimated working hours and who is free.

Estimated working hours of a Termin, per person on it (`estimate`):

    lead:    course hours × lead factor + preparation (factor × course hours) + fixed hours + travel
    support: course hours × support factor                                    + fixed hours + travel

Course hours are Ende minus Beginn, else the recorded duration, else the Format's usual duration;
a Termin with none of them counts its fixed hours and travel only (and is counted as "ohne Dauer").
Each value comes from the first of (`values`, Alex 2026-11-16):

- the course's own value (course page, "Arbeitszeit"; every value can be set),
- the Format's fixed hours (lead) or the place's travel time ("Orte"),
- the default in `[workload]` of the instance config (`instance.WorkloadRule`; no travel).

Every stage but Abgesagt counts: a held Termin of this week was still work.

`loads` adds the estimates up per person and calendar week (lead and support apart); `table` is
the Team page's table and Mein Bereich's bars, `capacity` the Person's weekly hours (from Kimai's
working contract, WM-142; entered by hand for people without one). The hint "ueber-kapazitaet"
(`metrics.hints`) reads the same sums.

Absences (WM-142): `absences` reads the absences from Kimai (`metrics.absences`) for the calendar
lanes, "Wer ist frei?" and the table; only the viewer sees their own kind, everyone else "abwesend".

"Wer ist frei?" (`who_is_free`) uses the team pickers' own check (`workflow.busy_people`) on the
pickers' own people (`lists.choices`), so both always agree.
"""

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from metrics import absences as away
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


def exact(value: Decimal | float | int) -> str:
    """A value of the rule as entered, with a decimal comma: 0,25 or 1."""
    return f"{Decimal(value).normalize():f}".replace(".", ",")


def course_hours(t: Appointment) -> Decimal | None:
    """Ende minus Beginn, else the recorded duration, else the Format's usual duration."""
    hours = hints.duration(t)
    if hours is None and t.course is not None and t.format is not None:
        hours = t.format.duration_hours
    return Decimal(hours) if hours is not None else None


STANDARD, OWN, FORMAT, PLACE = "Standard", "Kurs", "Format", "Ort"


@dataclass(frozen=True)
class Value:
    """One value of the rule for a course, with where it came from."""

    amount: Decimal
    source: str  # OWN, FORMAT, PLACE or STANDARD
    name: str | None = None  # the Format's or place's name

    @property
    def origin(self) -> str:
        """Where the value came from, in German: "Kurs", "Format „HackDays“", "Standard"."""
        return f"{self.source} „{self.name}“" if self.name else self.source


def _first(*candidates: tuple[Decimal | None, str, str | None]) -> Value:
    for amount, source, name in candidates:
        if amount is not None:
            return Value(Decimal(amount), source, name)
    raise ValueError("no value")  # the last candidate is always a default


@dataclass(frozen=True)
class Values:
    """The rule's values for the Termine of one course."""

    lead_factor: Value
    preparation_factor: Value
    lead_overhead: Value
    support_factor: Value
    support_overhead: Value
    travel: Value


# German terms of the values, as the course page and the Team page show them.
TERMS = {
    "lead_factor": "Faktor Kursleitung",
    "preparation_factor": "Vorbereitung",
    "lead_overhead": "Zuschlag Kursleitung",
    "support_factor": "Faktor Unterstützung",
    "support_overhead": "Zuschlag Unterstützung",
    "travel": "Fahrtzeit",
}


def values(
    course: Course | None,
    workload: WorkloadRule | None = None,
    own_values: bool = True,
    t: Appointment | None = None,
) -> Values:
    """The rule for this course: its own value, else the Format's or place's, else the default.
    Without `own_values`: what the course inherits (the course page shows it for empty fields).
    With a Termin `t`: its Format and place (the ones it kept, WM-135)."""
    w = workload or rule()
    holder = t if t is not None else course
    fmt = holder.format if holder is not None else None
    place = holder.place if holder is not None else None

    def own(name: str) -> tuple[Decimal | None, str, None]:
        return (getattr(course, name) if course is not None and own_values else None, OWN, None)

    return Values(
        lead_factor=_first(own("lead_factor"), (w.lead_factor, STANDARD, None)),
        preparation_factor=_first(own("preparation_factor"), (w.preparation_factor, STANDARD, None)),
        lead_overhead=_first(
            own("lead_overhead_hours"),
            (fmt.overhead_hours if fmt else None, FORMAT, fmt.name if fmt else None),
            (w.lead_overhead, STANDARD, None),
        ),
        support_factor=_first(own("support_factor"), (w.support_factor, STANDARD, None)),
        support_overhead=_first(own("support_overhead_hours"), (w.support_overhead, STANDARD, None)),
        travel=_first(
            own("travel_hours"),
            (place.travel_hours if place else None, PLACE, place.name if place else None),
            (Decimal(0), STANDARD, None),
        ),
    )


@dataclass(frozen=True)
class Estimate:
    """The estimated working hours of one person on one Termin, with its parts."""

    course: Decimal | None  # course hours, None if unknown
    factor: Decimal
    preparation: Decimal  # factor of the course hours (lead only)
    overhead: Decimal
    travel: Decimal

    @property
    def total(self) -> Decimal:
        return (self.course or Decimal(0)) * (self.factor + self.preparation) + self.overhead + self.travel


def estimate(t: Appointment, role: str, workload: WorkloadRule | None = None) -> Estimate:
    v = values(t.course, workload, t=t)
    if role == LEAD:
        parts = (v.lead_factor, v.preparation_factor, v.lead_overhead)
    else:
        parts = (v.support_factor, Value(Decimal(0), STANDARD), v.support_overhead)
    return Estimate(course_hours(t), *(p.amount for p in parts), v.travel.amount)


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


def absences(
    session: Session, start: dt.date, end: dt.date, own: int | None = None
) -> dict[int, dict[dt.date, str]]:
    """person ID -> day -> what the page shows of the absence from `start` to `end`: the kind
    (Urlaub, Krank, …) for the person `own` (the viewer), "abwesend" for everyone else (WM-142)."""
    return away.labels(away.by_person(session, start, end), own)


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
    own: int | None = None,
) -> list[Row]:
    """Per person and calendar week from the week of `start`: Termine and estimated hours.

    `people` get a row each (the active team), plus anyone else with a Termin in those weeks.
    `appointments` may pass the Termine of those weeks when the caller has them already; `own` is
    the viewer, who sees the kind of their own absences.
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
    absent = absences(session, days[0], end, own)
    rows = []
    for person in sorted(everyone.values(), key=lambda p: p.name.casefold()):
        cells = [found.get((person.id, monday)) or Cell(monday) for monday in days]
        for cell in cells:
            kinds = {
                k
                for d, k in absent.get(person.id, {}).items()
                if cell.monday <= d <= cell.monday + dt.timedelta(days=6)
            }
            cell.absent = sorted(kinds)
        rows.append(Row(person, cells))
    return rows


def _times(factor: Decimal) -> str:
    return "Kurszeit" if factor == 1 else f"{exact(factor)} × Kurszeit"


def formula(workload: WorkloadRule | None = None) -> list[str]:
    """The rule in German words, for the page."""
    w = workload or rule()
    return [
        f"Kursleitung: {_times(w.lead_factor)} + Vorbereitung ({exact(w.preparation_factor)} × Kurszeit) "
        f"+ {exact(w.lead_overhead)} Std. Zuschlag + Fahrtzeit",
        f"Unterstützung: {_times(w.support_factor)} + {exact(w.support_overhead)} Std. Zuschlag + Fahrtzeit",
        "Kurszeit: Ende minus Beginn, sonst die eingetragene Dauer, sonst die übliche Dauer des Formats.",
        "Fahrtzeit: die des Orts, falls eingetragen.",
        "Ein Kurs kann jeden dieser Werte selbst festlegen (Kursseite, „Arbeitszeit“). Sonst gilt der "
        "Zuschlag des Formats (Kursleitung) bzw. die Fahrtzeit des Orts, sonst der Standard.",
    ]


def show(name: str, value: Value) -> str:
    """One value with its unit: "1 × Kurszeit" (factors), "2 Std."."""
    unit = " × Kurszeit" if name.endswith("factor") else " Std."
    return f"{exact(value.amount)}{unit}"


def own_values(appointments: Iterable[Appointment]) -> list[tuple[Course, list[str]]]:
    """The courses of these Termine that set values of the rule themselves, with those values."""
    found: dict[int, tuple[Course, list[str]]] = {}
    for t in counted(appointments):
        if t.course is None or t.course_id in found:
            continue
        v = values(t.course, t=t)
        own = [f"{TERMS[n]} {show(n, x)}" for n, x in vars(v).items() if x.source == OWN]
        if own:
            found[t.course_id] = (t.course, own)
    return list(found.values())


@dataclass
class Free:
    """ "Wer ist frei?" at one date and time."""

    day: dt.date
    start: dt.time | None
    end: dt.time | None
    free: list[Person]
    busy: list[tuple[Person, str]]  # with the Termin that keeps them busy
    absent: list[tuple[Person, str]]  # with the kind of absence


def who_is_free(
    session: Session, day: dt.date, start: dt.time | None, end: dt.time | None, own: int | None = None
) -> Free:
    """The team pickers' people (`lists.choices`) without a Termin then (`workflow.busy_people`,
    the pickers' own check) and without an absence that day (`own`: the viewer, see `absences`)."""
    team = lists.choices(session, lists.PEOPLE)
    busy = workflow.busy_people(session, day, start, end)
    absent = absences(session, day, day, own)
    result = Free(day, start, end, [], [], [])
    for person in team:
        if kind := absent.get(person.id, {}).get(day):
            result.absent.append((person, kind))
        elif person.id in busy:
            result.busy.append((person, busy[person.id]))
        else:
            result.free.append(person)
    return result
