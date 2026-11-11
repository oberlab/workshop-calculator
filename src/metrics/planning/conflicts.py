"""Courses at the same time: who is double-booked, which courses collide.

| severity | case |
| high     | a person is in two courses whose times overlap |
| medium   | times overlap without a shared person, the same place overlaps, or a shared person on
|          | the same day where a time is missing |
| low      | another course on the same day, or any conflict only on an alternative date |
"""

import datetime as dt
from dataclasses import dataclass

from metrics.planning.models import BESTAETIGT, DURCHGEFUEHRT, GEPLANT, Course

HIGH, MEDIUM, LOW = "high", "medium", "low"
SEVERITY_ORDER = {HIGH: 0, MEDIUM: 1, LOW: 2}
SEVERITY_LABELS = {HIGH: "hoch", MEDIUM: "mittel", LOW: "niedrig"}

ACTIVE = (GEPLANT, BESTAETIGT)


@dataclass(frozen=True)
class Conflict:
    severity: str
    other_id: int
    other_title: str
    date: dt.date
    reason: str
    alternative: bool  # only on an alternative date


def _dates(course: Course) -> list[tuple[dt.date, bool]]:
    """(date, is_alternative) for the main date and the alternatives."""
    result = [(course.date, False)] if course.date else []
    return result + [(a.date, True) for a in course.alternatives if a.date != course.date]


def _overlap(a: Course, b: Course) -> bool | None:
    """True/False if both have start and end, None if a time is missing."""
    if not (a.start_time and a.end_time and b.start_time and b.end_time):
        return None
    return a.start_time < b.end_time and b.start_time < a.end_time


def is_active(course: Course, today: dt.date) -> bool:
    return course.stage in ACTIVE or (course.stage == DURCHGEFUEHRT and course.date == today)


def conflicts_for(course: Course, others: list[Course], names: dict[str, str]) -> list[Conflict]:
    found = []
    for other in others:
        if other.id == course.id:
            continue
        for day, alt in _dates(course):
            for other_day, other_alt in _dates(other):
                if day != other_day:
                    continue
                severity, reason = _judge(course, other, names)
                alternative = alt or other_alt
                if alternative:
                    severity = LOW
                found.append(Conflict(severity, other.id, other.title, day, reason, alternative))
    return sorted(found, key=lambda c: (SEVERITY_ORDER[c.severity], c.date))


def _judge(a: Course, b: Course, names: dict[str, str]) -> tuple[str, str]:
    shared = sorted(names.get(k, k) for k in a.all_keys & b.all_keys)
    overlap = _overlap(a, b)
    same_place = a.location and a.location == b.location
    who = ", ".join(shared)
    if shared and overlap:
        return HIGH, f"{who} gleichzeitig eingeplant"
    if shared and overlap is None:
        return MEDIUM, f"{who} am selben Tag eingeplant (Uhrzeit fehlt)"
    if overlap and same_place:
        return MEDIUM, f"gleichzeitig am selben Ort ({a.location})"
    if overlap:
        return MEDIUM, "zur selben Zeit"
    if shared:
        return LOW, f"{who} am selben Tag, Zeiten überschneiden sich nicht"
    return LOW, "am selben Tag"


def busy_people(
    day: dt.date, start: dt.time | None, end: dt.time | None, courses: list[Course], exclude: int | None
) -> dict[str, str]:
    """person key -> title of the course that keeps them busy at that time (main dates only)."""
    busy: dict[str, str] = {}
    for course in courses:
        if course.id == exclude or course.date != day:
            continue
        if start and end and course.start_time and course.end_time:
            if not (start < course.end_time and course.start_time < end):
                continue
        label = course.title
        if course.start_time:
            label += f" ({course.start_time:%H:%M}"
            label += f"–{course.end_time:%H:%M})" if course.end_time else ")"
        for key in course.all_keys:
            busy.setdefault(key, label)
    return busy
