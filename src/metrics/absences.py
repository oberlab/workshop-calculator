"""Reading the absences (WM-142) for the Team page, the calendar, the pickers and the hints.

The rows come from Kimai (`metrics.kimai`); withdrawn ones (no longer in Kimai) never count.
Privacy: the kind (Urlaub, Krank, …) is shown only to the person themselves; everyone else, and
every hint text, says "abwesend".
"""

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from metrics.db import Absence

ABSENT = "abwesend"


@dataclass(frozen=True)
class Away:
    kind: str
    half_day: bool = False


def by_person(
    session: Session, start: dt.date, end: dt.date, people: Iterable[int] | None = None
) -> dict[int, dict[dt.date, Away]]:
    """person ID -> day -> the absence, from `start` to `end`, for `people` (default everyone)."""
    stmt = select(Absence).where(Absence.day.between(start, end), Absence.withdrawn_at.is_(None))
    if people is not None:
        stmt = stmt.where(Absence.person_id.in_(list(people)))
    found: dict[int, dict[dt.date, Away]] = {}
    for row in session.scalars(stmt.order_by(Absence.day, Absence.id)):
        days = found.setdefault(row.person_id, {})
        current = days.get(row.day)
        # Two absences on one day (a half day of each): the whole day is away.
        if current is not None:
            days[row.day] = Away(current.kind, current.half_day and row.half_day)
        else:
            days[row.day] = Away(row.kind, row.half_day)
    return found


def label(away: Away, own: bool) -> str:
    """What the page shows: the kind for the person themselves, "abwesend" for everyone else."""
    text = away.kind if own else ABSENT
    return f"{text} (halbtags)" if away.half_day else text


def labels(found: dict[int, dict[dt.date, Away]], own: int | None = None) -> dict[int, dict[dt.date, str]]:
    """`by_person` as texts for the person `own` (the viewer)."""
    return {pid: {d: label(a, pid == own) for d, a in days.items()} for pid, days in found.items()}
