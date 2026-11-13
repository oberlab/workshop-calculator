"""Helpers shared by the routers: filters from the query string, login state, pages with the sidebar."""

import zlib
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import QueryParams

from metrics import analytics, auth, charts, hints, lists, logins
from metrics.db import ImportRun, Proposal, get_sessionmaker
from metrics.instance import get_instance
from metrics.planning import overview
from metrics.web import nav, security

HERE = Path(__file__).parent
TIMEZONE = ZoneInfo("Europe/Berlin")

templates = Jinja2Templates(directory=HERE / "templates")

AVATAR_TONES = 5


def avatar_tone(name: str) -> int:
    """A stable colour slot (0-4) per name, so a person keeps the same avatar colour everywhere."""
    return zlib.crc32((name or "").encode()) % AVATAR_TONES


templates.env.filters["avatar_tone"] = avatar_tone


def today() -> date:
    return datetime.now(TIMEZONE).date()


def _date_param(value: str | None, name: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise HTTPException(400, f"Ungültiges Datum für {name}: {value}") from error


def parse_filters(params: QueryParams, detailed: bool = True) -> analytics.Filters:
    """Filters from the query string; the overview (detailed=False) only takes bin and period."""
    bin_name = params.get("bin") or "month"
    preset = params.get("period") or "all"
    if bin_name not in analytics.BINS:
        raise HTTPException(400, f"Unbekanntes Zeitraster: {bin_name}")
    if preset not in analytics.PRESETS:
        raise HTTPException(400, f"Unbekannter Zeitraum: {preset}")
    return analytics.Filters(
        bin=bin_name,
        preset=preset,
        date_from=_date_param(params.get("from"), "from") if preset == "custom" else None,
        date_to=_date_param(params.get("to"), "to") if preset == "custom" else None,
        categories=params.getlist("category") if detailed else [],
        contexts=params.getlist("context") if detailed else [],
        target_groups=params.getlist("target_group") if detailed else [],
    )


_NOT_LOOKED_UP = object()


def current_login(request: Request) -> auth.Login | None:
    """Who is logged in: the server-side session, looked up once per request."""
    login = getattr(request.state, "login", _NOT_LOOKED_UP)
    if login is _NOT_LOOKED_UP:
        if auth.mode() == "off":
            login = auth.Login(auth.OFF_USER, admin=True, subject="off")
        else:
            login = logins.lookup(request.cookies.get(auth.COOKIE_NAME))
        request.state.login = login
    return login


def page_context(filters: analytics.Filters, **extra) -> dict:
    return {"filters": filters, "bins": analytics.BINS, "presets": analytics.PRESETS, **extra}


def page(request: Request, template: str, context: dict, status_code: int = 200) -> HTMLResponse:
    """A page with the sidebar: login state and the links this person may open."""
    login = current_login(request)
    counts = overview.sidebar_counts(login, today()) if login else {}
    projects: list = []
    if login:
        reference = list_state()
        counts |= reference.counts
        projects = reference.projects
    sidebar = {
        "login": login,
        "nav": nav.sidebar(login, request.url.path, counts, projects),
        "auth_off": auth.mode() == "off",
        "path": request.url.path,
        "csrf_token": security.csrf_token(request),
    }
    response = templates.TemplateResponse(request, template, {**sidebar, **context}, status_code=status_code)
    security.remember_csrf_token(request, response)
    return response


@dataclass(frozen=True)
class ListState:
    projects: list  # project pages, from the contexts (WM-101)
    counts: dict[str, int]  # proposed entries per list, open hints and proposals for "Prüfen"


def list_state() -> ListState:
    """Project pages and the counters for the sidebar; empty if the database is unreachable."""
    try:
        with get_sessionmaker()() as session:
            counts = lists.proposed_counts(session)
            found = hints.sidebar_counts(session)
            open_proposals = session.scalar(
                select(func.count()).select_from(Proposal).where(Proposal.decision.is_(None))
            )
            counts |= {
                "hints_high": found["hints_high"],
                "pruefen_open": (open_proposals or 0) + found["hints_medium"],
            }
            return ListState(lists.projects(session), counts)
    except SQLAlchemyError:
        return ListState([], {})


def public_base(request: Request) -> str:
    """The address people use to open the app (for links in calendar entries)."""
    return auth.get_settings().public_url or str(request.base_url).rstrip("/")


def _data_as_of(session) -> str | None:
    finished = session.execute(select(func.max(ImportRun.finished_at))).scalar()
    return finished.astimezone(TIMEZONE).strftime("%d.%m.%Y, %H:%M Uhr") if finished else None


def sessions_over_time(filters: analytics.Filters) -> JSONResponse:
    """Chart and key figures for the overview and the analysis page."""
    instance = get_instance()
    with get_sessionmaker()() as session:
        rows, (period_from, period_to) = analytics.count_by_date_and_category(
            session, filters, today(), instance.school_year_start_month
        )
        data_as_of = _data_as_of(session)
        totals = analytics.totals(session, filters, today(), instance.school_year_start_month)
    segments = analytics.aggregate(rows, filters.bin, instance.school_year_start_month)
    dates = [row[0] for row in rows]
    caption = analytics.period_caption(min(dates), max(dates)) if dates else ""
    return JSONResponse(
        {
            "spec": charts.sessions_over_time(segments, filters.bin, instance.theme, caption),
            "meta": {
                "count": sum(row[2] for row in rows),
                "first": min(dates).isoformat() if dates else None,
                "last": max(dates).isoformat() if dates else None,
                "period_from": period_from.isoformat() if period_from else None,
                "period_to": period_to.isoformat() if period_to else None,
                "data_as_of": data_as_of,
                "totals": totals.__dict__,
            },
        }
    )
