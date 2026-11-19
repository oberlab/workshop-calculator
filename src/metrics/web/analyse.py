"""The team's analysis: `/analyse` with all filters and the internal views, one page per selected
project (`/projekte/<slug>`, the analysis limited to the project's contexts) and their data endpoints.
"""

from decimal import Decimal
from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select

from metrics import analytics, charts, lists, metrics
from metrics.db import Person, get_sessionmaker
from metrics.ingest.records import TARGET_GROUPS
from metrics.instance import Project, get_instance
from metrics.ranking import instructor_ranking
from metrics.web.common import page, page_context, parse_filters, sessions_over_time, today

router = APIRouter()


def filter_choices(session) -> dict:
    options = analytics.filter_options(session)
    instance = get_instance()
    return {
        "categories": charts.category_order(instance.theme, options.categories),
        "contexts": options.contexts,
        "target_groups": [g for g in TARGET_GROUPS if g in options.target_groups],
        # The municipalities on the map: a click on one filters by it (WM-151).
        "municipalities": [m.name for m in instance.map.municipalities] if instance.map else [],
    }


def _has_school_termine(session, project: Project | None) -> bool:
    """Whether the Schularten and Schulen views have anything to show: a held Termin at a school, over
    the whole time span (so the tabs do not come and go with the period), in the project's contexts."""
    instance = get_instance()
    every = metrics.Filters(contexts=list(project.contexts) if project else [])
    rows = analytics.count_by_place(session, every, today(), instance.school_year_start_month)
    return any(row.is_school for row in rows)


def _analysis_page(request: Request, project: Project | None = None) -> HTMLResponse:
    filters = parse_filters(request.query_params, detailed=project is None)
    options = None
    with get_sessionmaker()() as session:
        if project is None:  # a project page has no category, target group or context filter
            options = filter_choices(session)
        school_views = _has_school_termine(session, project)
    instance = get_instance()
    return page(
        request,
        "analyse.html",
        page_context(
            filters,
            api="/api/analyse/sessions-over-time",
            options=options,
            project=project,
            # Sent with every data request but kept out of the address bar.
            fixed=urlencode([("context", c) for c in project.contexts]) if project else "",
            instructor_ranking=instance.instructor_ranking,
            # Schularten and Schulen only where there is a Termin at a school (e.g. not for JugendLab).
            school_views=school_views,
            map=instance.map,
            # Herkunft map (WM-134): only where the project's context asks for home municipalities.
            origins_map=bool(instance.map and project and project.participant_municipalities),
        ),
    )


@router.get("/analyse", response_class=HTMLResponse)
def analysis(request: Request):
    return _analysis_page(request)


@router.get("/projekte/{slug}", response_class=HTMLResponse)
def project_page(slug: str, request: Request):
    with get_sessionmaker()() as session:
        project = lists.project(session, slug)
    if project is None:
        raise HTTPException(404, "Unbekanntes Projekt")
    return _analysis_page(request, project)


@router.get("/api/analyse/sessions-over-time")
def sessions_over_time_detailed(request: Request) -> JSONResponse:
    return sessions_over_time(parse_filters(request.query_params))


def period_label(preset: str, start, end) -> str:
    """The comparison period in words: "Schuljahr 2024/25", "2025" or "01.01.2025–31.12.2025"."""
    if preset in ("this_school_year", "last_school_year"):
        return f"Schuljahr {analytics.bin_label(start, 'school_year')}"
    if preset in ("this_year", "last_year"):
        return str(start.year)
    return f"{start:%d.%m.%Y}–{end:%d.%m.%Y}"


@router.get("/api/analyse/key-figures")
def key_figures(request: Request) -> JSONResponse:
    """KPI tiles (WM-150): the key figures of the selection and of the period before (all filters)."""
    filters = parse_filters(request.query_params)
    now = today()
    with get_sessionmaker()() as session:
        result = metrics.compare(session, filters, now, get_instance().school_year_start_month)
    before = result.previous_period
    end = result.period[1]

    def number(value):
        return float(value) if isinstance(value, Decimal) else value

    figures = {}
    for key, value in result.figures.__dict__.items():
        previous = getattr(result.previous, key) if result.previous else None
        figures[key] = {
            "value": number(value),
            "previous": number(previous),
            "change": number(value - previous) if previous is not None else None,
        }
    return JSONResponse(
        {
            "figures": figures,
            # A period that is not over yet (e.g. this school year) is compared with a whole one.
            "running_until": end.isoformat() if end and end >= now else None,
            "previous": {
                "from": before[0].isoformat(),
                "to": before[1].isoformat(),
                "label": period_label(filters.preset, *before),
            }
            if before
            else None,
        }
    )


def _place_rows(filters: metrics.Filters):
    instance = get_instance()
    with get_sessionmaker()() as session:
        rows = analytics.count_by_place(session, filters, today(), instance.school_year_start_month)
        labels = analytics.school_type_labels(session)
    return rows, labels, instance


@router.get("/api/analyse/schools")
def sessions_by_school_type(request: Request) -> JSONResponse:
    """Sessions at schools per school type (all filters)."""
    rows, labels, instance = _place_rows(parse_filters(request.query_params))
    counts, order = analytics.by_school_type(rows, labels)
    return JSONResponse(
        {
            "spec": charts.sessions_by_school_type(counts, order, instance.theme),
            "meta": {"count": sum(c for _, _, c in counts)},
        }
    )


@router.get("/api/analyse/municipalities")
def sessions_by_municipality(request: Request) -> JSONResponse:
    """Map of sessions per municipality plus the sessions outside the map (all filters)."""
    instance = get_instance()
    if instance.map is None:
        raise HTTPException(404, "Keine Karte eingerichtet")
    rows, _, instance = _place_rows(parse_filters(request.query_params))
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


@router.get("/api/analyse/origins")
def participants_by_origin(request: Request) -> JSONResponse:
    """Map of participants per home municipality (Herkunft), plus those outside the map and those
    without a Herkunft (all filters; shown on project pages whose context asks for it)."""
    instance = get_instance()
    if instance.map is None:
        raise HTTPException(404, "Keine Karte eingerichtet")
    filters = parse_filters(request.query_params)
    with get_sessionmaker()() as session:
        rows = analytics.count_participants_by_origin(
            session, filters, today(), instance.school_year_start_month
        )
    counts = analytics.by_municipality(rows, instance)
    return JSONResponse(
        {
            "spec": charts.municipality_map(instance.map, counts, instance.theme, measure="Teilnehmende"),
            "meta": {
                "title": instance.map.title,
                "inside": sum(counts.inside.values()),
                "outside": counts.outside,
                "unassigned": counts.unassigned,
            },
        }
    )


# Standard views (WM-151): what each breakdown groups by and how its axis is named.
BREAKDOWNS = {
    "kontext": (analytics.by_context, "Kontext"),
    "format": (analytics.by_format, "Format"),
    "schule": (analytics.by_school, "Schule"),
    "alter": (analytics.by_age, "Klasse / Alter"),
}


@router.get("/api/analyse/by/{dimension}")
def sessions_by(dimension: str, request: Request) -> JSONResponse:
    """Sessions per context, format, school or class/age, stacked by category (all filters)."""
    if dimension not in BREAKDOWNS:
        raise HTTPException(404)
    count, title = BREAKDOWNS[dimension]
    filters = parse_filters(request.query_params)
    instance = get_instance()
    with get_sessionmaker()() as session:
        rows, order = count(session, filters, today(), instance.school_year_start_month)
    return JSONResponse(
        {
            "spec": charts.sessions_by_group(rows, order, instance.theme, title),
            "meta": {"count": sum(n for _, _, n in rows), "groups": len(order)},
        }
    )


@router.get("/api/analyse/cumulative")
def sessions_cumulative(request: Request) -> JSONResponse:
    """Held Termine added up over time (all filters); the last point is the key figure Einsätze."""
    filters = parse_filters(request.query_params)
    instance = get_instance()
    with get_sessionmaker()() as session:
        rows, _ = analytics.count_by_date_and_category(
            session, filters, today(), instance.school_year_start_month
        )
    points = analytics.cumulative(rows)
    caption = analytics.period_caption(points[0][0], points[-1][0]) if points else ""
    return JSONResponse(
        {
            "spec": charts.cumulative_sessions(points, instance.theme, caption),
            "meta": {"count": points[-1][1] if points else 0},
        }
    )


@router.get("/api/analyse/places")
def sessions_by_place(request: Request) -> JSONResponse:
    """Point map: one circle per place with coordinates; the places without them listed (all filters)."""
    filters = parse_filters(request.query_params)
    instance = get_instance()
    with get_sessionmaker()() as session:
        points = analytics.count_by_place_point(session, filters, today(), instance.school_year_start_month)
    unlocated = [p for p in points if p.lat is None or p.lon is None]
    return JSONResponse(
        {
            "spec": charts.place_points(instance.map, points, instance.theme),
            "meta": {
                "count": sum(p.count for p in points),
                "located": sum(p.count for p in points if p not in unlocated),
                "unlocated": sum(p.count for p in unlocated),
                "unlocated_places": [{"name": p.place or "Ohne Ort", "count": p.count} for p in unlocated],
            },
        }
    )


def _require_ranking() -> None:
    # Internal view: unreachable unless enabled in the instance config.
    if not get_instance().instructor_ranking:
        raise HTTPException(404)


@router.get("/api/instructor-ranking")
def instructor_ranking_api(request: Request) -> JSONResponse:
    _require_ranking()
    filters = parse_filters(request.query_params)
    instance = get_instance()
    with get_sessionmaker()() as session:
        counts = analytics.count_by_lead(session, filters, today(), instance.school_year_start_month)
    rows = instructor_ranking(counts)
    return JSONResponse({"rows": [row.__dict__ for row in rows]})


@router.get("/instructors/{key}/image")
def instructor_image(key: str) -> Response:
    """Picture of a person, by the ranking key (Slack user ID, else the recorded name)."""
    _require_ranking()
    with get_sessionmaker()() as session:
        by_key = (Person.slack_id == key) | (Person.slack_id.is_(None) & (Person.name == key))
        row = session.scalars(select(Person).where(by_key).limit(1)).first()
    if row is None or row.image is None:
        raise HTTPException(404)
    return Response(
        row.image,
        media_type=row.image_type or "image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )
