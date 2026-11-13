"""Helpers shared by the routers: filters from the query string, login state, pages with the sidebar."""

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.datastructures import QueryParams

from metrics import analytics, auth, charts
from metrics.db import ImportRun, get_sessionmaker
from metrics.instance import get_instance
from metrics.planning import overview

HERE = Path(__file__).parent
TIMEZONE = ZoneInfo("Europe/Berlin")

templates = Jinja2Templates(directory=HERE / "templates")


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


def current_login(request: Request) -> auth.Login | None:
    return auth.current_login(request.cookies.get(auth.COOKIE_NAME))


def page_context(filters: analytics.Filters, **extra) -> dict:
    return {"filters": filters, "bins": analytics.BINS, "presets": analytics.PRESETS, **extra}


def page(request: Request, template: str, context: dict, status_code: int = 200) -> HTMLResponse:
    """A page with the sidebar: login state and the links this person may open."""
    login = current_login(request)
    sidebar = {
        "login": login,
        "counts": overview.sidebar_counts(login, today()) if login else {},
        "auth_off": auth.mode() == "off",
        "projects": get_instance().projects if login else [],
        "path": request.url.path,
    }
    return templates.TemplateResponse(request, template, {**sidebar, **context}, status_code=status_code)


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
