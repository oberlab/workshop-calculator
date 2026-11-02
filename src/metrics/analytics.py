"""Time bins, period presets and the sessions-over-time aggregation."""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from metrics.db import STATUS_DONE, SessionRow

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


def _held_sessions(query, filters: Filters, today: date, school_year_start_month: int):
    """Restrict a query to held sessions matching all filters; returns it with the resolved period."""
    start, end = resolve_period(
        filters.preset, today, school_year_start_month, filters.date_from, filters.date_to
    )
    query = query.where(SessionRow.status == STATUS_DONE, SessionRow.date.is_not(None))
    if start:
        query = query.where(SessionRow.date >= start)
    if end:
        query = query.where(SessionRow.date <= end)
    if filters.categories:
        query = query.where(SessionRow.category.in_(filters.categories))
    if filters.contexts:
        query = query.where(SessionRow.context.in_(filters.contexts))
    if filters.target_groups:
        query = query.where(SessionRow.target_group.in_(filters.target_groups))
    return query, (start, end)


def count_by_date_and_category(session: Session, filters: Filters, today: date, school_year_start_month: int):
    """Held sessions grouped by date and category, with all filters applied."""
    query, period = _held_sessions(
        select(SessionRow.date, SessionRow.category, func.count()).group_by(
            SessionRow.date, SessionRow.category
        ),
        filters,
        today,
        school_year_start_month,
    )
    return session.execute(query).all(), period


def count_by_instructor(session: Session, filters: Filters, today: date, school_year_start_month: int):
    """Held sessions per recorded instructor value (first name or Slack ID), with all filters applied."""
    query, _ = _held_sessions(
        select(SessionRow.instructor, func.count()).group_by(SessionRow.instructor),
        filters,
        today,
        school_year_start_month,
    )
    return session.execute(query).all()


@dataclass
class Totals:
    sessions: int
    participants: int
    locations: int


def totals(session: Session, filters: Filters, today: date, school_year_start_month: int) -> Totals:
    """Key figures for the selection.

    Participants are counted at session number 1 only, so children in multi-session courses
    count once (rule of the R analysis). Entries without a session number are not counted.
    Locations are distinct recorded names.
    """
    first_session = case((SessionRow.session_number == 1, SessionRow.participants_total), else_=0)
    query, _ = _held_sessions(
        select(
            func.count(),
            func.coalesce(func.sum(first_session), 0),
            func.count(func.distinct(SessionRow.location)),
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
