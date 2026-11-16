"""Kalender (WM-112): week and month with a lane per person or per piece of equipment, and a Termin
in a side panel.

- `GET /kalender`: `ansicht` (`woche`, default, or `monat`), `datum` (any day of the week or month
  to show, default today), and the filters `person` (an ID or `ich`), `kontext` and `phase` (empty
  for every stage but Abgesagt, or one of the course list's tabs). The month shows its calendar
  weeks from Monday to Sunday. On phones the lanes become a list per day. `spuren=material` shows a
  lane per piece of equipment instead (WM-140): what the Termine of each day need together, over all
  Termine but the cancelled ones (availability is the whole team's, so the filters do not apply).
- `GET /kalender/termin/{id}`: the summary of a Termin's course for the side panel (an htmx
  partial); opened directly, it redirects to the course page.

The Termine come from the course list's own filter (`planning._filtered`) with `von` and `bis` set
to the days shown, so the calendar and the list with the same filters show the same Termine
("Kursliste" links there). Both are Team routes (`access.py`).
"""

import datetime as dt
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from metrics import equipment, hints, lists
from metrics.db import Appointment, Context, get_sessionmaker
from metrics.planning import lanes, workflow
from metrics.planning.mine import monday_of
from metrics.web import planning
from metrics.web.common import page, templates, today
from metrics.web.mine import MONTHS

router = APIRouter()

WEEK, MONTH = "woche", "monat"
VIEWS = {WEEK: "Woche", MONTH: "Monat"}
# No phase is the list's "Alle" tab; `alle` itself is accepted but not offered twice.
PHASES = {"": "Alle außer abgesagte", **{k: v for k, v in planning.TABS if k != planning.ALL}}
FILTERS = ("person", "kontext", "phase")
PEOPLE_LANES, EQUIPMENT_LANES = "personen", "material"
LANES = {PEOPLE_LANES: "Personen", EQUIPMENT_LANES: "Material"}


def stages(phase: str) -> tuple[str, ...]:
    if phase not in PHASES and phase != planning.ALL:
        raise HTTPException(400, "Unbekannte Phase")
    return planning._stages(phase or planning.ALL)


def span(view: str, day: dt.date) -> tuple[dt.date, dt.date]:
    """First and last day shown: a week from Monday, or the calendar weeks of the month."""
    if view == MONTH:
        first = day.replace(day=1)
        last = (first + dt.timedelta(days=32)).replace(day=1) - dt.timedelta(days=1)
        return monday_of(first), monday_of(last) + dt.timedelta(days=6)
    monday = monday_of(day)
    return monday, monday + dt.timedelta(days=6)


def termine(session: Session, params, me, start: dt.date, end: dt.date) -> list[Appointment]:
    """The Termine shown: the course list's filters plus the days shown and the phase."""
    filters = {"person": params.get("person") or "", "kontext": params.get("kontext") or ""}
    filters |= {"von": start.isoformat(), "bis": end.isoformat()}
    stmt = planning._filtered(session, filters, me).where(
        Appointment.stage.in_(stages(params.get("phase") or ""))
    )
    return list(session.scalars(planning._load_appointments(stmt)))


def _shift(view: str, day: dt.date, step: int) -> dt.date:
    if view == MONTH:
        month = day.year * 12 + day.month - 1 + step
        return dt.date(month // 12, month % 12 + 1, 1)
    return day + dt.timedelta(weeks=step)


def _title(view: str, day: dt.date, start: dt.date, end: dt.date) -> str:
    if view == MONTH:
        return f"{MONTHS[day.month - 1]} {day.year}"
    return f"KW {start.isocalendar().week} · {start:%d.%m.}–{end:%d.%m.%Y}"


@router.get("/kalender", response_class=HTMLResponse)
def calendar(request: Request):
    params = request.query_params
    view = params.get("ansicht") or WEEK
    if view not in VIEWS:
        raise HTTPException(400, "Unbekannte Ansicht")
    lanes_by = params.get("spuren") or PEOPLE_LANES
    if lanes_by not in LANES:
        raise HTTPException(400, "Unbekannte Spuren")
    day = planning._date(params.get("datum")) or today()
    start, end = span(view, day)
    kept = {k: params[k] for k in FILTERS if params.get(k)}
    if lanes_by == EQUIPMENT_LANES:
        kept["spuren"] = lanes_by

    def link(**changes) -> str:
        query = {"ansicht": view, "datum": day.isoformat(), **kept, **changes}
        return "/kalender?" + urlencode({k: v for k, v in query.items() if v})

    with get_sessionmaker()() as session:
        me = planning._me(session, request)
        person = params.get("person") or ""
        if lanes_by == EQUIPMENT_LANES:
            shown = equipment.booked_between(session, start, end)
            weeks = lanes.equipment_weeks(shown, start, end, equipment.listed(session))
        else:
            shown = termine(session, params, me, start, end)
            only = (me.id if me else -1) if person == "ich" else planning._int(person)
            weeks = lanes.weeks(shown, start, end, only)
        found = planning.open_hints(session, [t.id for t in shown])
        list_query = {
            "phase": params.get("phase") or planning.ALL,
            "person": person,
            "kontext": params.get("kontext") or "",
            "von": start.isoformat(),
            "bis": end.isoformat(),
        }
        return page(
            request,
            "kalender.html",
            {
                "view": view,
                "views": [(key, label, link(ansicht=key)) for key, label in VIEWS.items()],
                "lanes_by": lanes_by,
                "lane_kinds": [
                    (key, label, link(spuren=key if key != PEOPLE_LANES else ""))
                    for key, label in LANES.items()
                ],
                "day": day,
                "title": _title(view, day, start, end),
                "weeks": weeks,
                "count": len(shown),
                "today": today(),
                "prev": link(datum=_shift(view, day, -1).isoformat()),
                "next": link(datum=_shift(view, day, 1).isoformat()),
                "now": link(datum=""),
                "params": {k: params.get(k, "") for k in FILTERS},
                "phases": PHASES,
                "people": lists.choices(session, lists.PEOPLE),
                "contexts": list(
                    session.scalars(
                        select(Context)
                        .where(Context.status.in_(lists.ACTIVE_STATUSES))
                        .order_by(Context.name)
                    )
                ),
                "me": me,
                "top": {i: hs[0] for i, hs in found.items()},
                "badge": hints.BADGE,
                "titles": {t.id: workflow.title(t) for t in shown},
                # The course list for the same Termine: no phase here is its "Alle" tab.
                "list_href": "/planung?" + urlencode({k: v for k, v in list_query.items() if v}),
            },
        )


@router.get("/kalender/termin/{appointment_id:int}", response_class=HTMLResponse)
def termin_panel(appointment_id: int, request: Request):
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(f"/planung/termin/{appointment_id}", status_code=303)
    with get_sessionmaker()() as session:
        stmt = planning._load_appointments(select(Appointment).where(Appointment.id == appointment_id))
        t = session.scalars(stmt).first()
        if t is None:
            raise HTTPException(404, "Termin nicht gefunden")
        return templates.TemplateResponse(
            request,
            "_kalender_termin.html",
            {
                "t": t,
                "title": workflow.title(t),
                "termine": len(t.course.appointments),
                "open": planning.open_hints(session, [t.id]).get(t.id, []),
                "badge": hints.BADGE,
            },
        )
