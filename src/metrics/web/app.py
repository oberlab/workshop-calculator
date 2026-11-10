"""Two pages on the sessions-over-time chart.

`/` is the overview: chart and key figures, only time bin and period can be chosen.
`/analyse` is for the team (shared password): all filters and the internal views.
"""

import asyncio
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from starlette.datastructures import QueryParams

from metrics import analytics, auth, charts, oidc
from metrics.db import STATUS_DONE, ImportRun, Instructor, SessionRow, get_sessionmaker
from metrics.ingest.records import TARGET_GROUPS
from metrics.instance import Theme, get_instance
from metrics.ranking import instructor_ranking

HERE = Path(__file__).parent
TIMEZONE = ZoneInfo("Europe/Berlin")

app = FastAPI(title="Einsätze", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
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


def _options(session) -> dict:
    theme = get_instance().theme
    done = SessionRow.status == STATUS_DONE
    categories = {c for (c,) in session.execute(select(SessionRow.category).where(done).distinct()) if c}
    contexts = sorted(
        (c for (c,) in session.execute(select(SessionRow.context).where(done).distinct()) if c),
        key=str.casefold,
    )
    groups = {g for (g,) in session.execute(select(SessionRow.target_group).where(done).distinct())}
    return {
        "categories": charts.category_order(theme, categories),
        "contexts": contexts,
        "target_groups": [g for g in TARGET_GROUPS if g in groups],
    }


def _data_as_of(session) -> str | None:
    finished = session.execute(select(func.max(ImportRun.finished_at))).scalar()
    return finished.astimezone(TIMEZONE).strftime("%d.%m.%Y, %H:%M Uhr") if finished else None


def current_user(request: Request) -> str | None:
    return auth.current_user(request.cookies.get(auth.COOKIE_NAME))


def is_team(request: Request) -> bool:
    return current_user(request) is not None


def _require_team(request: Request) -> None:
    if not is_team(request):
        raise HTTPException(401, "Anmeldung erforderlich")


def _page_context(filters: analytics.Filters, **extra) -> dict:
    return {"filters": filters, "bins": analytics.BINS, "presets": analytics.PRESETS, **extra}


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    filters = parse_filters(request.query_params, detailed=False)
    return templates.TemplateResponse(
        request, "index.html", _page_context(filters, api="/api/sessions-over-time", team=is_team(request))
    )


@app.get("/analyse", response_class=HTMLResponse)
def analysis(request: Request):
    if not is_team(request):
        return RedirectResponse("/login", status_code=303)
    filters = parse_filters(request.query_params)
    with get_sessionmaker()() as session:
        options = _options(session)
    return templates.TemplateResponse(
        request,
        "analyse.html",
        _page_context(
            filters,
            api="/api/analyse/sessions-over-time",
            options=options,
            instructor_ranking=get_instance().instructor_ranking,
            map=get_instance().map,
            team=True,
            user=current_user(request),
            auth_off=auth.mode() == "off",
        ),
    )


def _secure(request: Request) -> bool:
    public = auth.get_settings().public_url or ""
    return request.url.scheme == "https" or public.startswith("https://")


def _logged_in(request: Request, user: str) -> RedirectResponse:
    response = RedirectResponse("/analyse", status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.make_token(user),
        max_age=auth.SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_secure(request),
    )
    return response


def _login_page(request: Request, status_code: int = 200, **context) -> HTMLResponse:
    defaults = {"mode": auth.mode(), "configured": False, "failed": False, "error": None}
    return templates.TemplateResponse(request, "login.html", {**defaults, **context}, status_code=status_code)


def _callback_url(request: Request) -> str:
    # Behind a reverse proxy the app may not see the public host and scheme; PUBLIC_URL fixes that.
    public = auth.get_settings().public_url
    return f"{public}/login/callback" if public else str(request.url_for("oidc_callback"))


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if is_team(request):
        return RedirectResponse("/analyse", status_code=303)
    if auth.mode() == "password":
        return _login_page(request, configured=auth.password_configured())
    if not oidc.configured():
        return _login_page(request)
    try:
        url, flow = oidc.start(_callback_url(request))
    except OSError:
        return _login_page(request, 503, configured=True, error="Der Anmeldedienst ist nicht erreichbar.")
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(
        oidc.STATE_COOKIE,
        auth.sign(flow, oidc.STATE_SECONDS),
        max_age=oidc.STATE_SECONDS,
        path="/login",
        httponly=True,
        samesite="lax",  # sent on the provider's top-level redirect back to us
        secure=_secure(request),
    )
    return response


@app.get("/login/callback", response_class=HTMLResponse, name="oidc_callback")
def login_callback(request: Request):
    if auth.mode() != "oidc" or not oidc.configured():
        raise HTTPException(404)
    flow = auth.unsign(request.cookies.get(oidc.STATE_COOKIE))
    try:
        user = oidc.finish(dict(request.query_params), flow, _callback_url(request))
    except oidc.OidcError as error:
        response = _login_page(request, 403, configured=True, error=str(error))
    else:
        response = _logged_in(request, user)
    response.delete_cookie(oidc.STATE_COOKIE, path="/login")
    return response


@app.post("/login", response_class=HTMLResponse)
async def login(request: Request):
    if auth.mode() != "password":
        raise HTTPException(405)
    form = parse_qs((await request.body()).decode(errors="replace"))
    password = (form.get("password") or [""])[0]
    if auth.check_team_password(password):
        return _logged_in(request, "Team")
    await asyncio.sleep(1)  # slows down guessing
    return _login_page(request, 401, configured=auth.password_configured(), failed=True)


@app.post("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME)
    return response


def _sessions_over_time(filters: analytics.Filters) -> JSONResponse:
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


@app.get("/api/sessions-over-time")
def sessions_over_time(request: Request) -> JSONResponse:
    # Overview: other filter parameters are ignored, so the open endpoint never slices the data further.
    return _sessions_over_time(parse_filters(request.query_params, detailed=False))


@app.get("/api/analyse/sessions-over-time")
def sessions_over_time_detailed(request: Request) -> JSONResponse:
    _require_team(request)
    return _sessions_over_time(parse_filters(request.query_params))


def _location_rows(filters: analytics.Filters):
    instance = get_instance()
    with get_sessionmaker()() as session:
        rows = analytics.count_by_location(session, filters, today(), instance.school_year_start_month)
    return rows, instance


@app.get("/api/analyse/schools")
def sessions_by_school_type(request: Request) -> JSONResponse:
    """Sessions at schools per school type (team only, all filters)."""
    _require_team(request)
    rows, instance = _location_rows(parse_filters(request.query_params))
    counts, order = analytics.by_school_type(rows, instance)
    return JSONResponse(
        {
            "spec": charts.sessions_by_school_type(counts, order, instance.theme),
            "meta": {"count": sum(c for _, _, c in counts)},
        }
    )


@app.get("/api/analyse/municipalities")
def sessions_by_municipality(request: Request) -> JSONResponse:
    """Map of sessions per municipality plus the sessions outside the map (team only, all filters)."""
    _require_team(request)
    instance = get_instance()
    if instance.map is None:
        raise HTTPException(404, "Keine Karte eingerichtet")
    rows, instance = _location_rows(parse_filters(request.query_params))
    counts = analytics.by_municipality(rows, instance)
    return JSONResponse(
        {
            "spec": charts.municipality_map(instance.map, counts, instance.theme),
            "meta": {
                "title": instance.map.title,
                "inside": sum(counts.inside.values()),
                "outside": counts.outside,
                "unassigned": counts.unassigned,
            },
        }
    )


def _theme_css(theme: Theme) -> str:
    faces = [
        f"@font-face {{ font-family: '{f['family']}'; src: url('/theme/fonts/{Path(f['file']).name}'); "
        f"font-weight: {f.get('weight', '100 900')}; font-style: normal; font-display: swap; }}"
        for f in theme.font_files
    ]
    lines = [f"  --color-{name}: {value};" for name, value in theme.colors.items()]
    lines += [f"  --font-heading: {theme.heading_font};", f"  --font-body: {theme.body_font};"]
    return "\n".join([*faces, ":root {", *lines, "}", ""])


@app.get("/theme.css")
def theme_css() -> Response:
    return Response(_theme_css(get_instance().theme), media_type="text/css")


@app.get("/theme/fonts/{name}")
def theme_font(name: str) -> FileResponse:
    theme = get_instance().theme
    files = {Path(f["file"]).name: theme.base_dir / f["file"] for f in theme.font_files}
    if name not in files or not files[name].is_file():
        raise HTTPException(404)
    return FileResponse(files[name])


def _require_ranking(request: Request) -> None:
    # Internal view: team login, and unreachable unless enabled in the instance config.
    _require_team(request)
    if not get_instance().instructor_ranking:
        raise HTTPException(404)


@app.get("/api/instructor-ranking")
def instructor_ranking_api(request: Request) -> JSONResponse:
    _require_ranking(request)
    filters = parse_filters(request.query_params)
    instance = get_instance()
    with get_sessionmaker()() as session:
        counts = analytics.count_by_instructor(session, filters, today(), instance.school_year_start_month)
        people = {
            key: (name, has_image)
            for key, name, has_image in session.execute(
                select(Instructor.key, Instructor.name, Instructor.image.is_not(None))
            )
        }
    rows = instructor_ranking(counts, instance, people)
    return JSONResponse({"rows": [row.__dict__ for row in rows]})


@app.get("/instructors/{key}/image")
def instructor_image(key: str, request: Request) -> Response:
    _require_ranking(request)
    with get_sessionmaker()() as session:
        row = session.get(Instructor, key)
    if row is None or row.image is None:
        raise HTTPException(404)
    return Response(
        row.image,
        media_type=row.image_type or "image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.get("/healthz")
def healthz() -> JSONResponse:
    """For monitoring: 200 with the time of the last finished import, 503 without a database."""
    try:
        with get_sessionmaker()() as session:
            last_import = session.execute(select(func.max(ImportRun.finished_at))).scalar()
    except SQLAlchemyError:
        # No error details: the endpoint is reachable without a login.
        return JSONResponse({"status": "error", "database": "unreachable"}, status_code=503)
    return JSONResponse(
        {
            "status": "ok",
            "database": "ok",
            "last_import": last_import.isoformat() if last_import else None,
        }
    )
