"""Time bins, period presets and the aggregations behind the charts."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import NamedTuple

from sqlalchemy import and_, case, func, or_, select
from sqlalchemy.orm import Session

from metrics.db import STAGE_COUNTED, Appointment, Context, Course, Format, Person, Place, SchoolType
from metrics.instance import Instance

MONTHS = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]

BINS = {
    "week": "Woche",
    "month": "Monat",
    "quarter": "Quartal",
    "year": "Kalenderjahr",
    "school_year": "Schuljahr",
}
BIN_PER = {
    "week": "Woche",
    "month": "Monat",
    "quarter": "Quartal",
    "year": "Jahr",
    "school_year": "Schuljahr",
}

PRESETS = {
    "all": "Gesamter Zeitraum",
    "this_school_year": "Dieses Schuljahr",
    "last_school_year": "Letztes Schuljahr",
    "this_year": "Dieses Kalenderjahr",
    "last_year": "Letztes Kalenderjahr",
    "last_12_months": "Letzte 12 Monate",
    "custom": "Eigener Zeitraum",
}

NO_CATEGORY = "Ohne Kategorie"


def _add_months(d: date, months: int) -> date:
    index = d.year * 12 + d.month - 1 + months
    return date(index // 12, index % 12 + 1, 1)


def school_year_start(d: date, start_month: int) -> date:
    year = d.year if d.month >= start_month else d.year - 1
    return date(year, start_month, 1)


def bin_start(d: date, bin_name: str, school_year_start_month: int) -> date:
    if bin_name == "week":
        return d - timedelta(days=d.weekday())
    if bin_name == "month":
        return d.replace(day=1)
    if bin_name == "quarter":
        return date(d.year, (d.month - 1) // 3 * 3 + 1, 1)
    if bin_name == "year":
        return date(d.year, 1, 1)
    if bin_name == "school_year":
        return school_year_start(d, school_year_start_month)
    raise ValueError(f"Unknown bin: {bin_name}")


def bin_end(start: date, bin_name: str) -> date:
    """Exclusive end of the bin that starts at `start`."""
    if bin_name == "week":
        return start + timedelta(days=7)
    return _add_months(start, {"month": 1, "quarter": 3, "year": 12, "school_year": 12}[bin_name])


def bin_label(start: date, bin_name: str) -> str:
    if bin_name == "week":
        year, week, _ = start.isocalendar()
        return f"KW {week}/{year}"
    if bin_name == "month":
        return f"{MONTHS[start.month - 1]} {start.year}"
    if bin_name == "quarter":
        return f"Q{(start.month - 1) // 3 + 1} {start.year}"
    if bin_name == "year":
        return str(start.year)
    return f"{start.year}/{str(start.year + 1)[-2:]}"


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
            start = _add_months(start, -12)
        return start, _add_months(start, 12) - timedelta(days=1)
    if preset == "this_year":
        return date(today.year, 1, 1), date(today.year, 12, 31)
    if preset == "last_year":
        return date(today.year - 1, 1, 1), date(today.year - 1, 12, 31)
    if preset == "last_12_months":
        # The current calendar month and the 11 before it.
        return _add_months(today.replace(day=1), -11), _add_months(today.replace(day=1), 1) - timedelta(
            days=1
        )
    if preset == "custom":
        return custom_from, custom_to
    raise ValueError(f"Unknown period preset: {preset}")


@dataclass
class Filters:
    bin: str = "month"
    preset: str = "all"
    date_from: date | None = None
    date_to: date | None = None
    categories: list[str] = field(default_factory=list)
    contexts: list[str] = field(default_factory=list)
    target_groups: list[str] = field(default_factory=list)


@dataclass
class Segment:
    start: date
    end: date
    label: str
    category: str
    count: int


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


def _counted(query):
    """Termine the charts count: finally confirmed (Abgeschlossen) and dated."""
    return joined(query).where(Appointment.stage == STAGE_COUNTED, DATE.is_not(None))


def _held_sessions(query, filters: Filters, today: date, school_year_start_month: int):
    """Restrict a query to held Termine matching all filters; returns it with the resolved period."""
    start, end = resolve_period(
        filters.preset, today, school_year_start_month, filters.date_from, filters.date_to
    )
    query = _counted(query)
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


@dataclass
class FilterOptions:
    categories: set[str]
    contexts: list[str]
    target_groups: set[str]


def filter_options(session: Session) -> FilterOptions:
    """Categories, contexts and target groups that occur in held Termine."""

    def distinct(column):
        return {v for (v,) in session.execute(_counted(select(column).distinct())) if v}

    return FilterOptions(
        categories=distinct(CATEGORY),
        contexts=sorted(distinct(CONTEXT), key=str.casefold),
        target_groups=distinct(TARGET_GROUP),
    )


def count_by_date_and_category(session: Session, filters: Filters, today: date, school_year_start_month: int):
    """Held Termine grouped by date and category, with all filters applied."""
    query, period = _held_sessions(
        select(DATE, CATEGORY, func.count()).group_by(DATE, CATEGORY),
        filters,
        today,
        school_year_start_month,
    )
    return session.execute(query).all(), period


@dataclass
class PersonCount:
    key: str | None  # Slack user ID, else the recorded name; None for Termine without a lead
    name: str | None
    has_image: bool
    count: int


def count_by_lead(session: Session, filters: Filters, today: date, school_year_start_month: int):
    """Held Termine per lead, with all filters applied."""
    query, _ = _held_sessions(
        select(Person.slack_id, Person.name, Person.image.is_not(None), func.count()).group_by(Person.id),
        filters,
        today,
        school_year_start_month,
    )
    query = query.outerjoin(Person, Appointment.lead_id == Person.id)
    return [
        PersonCount(slack_id or name, name, bool(has_image), count)
        for slack_id, name, has_image, count in session.execute(query)
    ]


class PlaceCount(NamedTuple):
    place: str | None
    is_school: bool | None
    school_type: str | None
    municipality: str | None
    category: str | None
    count: int


def count_by_place(session: Session, filters: Filters, today: date, school_year_start_month: int):
    """Held Termine per place and category, with the place's details; all filters applied."""
    query, _ = _held_sessions(
        select(
            PLACE, Place.is_school, Place.school_type, Place.municipality, CATEGORY, func.count()
        ).group_by(Place.id, CATEGORY),
        filters,
        today,
        school_year_start_month,
    )
    return [PlaceCount(*row) for row in session.execute(query)]


def school_type_labels(session: Session) -> dict[str, str]:
    """code -> label, in chart order."""
    return dict(
        session.execute(select(SchoolType.code, SchoolType.label).order_by(SchoolType.position)).all()
    )


UNKNOWN_SCHOOL_TYPE = "Schulart unbekannt"


def by_school_type(
    rows: list[PlaceCount], labels: dict[str, str]
) -> tuple[list[tuple[str, str, int]], list[str]]:
    """Termine at schools per school type and category, and the order of the school types.

    Only places marked as schools count. A code without a label is shown as the code, a
    school without a type as "Schulart unbekannt" (last).
    """
    counts: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        if row.is_school is not True:
            continue
        label = labels.get(row.school_type, row.school_type) if row.school_type else UNKNOWN_SCHOOL_TYPE
        counts[(label, row.category or NO_CATEGORY)] += row.count
    present = {label for label, _ in counts}
    configured = [label for label in labels.values() if label in present]
    others = sorted(present - set(configured) - {UNKNOWN_SCHOOL_TYPE})
    order = configured + others + ([UNKNOWN_SCHOOL_TYPE] if UNKNOWN_SCHOOL_TYPE in present else [])
    return [(label, category, count) for (label, category), count in counts.items()], order


@dataclass
class MunicipalityCounts:
    inside: dict[str, int]  # every municipality on the map, including those with 0
    outside: int  # a municipality is set but is not on the map
    unassigned: int  # no place, or no municipality set (e.g. "Online")


def by_municipality(rows: list[PlaceCount], instance: Instance) -> MunicipalityCounts:
    names = [m.name for m in instance.map.municipalities] if instance.map else []
    inside = dict.fromkeys(names, 0)
    outside = unassigned = 0
    for row in rows:
        if row.municipality in inside:
            inside[row.municipality] += row.count
        elif row.municipality:
            outside += row.count
        else:
            unassigned += row.count
    return MunicipalityCounts(inside=inside, outside=outside, unassigned=unassigned)


@dataclass
class Totals:
    sessions: int
    participants: int
    locations: int


# Participants a held Termin adds to the total: Terminnummer 1 counts, so children in multi-session
# courses count once (rule of the R analysis); Termine without a number count nothing. A Termin made
# in the app for a continuous format (WM-114) counts at every number. Imported rows keep their
# recorded rule (they record number 1 on every Termin of a continuous format).
COUNTS_PARTICIPANTS = or_(
    Appointment.number == 1,
    and_(Appointment.source == "app", Format.continuous.is_(True), Appointment.number.is_not(None)),
)
counted_participants = case((COUNTS_PARTICIPANTS, Appointment.participants_total), else_=0)


def totals(session: Session, filters: Filters, today: date, school_year_start_month: int) -> Totals:
    """Key figures for the selection.

    Participants follow `COUNTS_PARTICIPANTS`. Places are distinct places.
    """
    query, _ = _held_sessions(
        select(
            func.count(),
            func.coalesce(func.sum(counted_participants), 0),
            func.count(func.distinct(Place.id)),
        ),
        filters,
        today,
        school_year_start_month,
    )
    sessions, participants, locations = session.execute(query).one()
    return Totals(sessions=sessions, participants=int(participants), locations=locations)


def aggregate(rows, bin_name: str, school_year_start_month: int) -> list[Segment]:
    """Sum (date, category, count) rows into calendar bins."""
    counts: dict[tuple[date, str], int] = defaultdict(int)
    for session_date, category, count in rows:
        counts[(bin_start(session_date, bin_name, school_year_start_month), category or NO_CATEGORY)] += count
    return [
        Segment(
            start=start,
            end=bin_end(start, bin_name),
            label=bin_label(start, bin_name),
            category=category,
            count=count,
        )
        for (start, category), count in sorted(counts.items())
    ]


def period_caption(first: date, last: date) -> str:
    """'Dezember 2019 – September 2026' for the x axis title."""
    first_text = f"{MONTHS[first.month - 1]} {first.year}"
    last_text = f"{MONTHS[last.month - 1]} {last.year}"
    return first_text if first_text == last_text else f"{first_text} – {last_text}"
