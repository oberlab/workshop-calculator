"""Planning state shared by the planning pages and the sidebar of every page.

Kept apart from `metrics.web.planning` so the shared page helpers in `metrics.web.common` can show
the sidebar counters without importing the planning router.
"""

import datetime as dt

from sqlalchemy import func, select

from metrics.instance import Instance, get_instance
from metrics.planning import places, service, vocabulary
from metrics.planning.models import DURCHGEFUEHRT, CalendarOrphan, Course, get_planning_sessionmaker


def names(session) -> dict[str, str]:
    return {p.key: p.name for p in vocabulary.people(session)}


def merged_instance(session) -> Instance:
    """The instance with the places entered in planning added to the location list."""
    return places.merged(get_instance(), session)


def flags_by_course(session, courses: list[Course], today: dt.date) -> dict[int, list[service.Flag]]:
    instance = merged_instance(session)
    known = vocabulary.known(instance)
    people = names(session)
    return {c.id: service.flags(c, courses, people, instance, known, today, True) for c in courses}


def sidebar_counts(login, today: dt.date) -> dict[str, int]:
    """Open items for the sidebar badges; never fails a page (empty on any error)."""
    try:
        with get_planning_sessionmaker()() as session:
            courses = list(session.scalars(select(Course)))
            flags = flags_by_course(session, courses, today)
            tops = [service.top_severity(f) for f in flags.values()]
            counts = {
                "courses_high": tops.count("high"),
                "courses_medium": tops.count("medium"),
                "places": len(places.missing(session, merged_instance(session))),
            }
            if login.admin:
                orphans = session.scalar(
                    select(func.count())
                    .select_from(CalendarOrphan)
                    .where(CalendarOrphan.ignored_by.is_(None))
                )
                waiting = sum(1 for c in courses if c.stage == DURCHGEFUEHRT)
                counts["review"] = counts["courses_high"] + waiting + (orphans or 0)
            return counts
    except Exception:  # noqa: BLE001 - a broken prototype must not break the other pages
        return {}
