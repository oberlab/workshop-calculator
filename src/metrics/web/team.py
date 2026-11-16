"""Team (WM-131, `PROPOSAL.md` 4.3): who is planned when, how full the coming weeks are, who is free.

`GET /team` (Team route, `access.py`), read only:
- `ansicht` (`woche`, default, or `monat`) and `datum` (any day of the week or month, default
  today): the calendar lanes, one per active team member and anyone else with a Termin, every
  stage but Abgesagt (the calendar's lanes and side panel, `_lanes.html`); absences as grey
  blocks once WM-142 fills `workload.absences`. Below them the equipment lanes of the same days
  (WM-140), when there is a Material list.
- The workload table per person and calendar week: four weeks from the week shown, or the weeks
  of the month shown, with the estimated working hours (`metrics.planning.workload`) against the
  person's weekly hours, and the formula in words.
- "Wer ist frei?": `frei` (a date, default the day shown), `von` and `bis` (optional times): the
  team pickers' people without a Termin or absence then (`workload.who_is_free`).
"""

import datetime as dt
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from metrics import equipment, hints, lists
from metrics.db import get_sessionmaker
from metrics.planning import lanes, workflow, workload
from metrics.web import planning
from metrics.web.common import page, today
from metrics.web.kalender import MONTH, VIEWS, WEEK, _shift, _title, span

router = APIRouter()


def _time(value: str | None) -> dt.time | None:
    try:
        return dt.time.fromisoformat(value) if value else None
    except ValueError:
        return None


@router.get("/team", response_class=HTMLResponse)
def team(request: Request):
    params = request.query_params
    view = params.get("ansicht") or WEEK
    if view not in VIEWS:
        raise HTTPException(400, "Unbekannte Ansicht")
    day = planning._date(params.get("datum")) or today()
    start, end = span(view, day)
    # The table: four weeks from the week shown, or the weeks of the month shown.
    weeks = (end - start).days // 7 + 1 if view == MONTH else workload.WEEKS
    table_end = start + dt.timedelta(weeks=weeks, days=-1)
    free_day = planning._date(params.get("frei")) or day
    free_start, free_end = _time(params.get("von")), _time(params.get("bis"))
    if free_start and free_end and free_end <= free_start:
        free_start = free_end = None  # an impossible span: the whole day

    def link(**changes) -> str:
        query = {"ansicht": view, "datum": day.isoformat(), **changes}
        return "/team?" + urlencode({k: v for k, v in query.items() if v})

    with get_sessionmaker()() as session:
        team_people = lists.choices(session, lists.PEOPLE)
        shown = workload.termine(session, start, max(end, table_end))
        in_lanes = [t for t in shown if start <= t.date <= end]
        away = workload.absences(session, start, max(end, table_end))
        found = planning.open_hints(session, [t.id for t in in_lanes])
        rows = workload.table(session, team_people, start, weeks, shown)
        # Equipment lanes (WM-140) for the same days, when there is a Material list.
        items = equipment.listed(session)
        booked = equipment.booked_between(session, start, end) if items else []
        found |= planning.open_hints(session, [t.id for t in booked if t.id not in found])
        return page(
            request,
            "team.html",
            {
                "view": view,
                "views": [(key, label, link(ansicht=key)) for key, label in VIEWS.items()],
                "day": day,
                "title": _title(view, day, start, end),
                "weeks": lanes.weeks(in_lanes, start, end, people=team_people, absences=away),
                "equipment_weeks": lanes.equipment_weeks(booked, start, end, items) if items else [],
                "count": len(in_lanes),
                "today": today(),
                "prev": link(datum=_shift(view, day, -1).isoformat()),
                "next": link(datum=_shift(view, day, 1).isoformat()),
                "now": link(datum=""),
                "rows": rows,
                "week_labels": [f"KW {m.isocalendar().week}" for m in workload.mondays(start, weeks)],
                # What a full bar is for people without weekly hours: the busiest week shown.
                "scale": max([float(c.total) for r in rows for c in r.cells] + [1.0]),
                "formula": workload.formula(),
                "free": workload.who_is_free(session, free_day, free_start, free_end),
                "top": {i: hs[0] for i, hs in found.items()},
                "badge": hints.BADGE,
                "titles": {t.id: workflow.title(t) for t in in_lanes + booked},
                "hours": workload.number,
            },
        )
