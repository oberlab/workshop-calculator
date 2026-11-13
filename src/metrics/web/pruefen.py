""" "Prüfen" (admins, WM-113): one queue for proposed list entries, proposed corrections and hints.

`GET /pruefen` shows three sections: list entries waiting for a decision (taken on their Stammdaten
page), proposed corrections (WM-132 adds them; until then the section is empty) and the stored
hints with filters `stufe`, `regel`, `kontext`, `von`, `bis` and `status` (offen, akzeptiert, alle).
`POST /pruefen/akzeptieren` accepts a warning with a reason (`termin`, `regel`, `schluessel` for the
finding, `grund`); blocking
hints cannot be accepted. Both are admin routes in `access.py`.
"""

import datetime as dt
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from metrics import courses, hints, lists
from metrics.db import (
    ABGESAGT,
    ABGESCHLOSSEN,
    ACTIVE_STATUSES,
    BESTAETIGT,
    CAL_OK,
    DURCHGEFUEHRT,
    GEPLANT,
    CalendarOrphan,
    Context,
    Place,
    Proposal,
    get_sessionmaker,
)
from metrics.planning import caldav
from metrics.web.common import TIMEZONE, current_login, page, public_base, today

router = APIRouter()

STATUS_FILTERS = {hints.OPEN: "Offen", hints.ACCEPTED_ONLY: "Akzeptiert", hints.EVERY: "Alle"}
STAGE_PILLS = {
    GEPLANT: "geplant",
    BESTAETIGT: "bestaetigt",
    DURCHGEFUEHRT: "durchgefuehrt",
    ABGESCHLOSSEN: "abgeschlossen",
    ABGESAGT: "abgesagt",
}
SOURCES = {"baseline": "Altdaten", "slack": "aus Slack"}
FILTER_KEYS = ("stufe", "regel", "kontext", "von", "bis", "status")
LIMIT = 200


@dataclass(frozen=True)
class Waiting:
    """A proposed list entry for the first section."""

    proposal: Proposal
    kind: lists.Kind
    entry: object
    label: str
    used: int


def _date(value: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _filter(params) -> hints.QueueFilter:
    level = params.get("stufe")
    rule_id = params.get("regel")
    status = params.get("status")
    context = params.get("kontext") or ""
    return hints.QueueFilter(
        level=level if level in hints.LEVELS else None,
        rule=rule_id if rule_id in hints.RULES else None,
        context_id=int(context) if context.isdigit() else None,
        date_from=_date(params.get("von")),
        date_to=_date(params.get("bis")),
        status=status if status in STATUS_FILTERS else hints.OPEN,
    )


def _filter_query(params) -> str:
    """The filters as a query string (only the known keys), to come back to after an action."""
    return urlencode([(k, params[k]) for k in FILTER_KEYS if params.get(k)])


def _waiting(session) -> list[Waiting]:
    by_table = {kind.model.__tablename__: kind for kind in lists.KINDS.values()}
    proposals = session.scalars(
        select(Proposal)
        .where(Proposal.kind == "eintrag", Proposal.decision.is_(None))
        .order_by(Proposal.proposed_at, Proposal.id)
    )
    found = []
    for proposal in proposals:
        kind = by_table.get(proposal.target_table)
        entry = lists.get(session, kind, proposal.target_id) if kind and proposal.target_id else None
        if entry is None:
            continue
        usage = lists.usage(session, kind, [entry.id]).get(entry.id, lists.Usage())
        found.append(
            Waiting(
                proposal,
                kind,
                entry,
                lists.label(entry),
                usage.appointments if kind is lists.PEOPLE else usage.courses,
            )
        )
    return found


def _queue_page(request: Request, *, error: str | None = None, status_code: int = 200) -> HTMLResponse:
    params = request.query_params
    f = _filter(params)
    with get_sessionmaker()() as session:
        rows, total = hints.queue(session, f, LIMIT)
        corrections = list(
            session.scalars(
                select(Proposal)
                .where(Proposal.kind == "korrektur", Proposal.decision.is_(None))
                .order_by(Proposal.proposed_at, Proposal.id)
            )
        )
        contexts = list(
            session.scalars(select(Context).where(Context.status.in_(ACTIVE_STATUSES)).order_by(Context.name))
        )
        by_rule = hints.counts_by_rule(session)
        orphans = list(session.scalars(select(CalendarOrphan).order_by(CalendarOrphan.starts_at)))
        context = {
            "orphans": [o for o in orphans if o.ignored_by is None],
            "ignored": [o for o in orphans if o.ignored_by is not None],
            "calendar_on": caldav.get_calendar() is not None,
            "waiting": _waiting(session),
            "corrections": corrections,
            "rows": rows,
            "total": total,
            "limit": LIMIT,
            "filter": f,
            "filter_query": _filter_query(params),
            "rules": hints.RULES,
            "by_rule": by_rule,
            "levels": hints.LEVELS,
            "badge": hints.BADGE,
            "contexts": contexts,
            "status_filters": STATUS_FILTERS,
            "stage_pills": STAGE_PILLS,
            "sources": SOURCES,
            "titles": {r.appointment.id: hints.title(r.appointment) for r in rows},
            "timezone": TIMEZONE,
            "message": params.get("hinweis"),
            "error": error,
        }
        return page(request, "pruefen.html", context, status_code=status_code)


@router.get("/pruefen", response_class=HTMLResponse)
def queue_page(request: Request):
    return _queue_page(request)


@router.post("/pruefen/akzeptieren", response_class=HTMLResponse)
async def accept(request: Request):
    form = {k: v[-1] for k, v in parse_qs((await request.body()).decode(errors="replace")).items()}
    appointment_id = form.get("termin") or ""
    rule_id = form.get("regel") or ""
    back = _filter_query({k: v[-1] for k, v in parse_qs(form.get("zurueck") or "").items()})
    try:
        if not appointment_id.isdigit() or rule_id not in hints.RULES:
            raise hints.HintError("Unbekannter Hinweis.")
        with get_sessionmaker()() as session, session.begin():
            hints.accept(
                session,
                int(appointment_id),
                rule_id,
                form.get("grund") or "",
                current_login(request).user,
                key=form.get("schluessel") or None,
            )
    except hints.HintError as error:
        return _queue_page(request, error=str(error), status_code=422)
    query = f"{back}&" if back else ""
    return RedirectResponse(f"/pruefen?{query}hinweis={quote('Akzeptiert.')}", status_code=303)


# --- Calendar events without a Termin (moved from the prototype's review page, WM-110) -----------


def _back(message: str) -> RedirectResponse:
    return RedirectResponse(f"/pruefen?hinweis={quote(message)}#kalender", status_code=303)


async def _uid(request: Request) -> str:
    form = parse_qs((await request.body()).decode(errors="replace"))
    return (form.get("uid") or [""])[0]


@router.post("/pruefen/kalender/pruefen")
def check_calendar(request: Request):
    """Run the calendar check now (the scheduler does it every 15 minutes)."""
    calendar = caldav.get_calendar()
    if calendar is None:
        return _back("Kein Kalender eingerichtet.")
    try:
        with get_sessionmaker()() as session, session.begin():
            result = caldav.check(session, calendar, today(), public_base(request))
    except caldav.CalendarError as error:
        return _back(str(error))
    return _back(f"Kalender geprüft: {result.checked} Einträge, {result.orphans} ohne Termin.")


@router.post("/pruefen/kalender/ignorieren")
async def ignore_event(request: Request):
    uid = await _uid(request)
    with get_sessionmaker()() as session, session.begin():
        orphan = session.get(CalendarOrphan, uid)
        if orphan is None:
            raise HTTPException(404)
        orphan.ignored_by, orphan.ignored_at = current_login(request).user, dt.datetime.now(dt.UTC)
    return _back("Wird nicht mehr gemeldet.")


@router.post("/pruefen/kalender/kurs")
async def course_from_event(request: Request):
    """A Geplant course from a calendar event; the event is linked, not changed."""
    uid = await _uid(request)
    by = current_login(request).user
    with get_sessionmaker()() as session, session.begin():
        orphan = session.get(CalendarOrphan, uid)
        if orphan is None:
            raise HTTPException(404)
        title, _, place_name = orphan.summary.partition(" · ")
        place = session.scalars(
            select(Place).where(
                Place.name == (orphan.location or place_name), Place.status.in_(ACTIVE_STATUSES)
            )
        ).first()
        course = courses.create_course(
            session, by=by, place_id=place.id if place else None, note=title or None
        )
        t = courses.add_appointment(
            session,
            course,
            by=by,
            date=orphan.starts_at.date(),
            start_time=orphan.starts_at.time(),
            end_time=orphan.ends_at.time(),
        )
        t.calendar_uid, t.calendar_state = uid, CAL_OK
        lists._log(
            session,
            "appointments",
            t.id,
            "calendar_uid",
            None,
            uid,
            by,
            f"aus Kalendereintrag „{orphan.summary}“",
        )
        session.delete(orphan)  # now linked to a Termin
        hints.refresh(session, [t.id])
        note = quote("Aus dem Kalendereintrag angelegt; bitte ergänzen.")
        target = f"/planung/{course.id}?termin={t.id}&hinweis={note}"
    return RedirectResponse(target, status_code=303)
