"""Every metric definition in one place (WM-150): which Termine count, in which period, and how.

The charts (`metrics.analytics`), the key figures on the front page, the KPI tiles of the analysis
and project pages and the Slack weekly status (`metrics.messages`) all count through this module,
so a figure means the same everywhere.

- **Einsatz:** a held Termin, i.e. finally confirmed (stage Abgeschlossen) and dated (`held`).
  The filters (period, category, context, target group) restrict the held Termine (`select_held`).
- **Teilnehmende:** the participants of the held Termine on which they count (`COUNTS_PARTICIPANTS`):
  Terminnummer 1, so a child in a course of several dates counts once; for a Termin made in the app
  for a continuous format every Termin. Termine without a number count nothing.
- **Orte:** distinct places with at least one held Termin.
- **Stunden:** course hours, the sum of the Dauer of the held Termine (Ende minus Beginn; stored
  on the Termin when it is saved, the recorded Dauer for imported rows). Termine without a Dauer
  add nothing. Not the team's estimated working hours (`metrics.planning.workload`).
- **Schulen:** distinct places marked as a school (`Place.is_school`) with at least one held Termin.

The comparison period (`previous_period`) of a KPI tile has the same length and ends the day before
the selected period starts: the previous school or calendar year for those presets, the 12 months
before for "Letzte 12 Monate", the same number of days before for a custom range. "Gesamter
Zeitraum" and an open custom range have none.
"""

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from metrics.db import STAGE_COUNTED, Appointment, Context, Course, Format, Place

# ---- Periods ----------------------------------------------------------------------------------


def add_months(d: date, months: int) -> date:
    """The first of the month `months` after the month of `d`."""
    index = d.year * 12 + d.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def school_year_start(d: date, start_month: int) -> date:
    year = d.year if d.month >= start_month else d.year - 1
    return date(year, start_month, 1)


def resolve_period(
    preset: str,
    today: date,
    school_year_start_month: int,
    custom_from: date | None = None,
    custom_to: date | None = None,
) -> tuple[date | None, date | None]:
    """Inclusive (from, to) for a preset; None means open."""
    if preset == "all":
        return None, None
    if preset in ("this_school_year", "last_school_year"):
        start = school_year_start(today, school_year_start_month)
        if preset == "last_school_year":
            start = add_months(start, -12)
        return start, add_months(start, 12) - timedelta(days=1)
    if preset == "this_year":
        return date(today.year, 1, 1), date(today.year, 12, 31)
    if preset == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if preset == "last_12_months":
        # The current calendar month and the 11 before it.
        return add_months(today.replace(day=1), -11), add_months(today.replace(day=1), 1) - timedelta(days=1)
    if preset == "custom":
        return custom_from, custom_to
    raise ValueError(f"Unknown period preset: {preset}")


# The presets that span twelve whole months: their comparison period is the twelve months before.
TWELVE_MONTHS = ("this_school_year", "last_school_year", "this_year", "last_year", "last_12_months")


def previous_period(
    preset: str,
    today: date,
    school_year_start_month: int,
    custom_from: date | None = None,
    custom_to: date | None = None,
) -> tuple[date, date] | None:
    """Inclusive (from, to) of the comparison period: as long as the selected one, directly before it.

    None for the whole time ("all") and for a custom range that is open on one side or empty.
    """
    start, end = resolve_period(preset, today, school_year_start_month, custom_from, custom_to)
    if preset in TWELVE_MONTHS:
        return add_months(start, -12), start - timedelta(days=1)
    if preset == "custom" and start and end and start <= end:
        return start - (end - start + timedelta(days=1)), start - timedelta(days=1)
    return None


# ---- Which Termine count ----------------------------------------------------------------------


@dataclass
class Filters:
    bin: str = "month"
    preset: str = "all"
    date_from: date | None = None
    date_to: date | None = None
    categories: list[str] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)
    target_groups: list[str] = field(default_factory=list)


# The columns the analysis filters and groups by, from the Termin and its course.
DATE = Appointment.date
CATEGORY = Format.category
CONTEXT = Context.name
TARGET_GROUP = Course.target_group
PLACE = Place.name


def joined(query):
    """A query over Termine with their course, format, context and place."""
    return (
        query.select_from(Appointment)
        .join(Course, Appointment.course_id == Course.id)
        .outerjoin(Format, Course.format_id == Format.id)
        .outerjoin(Context, Course.context_id == Context.id)
        .outerjoin(Place, Course.place_id == Place.id)
    )


def held(query):
    """Termine that count as an Einsatz: finally confirmed (Abgeschlossen) and dated."""
    return joined(query).where(Appointment.stage == STAGE_COUNTED, DATE.is_not(None))


def select_held(query, filters: Filters, today: date, school_year_start_month: int):
    """Restrict a query to held Termine matching all filters; returns it with the resolved period."""
    start, end = resolve_period(
        filters.preset, today, school_year_start_month, filters.date_from, filters.date_to
    )
    query = held(query)
    if start:
        query = query.where(DATE >= start)
    if end:
        query = query.where(DATE <= end)
    if filters.categories:
        query = query.where(CATEGORY.in_(filters.categories))
    if filters.contexts:
        query = query.where(CONTEXT.in_(filters.contexts))
    if filters.target_groups:
        query = query.where(TARGET_GROUP.in_(filters.target_groups))
    return query, (start, end)


# Participants a held Termin adds to the total: Terminnummer 1 counts, so children in multi-session
# courses count once (rule of the R analysis); Termine without a number count nothing. A Termin made
# in the app for a continuous format (WM-114) counts at every number. Imported rows keep their
# recorded rule (they record number 1 on every Termin of a continuous format).
COUNTS_PARTICIPANTS = or_(
    Appointment.number == 1,
    and_(Appointment.source == "app", Format.continuous.is_(True), Appointment.number.is_not(None)),
)
counted_participants = case((COUNTS_PARTICIPANTS, Appointment.participants_total), else_=0)

# ---- Key figures ------------------------------------------------------------------------------

# Aggregates over the held Termine of a selection (`select_held`), one per key figure.
SESSIONS = func.count()
PARTICIPANTS = func.coalesce(func.sum(counted_participants), 0)
PLACES = func.count(func.distinct(Place.id))
HOURS = func.coalesce(func.sum(Appointment.duration_hours), 0)
SCHOOLS = func.count(func.distinct(Place.id)).filter(Place.is_school.is_(True))


@dataclass(frozen=True)
class KeyFigures:
    sessions: int  # Einsätze
    participants: int  # Teilnehmende
    places: int  # Orte
    hours: Decimal  # Stunden (course hours)
    schools: int  # Schulen erreicht


def key_figures(session: Session, filters: Filters, today: date, school_year_start_month: int) -> KeyFigures:
    """The key figures of the held Termine matching the filters (definitions in the module docstring)."""
    query, _ = select_held(
        select(SESSIONS, PARTICIPANTS, PLACES, HOURS, SCHOOLS), filters, today, school_year_start_month
    )
    sessions, participants, places, hours, schools = session.execute(query).one()
    return KeyFigures(int(sessions), int(participants), int(places), Decimal(hours), int(schools))


@dataclass(frozen=True)
class Totals:
    """The key figures below the chart on the front page and in the Slack weekly status."""

    sessions: int
    participants: int
    locations: int


def totals(session: Session, filters: Filters, today: date, school_year_start_month: int) -> Totals:
    """Einsätze, Teilnehmende and Orte of the selection, as `key_figures` counts them."""
    figures = key_figures(session, filters, today, school_year_start_month)
    return Totals(sessions=figures.sessions, participants=figures.participants, locations=figures.places)


@dataclass(frozen=True)
class Comparison:
    """The key figures of a selection and, if it has one, of its comparison period."""

    period: tuple[date | None, date | None]
    figures: KeyFigures
    previous_period: tuple[date, date] | None
    previous: KeyFigures | None


def compare(session: Session, filters: Filters, today: date, school_year_start_month: int) -> Comparison:
    """Key figures for the selection and for the same filters in the previous period."""
    chosen = (filters.preset, today, school_year_start_month, filters.date_from, filters.date_to)
    period = resolve_period(*chosen)
    figures = key_figures(session, filters, today, school_year_start_month)
    before = previous_period(*chosen)
    previous = None
    if before:
        earlier = replace(filters, preset="custom", date_from=before[0], date_to=before[1])
        previous = key_figures(session, earlier, today, school_year_start_month)
    return Comparison(period, figures, before, previous)
