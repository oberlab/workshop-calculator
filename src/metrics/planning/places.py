"""Places in planning: which ones still need an address, and the location list they extend.

The instance's `seeds/locations.csv` is the location list (read-only here). A place typed into
a course that is not in it needs an address (street, ZIP code, town) and the school question;
both are entered on the "Orte" page and kept in the planning database.
"""

from dataclasses import replace

from sqlalchemy import select
from sqlalchemy.orm import Session

from metrics.instance import Instance
from metrics.planning.models import ABGESAGT, Course, Place


def places(session: Session) -> dict[str, Place]:
    return {p.name: p for p in session.scalars(select(Place).order_by(Place.name))}


def merged(instance: Instance, session: Session) -> Instance:
    """The instance with the planning places added to the location list."""
    extra = places(session).values()
    return replace(
        instance,
        locations={**instance.locations, **{p.name: p.school for p in extra if p.school is not None}},
        location_school_types={
            **instance.location_school_types,
            **{p.name: p.school_type for p in extra if p.school and p.school_type},
        },
        location_addresses={
            **instance.location_addresses,
            **{p.name: p.address for p in extra if p.complete},
        },
    )


def needs_address(instance: Instance, name: str | None) -> bool:
    """True for a place that is neither in the location list nor entered with a full address.

    Places of the list without an address (online, private) are left alone on purpose.
    """
    return bool(name) and name not in instance.location_addresses


def missing(session: Session, instance: Instance) -> list[tuple[str, int]]:
    """(place, number of courses) for every used place that still needs an address."""
    counts: dict[str, int] = {}
    for course in session.scalars(select(Course).where(Course.stage != ABGESAGT)):
        if needs_address(instance, course.location):
            counts[course.location] = counts.get(course.location, 0) + 1
    return sorted(counts.items(), key=lambda item: item[0].casefold())
