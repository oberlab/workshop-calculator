"""Calendar lanes (WM-112): Termine laid out as one row per person and one column per day.

`weeks(appointments, start, end)` returns one `Week` per calendar week from `start` (a Monday) to
`end` (a Sunday). In every week each person with a Termin gets a lane; a Termin sits in the lane
of its lead and in the lane of each support person (marked as support), and Termine without a team
in a lane of their own. The cells of a lane are the seven days, each a list of `Item`s sorted by
time. No pixel positions: the page draws the lanes with a CSS grid (the strict CSP allows no inline
styles). The Team page (WM-131) reuses the lanes and adds absences and workload.
"""

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass, field

from metrics.db import Appointment, Person

LEAD = "lead"
SUPPORT = "support"
NO_TEAM = "Ohne Team"


@dataclass(frozen=True)
class Item:
    t: Appointment
    role: str | None  # LEAD, SUPPORT, or None in the "Ohne Team" lane


@dataclass
class Lane:
    label: str
    person: Person | None
    days: list[list[Item]] = field(default_factory=lambda: [[] for _ in range(7)])

    @property
    def count(self) -> int:
        return len({item.t.id for day in self.days for item in day})


@dataclass
class Week:
    monday: dt.date
    lanes: list[Lane] = field(default_factory=list)

    @property
    def days(self) -> list[dt.date]:
        return [self.monday + dt.timedelta(days=i) for i in range(7)]

    @property
    def number(self) -> int:
        return self.monday.isocalendar().week

    def by_day(self) -> list[tuple[dt.date, list[Appointment]]]:
        """Each day's Termine once, sorted by time (the phone list)."""
        found: dict[dt.date, dict[int, Appointment]] = {d: {} for d in self.days}
        for lane in self.lanes:
            for day, items in zip(self.days, lane.days, strict=True):
                for item in items:
                    found[day].setdefault(item.t.id, item.t)
        return [(d, sorted(ts.values(), key=sort_key)) for d, ts in found.items() if ts]


def sort_key(t: Appointment):
    return (t.date or dt.date.max, t.start_time or dt.time.max, t.id or 0)


def weeks(
    appointments: Iterable[Appointment], start: dt.date, end: dt.date, only: int | None = None
) -> list[Week]:
    """The lanes for the weeks from `start` (a Monday) to `end`. With `only`, the lane of that one
    person (the person filter); otherwise every person with a Termin in the week."""
    result = [Week(start + dt.timedelta(weeks=i)) for i in range(((end - start).days // 7) + 1)]
    for t in sorted(appointments, key=sort_key):
        if t.date is None or not start <= t.date <= end:
            continue
        week = result[(t.date - start).days // 7]
        weekday = t.date.weekday()
        people = ([(t.lead, LEAD)] if t.lead else []) + [(p, SUPPORT) for p in t.support]
        if only is not None:
            people = [(p, role) for p, role in people if p.id == only]
        if not people and only is None:
            _lane(week, NO_TEAM, None).days[weekday].append(Item(t, None))
        for person, role in people:
            _lane(week, person.name, person).days[weekday].append(Item(t, role))
    for week in result:
        week.lanes.sort(key=lambda lane: (lane.person is None, lane.label.casefold()))
    return result


def _lane(week: Week, label: str, person: Person | None) -> Lane:
    for lane in week.lanes:
        if lane.person is person and lane.label == label:
            return lane
    lane = Lane(label, person)
    week.lanes.append(lane)
    return lane
