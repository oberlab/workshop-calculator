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

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from metrics import hints, lists
from metrics.db import (
    ABGESAGT,
    ABGESCHLOSSEN,
    ACTIVE_STATUSES,
    BESTAETIGT,
    DURCHGEFUEHRT,
    GEPLANT,
    Context,
    Proposal,
    get_sessionmaker,
)
from metrics.web.common import TIMEZONE, current_login, page

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
        context = {
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
