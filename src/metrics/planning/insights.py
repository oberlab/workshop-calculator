"""Planning insights (WM-151, `PROPOSAL.md` 4.12): how the team's planning went, team only.

Which Termine count is defined in `metrics.metrics` (planning insights section); this module only
adds them up for the page "Planungszahlen" (`metrics.web.insights`):

- **Team hours** per person and time bin: the estimated working hours of the workload rule
  (`metrics.planning.workload.estimate`) of the planned Termine (dated, not Abgesagt), lead and
  support apart. The page names people, so it is behind the team login like the Team page.
- **Equipment usage** per item over the planned Termine: how many Termine and days needed it, the
  most needed on one day and on how many days that was more than is available (a kit blocks the
  whole day, `metrics.equipment.day_totals`).
- **Cancellations** (Abgesagt, dated) per reason, and their share of all dated Termine.
- **Lead time**: days from creating a Termin in the app to its date, in bands, with the median.
- **Open billing**: Termine that took place and are not billed yet.
"""

import statistics
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from metrics import analytics, equipment, metrics
from metrics.db import Appointment, AppointmentEquipment, Course, Equipment, Person
from metrics.metrics import Filters
from metrics.planning import workload
from metrics.planning.lanes import LEAD


@dataclass(frozen=True)
class Bin:
    start: date
    end: date  # exclusive
    label: str


def bins_between(first: date, last: date, bin_name: str, school_year_start_month: int) -> list[Bin]:
    """Every time bin from the one holding `first` to the one holding `last`, empty ones included."""
    result = []
    start = analytics.bin_start(first, bin_name, school_year_start_month)
    while start <= last:
        end = analytics.bin_end(start, bin_name)
        result.append(Bin(start, end, analytics.bin_label(start, bin_name)))
        start = end
    return result


@dataclass
class Hours:
    """Estimated working hours of one person (or the team) in one bin."""

    lead: Decimal = Decimal(0)
    support: Decimal = Decimal(0)
    termine: int = 0

    @property
    def total(self) -> Decimal:
        return self.lead + self.support

    def add(self, role: str, hours: Decimal) -> None:
        if role == LEAD:
            self.lead += hours
        else:
            self.support += hours
        self.termine += 1


@dataclass
class PersonHours:
    person: Person
    cells: list[Hours]

    @property
    def total(self) -> Decimal:
        return sum((c.total for c in self.cells), Decimal(0))


@dataclass
class TeamHours:
    bins: list[Bin]
    rows: list[PersonHours]  # people with at least one Termin, by name
    totals: list[Hours]  # the whole team per bin


def _planned_termine(session: Session, filters: Filters, today: date, school_year_start_month: int):
    query, period = metrics.select_planned(select(Appointment), filters, today, school_year_start_month)
    termine = list(session.scalars(workload.load_stmt(query).order_by(Appointment.date, Appointment.id)))
    return termine, period


def team_hours(session: Session, filters: Filters, today: date, school_year_start_month: int) -> TeamHours:
    termine, (start, end) = _planned_termine(session, filters, today, school_year_start_month)
    dates = [t.date for t in termine]
    if not dates:
        return TeamHours([], [], [])
    bins = bins_between(start or min(dates), end or max(dates), filters.bin, school_year_start_month)
    index = {b.start: i for i, b in enumerate(bins)}
    rule = workload.rule()
    people: dict[int, Person] = {}
    cells: dict[int, list[Hours]] = {}
    totals = [Hours() for _ in bins]
    for t in termine:
        i = index[analytics.bin_start(t.date, filters.bin, school_year_start_month)]
        for person, role in workload.team_of(t):
            hours = workload.estimate(t, role, rule).total
            people.setdefault(person.id, person)
            cells.setdefault(person.id, [Hours() for _ in bins])[i].add(role, hours)
            totals[i].add(role, hours)
    rows = [PersonHours(p, cells[p.id]) for p in sorted(people.values(), key=lambda p: p.name.casefold())]
    return TeamHours(bins, rows, totals)


@dataclass
class EquipmentUse:
    item: Equipment
    termine: int = 0
    days: int = 0
    peak: int = 0  # the most needed on one day
    days_short: int = 0  # days on which more was needed than is available


def equipment_usage(
    session: Session, filters: Filters, today: date, school_year_start_month: int
) -> list[EquipmentUse]:
    """Usage per equipment item over the planned Termine; items without use are left out."""
    query, _ = metrics.select_planned(select(Appointment), filters, today, school_year_start_month)
    query = query.where(Appointment.id.in_(select(AppointmentEquipment.appointment_id))).options(
        selectinload(Appointment.equipment).selectinload(AppointmentEquipment.equipment)
    )
    termine = list(session.scalars(query))
    uses: dict[int, EquipmentUse] = {}
    for t in termine:
        for row in t.equipment:
            uses.setdefault(row.equipment_id, EquipmentUse(row.equipment)).termine += 1
    for needs in equipment.day_totals(termine).values():
        for item_id, quantity in needs.items():
            use = uses[item_id]
            use.days += 1
            use.peak = max(use.peak, quantity)
            if use.item.quantity is not None and quantity > use.item.quantity:
                use.days_short += 1
    return sorted(uses.values(), key=lambda u: (-u.termine, u.item.name.casefold()))


NO_REASON = "Ohne Grund"


@dataclass
class Cancellations:
    reasons: list[tuple[str, int]]  # by count, "Ohne Grund" last
    cancelled: int
    planned: int  # dated Termine that were not cancelled

    @property
    def share(self) -> float | None:
        """Cancelled Termine as a share of all dated Termine (cancelled or not)."""
        whole = self.cancelled + self.planned
        return self.cancelled / whole if whole else None


def cancellations(
    session: Session, filters: Filters, today: date, school_year_start_month: int
) -> Cancellations:
    query, _ = metrics.select_cancelled(
        select(metrics.CANCEL_REASON, func.count()).group_by(metrics.CANCEL_REASON),
        filters,
        today,
        school_year_start_month,
    )
    found = {reason: count for reason, count in session.execute(query)}
    planned, _ = metrics.select_planned(select(func.count()), filters, today, school_year_start_month)
    reasons = sorted(
        ((r, n) for r, n in found.items() if r is not None), key=lambda x: (-x[1], x[0].casefold())
    )
    if None in found:
        reasons.append((NO_REASON, found[None]))
    return Cancellations(reasons, sum(found.values()), session.execute(planned).scalar_one())


# Lead time bands: (label, lowest day count, highest day count or None).
LEAD_BANDS = [
    ("Nachgetragen (nach dem Termin angelegt)", None, -1),
    ("Unter 1 Woche", 0, 6),
    ("1 bis 2 Wochen", 7, 13),
    ("2 bis 4 Wochen", 14, 27),
    ("4 bis 8 Wochen", 28, 55),
    ("8 Wochen und mehr", 56, None),
]


@dataclass
class LeadTime:
    bands: list[tuple[str, int]] = field(default_factory=list)
    count: int = 0
    median: float | None = None


def lead_time(session: Session, filters: Filters, today: date, school_year_start_month: int) -> LeadTime:
    query, _ = metrics.select_lead_time(select(metrics.LEAD_DAYS), filters, today, school_year_start_month)
    days = list(session.scalars(query))
    bands = [
        (label, sum(1 for d in days if (low is None or d >= low) and (high is None or d <= high)))
        for label, low, high in LEAD_BANDS
    ]
    return LeadTime(bands, len(days), statistics.median(days) if days else None)


def unbilled(
    session: Session, filters: Filters, today: date, school_year_start_month: int
) -> list[Appointment]:
    """Termine that took place and are not billed yet, oldest first."""
    query, _ = metrics.select_unbilled(select(Appointment), filters, today, school_year_start_month)
    query = query.options(
        selectinload(Appointment.course).selectinload(Course.format),
        selectinload(Appointment.course).selectinload(Course.context),
        selectinload(Appointment.course).selectinload(Course.place),
    )
    return list(session.scalars(query.order_by(Appointment.date, Appointment.id)))
