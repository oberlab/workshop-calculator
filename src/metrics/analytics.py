"""Time bins, period presets and the aggregations behind the charts.

What counts (held Termine, the filters, participants, places) is defined in `metrics.metrics`.
"""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from metrics.db import Appointment, AppointmentOrigin, Person, Place, SchoolType
from metrics.instance import Instance
from metrics.metrics import (
    CATEGORY,
    CONTEXT,
    COUNTS_PARTICIPANTS,
    DATE,
    PLACE,
    TARGET_GROUP,
    Filters,
    add_months,
    held,
    school_year_start,
    select_held,
)

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
    return add_months(start, {"month": 1, "quarter": 3, "year": 12, "school_year": 12}[bin_name])


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


@dataclass
class Segment:
    start: date
    end: date
    label: str
    category: str
    count: int


@dataclass
class FilterOptions:
    categories: set[str]
    contexts: list[str]
    target_groups: set[str]


def filter_options(session: Session) -> FilterOptions:
    """Categories, contexts and target groups that occur in held Termine."""

    def distinct(column):
        return {v for (v,) in session.execute(held(select(column).distinct())) if v}

    return FilterOptions(
        categories=distinct(CATEGORY),
        contexts=sorted(distinct(CONTEXT), key=str.casefold),
        target_groups=distinct(TARGET_GROUP),
    )


def count_by_date_and_category(session: Session, filters: Filters, today: date, school_year_start_month: int):
    """Held Termine grouped by date and category, with all filters applied."""
    query, period = select_held(
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
    query, _ = select_held(
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
    query, _ = select_held(
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


class OriginCount(NamedTuple):
    municipality: str | None  # None: participants without a Herkunft
    count: int


@dataclass
class MunicipalityCounts:
    inside: dict[str, int]  # every municipality on the map, including those with 0
    outside: int  # a municipality is set but is not on the map
    unassigned: int  # no place, or no municipality set (e.g. "Online"); for Herkunft: none recorded


def by_municipality(rows: Iterable[PlaceCount | OriginCount], instance: Instance) -> MunicipalityCounts:
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


def count_participants_by_origin(
    session: Session, filters: Filters, today: date, school_year_start_month: int
) -> list[OriginCount]:
    """Participants per home municipality (Herkunft) on held Termine, with all filters applied.

    Termine and participants count as in the key figures (`metrics.metrics`): held Termine only, and
    only the Termine whose participants count (`COUNTS_PARTICIPANTS`). Participants of such a Termin that
    its Herkunft does not cover (no Herkunft recorded, or fewer than the total) come back as one
    row with municipality None; they are not assigned to any municipality.
    """
    by_origin, _ = select_held(
        select(AppointmentOrigin.municipality, func.sum(AppointmentOrigin.participants)).group_by(
            AppointmentOrigin.municipality
        ),
        filters,
        today,
        school_year_start_month,
    )
    by_origin = by_origin.join(AppointmentOrigin, AppointmentOrigin.appointment_id == Appointment.id).where(
        COUNTS_PARTICIPANTS
    )
    recorded = (
        select(
            AppointmentOrigin.appointment_id,
            func.sum(AppointmentOrigin.participants).label("participants"),
        )
        .group_by(AppointmentOrigin.appointment_id)
        .subquery()
    )
    missing = func.greatest(
        func.coalesce(Appointment.participants_total, 0) - func.coalesce(recorded.c.participants, 0), 0
    )
    without_origin, _ = select_held(
        select(func.coalesce(func.sum(missing), 0)), filters, today, school_year_start_month
    )
    without_origin = without_origin.outerjoin(recorded, recorded.c.appointment_id == Appointment.id).where(
        COUNTS_PARTICIPANTS
    )
    rows = [OriginCount(municipality, int(count)) for municipality, count in session.execute(by_origin)]
    unassigned = int(session.execute(without_origin).scalar_one())
    return [*rows, OriginCount(None, unassigned)] if unassigned else rows
