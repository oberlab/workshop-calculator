"""Suggestions for the planning form, read (never written) from the metrics database.

Earlier course names, contexts and places, most frequent first, plus what is already in the
planning database and the location list. Cached for five minutes. Without a reachable metrics
database the planning values alone are used.
"""

import time
from collections import Counter

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from metrics.db import Instructor, SessionRow, get_sessionmaker
from metrics.instance import Instance
from metrics.planning.models import Course, Person

FIELDS = ("course_name", "context", "location", "category")
CACHE_SECONDS = 300
_cache: dict[str, tuple[float, object]] = {}


def _cached(key: str, load):
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
        return hit[1]
    value = load()
    _cache[key] = (time.monotonic(), value)
    return value


def clear_cache() -> None:
    _cache.clear()


def _metrics_counts() -> dict[str, Counter]:
    counts = {name: Counter() for name in FIELDS}
    try:
        with get_sessionmaker()() as session:
            for name in FIELDS:
                column = getattr(SessionRow, name)
                for value, n in session.execute(
                    select(column, func.count()).where(column.is_not(None)).group_by(column)
                ):
                    counts[name][value] = n
    except SQLAlchemyError:
        pass
    return counts


def suggestions(planning: Session, instance: Instance) -> dict[str, list[str]]:
    """field -> values, most frequent first."""
    counts = {name: Counter(c) for name, c in _cached("metrics", _metrics_counts).items()}
    for course in planning.scalars(select(Course)):
        for name in FIELDS:
            if value := getattr(course, name):
                counts[name][value] += 1
    for place in {*instance.locations, *instance.location_addresses}:
        counts["location"][place] += 0
    for category in instance.theme.category_order:
        counts["category"][category] += 0
    return {
        name: [v for v, _ in sorted(c.items(), key=lambda kv: (-kv[1], kv[0].casefold()))]
        for name, c in counts.items()
    }


def known(instance: Instance) -> dict[str, set[str]]:
    """Values of the clean metrics data, for the spelling warnings."""
    counts = _cached("metrics", _metrics_counts)
    result = {name: set(counts[name]) for name in FIELDS}
    result["location"] |= set(instance.locations) | set(instance.location_addresses)
    return result


def municipalities(planning: Session, instance: Instance) -> list[str]:
    names = [m.name for m in instance.map.municipalities] if instance.map else []
    for course in planning.scalars(select(Course)):
        names += [m.municipality for m in course.municipalities]
    return sorted(set(names), key=str.casefold)


def ensure_people(planning: Session) -> None:
    """Fill an empty people list from the metrics instructors (Slack users first)."""
    if planning.scalar(select(func.count()).select_from(Person)):
        return
    try:
        with get_sessionmaker()() as session:
            rows = session.execute(select(Instructor.key, Instructor.name)).all()
    except SQLAlchemyError:
        rows = []
    if not rows:
        rows = [
            ("demo-a", "Alex Beispiel"),
            ("demo-b", "Kim Muster"),
            ("demo-c", "Sam Probe"),
            ("demo-d", "Robin Test"),
        ]
    for key, name in rows:
        planning.add(Person(key=key, name=name, active=True))
    planning.commit()


def people(planning: Session, active_only: bool = False) -> list[Person]:
    query = select(Person).order_by(Person.name)
    if active_only:
        query = query.where(Person.active.is_(True))
    return list(planning.scalars(query))
