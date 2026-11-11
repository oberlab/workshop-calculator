"""Suggestions for the planning form, read (never written) from the metrics database.

Earlier course names, contexts and places, most frequent first, plus what is already in the
planning database and the location list. Cached for five minutes. Without a reachable metrics
database the planning values alone are used.
"""

import re
import time
from collections import Counter

from sqlalchemy import delete, func, select
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


SLACK_USER = re.compile(r"^[UW][A-Z0-9]{6,}$")
DEMO_PEOPLE = [("demo-a", "Alex"), ("demo-b", "Kim"), ("demo-c", "Sam"), ("demo-d", "Robin")]


def first_names(rows: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """(key, first name); people sharing a first name keep the initial of their last name."""
    split = [(key, (name.split() or [key])) for key, name in rows]
    firsts = [parts[0] for _, parts in split]
    return [
        (key, parts[0] if firsts.count(parts[0]) == 1 or len(parts) == 1 else f"{parts[0]} {parts[-1][0]}.")
        for key, parts in split
    ]


def team_from_slack() -> list[tuple[str, str]]:
    """The instructors that are Slack users, by first name (legacy names without Slack are left out)."""
    try:
        with get_sessionmaker()() as session:
            rows = session.execute(select(Instructor.key, Instructor.name)).all()
    except SQLAlchemyError:
        rows = []
    slack = [(key, name) for key, name in rows if SLACK_USER.match(key)]
    return first_names(sorted(slack, key=lambda row: row[1].casefold())) or DEMO_PEOPLE


def ensure_people(planning: Session) -> None:
    """Fill an empty people list from the Slack users among the instructors."""
    if planning.scalar(select(func.count()).select_from(Person)):
        return
    for key, name in team_from_slack():
        planning.add(Person(key=key, name=name, active=True))
    planning.commit()


def rebuild_people(planning: Session) -> None:
    """Replace the people list with the current Slack users; login links are kept."""
    logins = {p.key: p.login for p in planning.scalars(select(Person)) if p.login}
    planning.execute(delete(Person))
    for key, name in team_from_slack():
        planning.add(Person(key=key, name=name, active=True, login=logins.get(key)))
    planning.commit()


def people(planning: Session, active_only: bool = False) -> list[Person]:
    query = select(Person).order_by(Person.name)
    if active_only:
        query = query.where(Person.active.is_(True))
    return list(planning.scalars(query))
