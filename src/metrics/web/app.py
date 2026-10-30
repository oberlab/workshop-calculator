"""The single MVP page: sessions over time, with filters."""

from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from starlette.datastructures import QueryParams

from metrics import analytics, charts
from metrics.db import STATUS_DONE, ImportRun, SessionRow, get_sessionmaker
from metrics.ingest.records import TARGET_GROUPS
from metrics.instance import Theme, get_instance

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


def parse_filters(params: QueryParams) -> analytics.Filters:
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
        categories=params.getlist("category"),
        contexts=params.getlist("context"),
        target_groups=params.getlist("target_group"),
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


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    filters = parse_filters(request.query_params)
    with get_sessionmaker()() as session:
        options = _options(session)
    return templates.TemplateResponse(
        request,
        "index.html",
        {"filters": filters, "options": options, "bins": analytics.BINS, "presets": analytics.PRESETS},
    )


@app.get("/api/sessions-over-time")
def sessions_over_time(request: Request) -> JSONResponse:
    filters = parse_filters(request.query_params)
    instance = get_instance()
    with get_sessionmaker()() as session:
        rows, (period_from, period_to) = analytics.count_by_date_and_category(
            session, filters, today(), instance.school_year_start_month
        )
        data_as_of = _data_as_of(session)
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


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}
