"""The team's analysis: `/analyse` with all filters and the internal views, one page per selected
project (`/projekte/<slug>`, the analysis limited to the project's contexts) and their data endpoints.
"""

from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select

from metrics import analytics, charts
from metrics.db import STATUS_DONE, Instructor, SessionRow, get_sessionmaker
from metrics.ingest.records import TARGET_GROUPS
from metrics.instance import Project, get_instance
from metrics.ranking import instructor_ranking
from metrics.web.common import page, page_context, parse_filters, sessions_over_time, today

router = APIRouter()


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


def _analysis_page(request: Request, project: Project | None = None) -> HTMLResponse:
    filters = parse_filters(request.query_params, detailed=project is None)
    options = None
    if project is None:  # a project page has no category, target group or context filter
        with get_sessionmaker()() as session:
            options = _options(session)
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
            map=instance.map,
        ),
    )


@router.get("/analyse", response_class=HTMLResponse)
def analysis(request: Request):
    return _analysis_page(request)


@router.get("/projekte/{slug}", response_class=HTMLResponse)
def project_page(slug: str, request: Request):
    project = get_instance().project(slug)
    if project is None:
        raise HTTPException(404, "Unbekanntes Projekt")
    return _analysis_page(request, project)


@router.get("/api/analyse/sessions-over-time")
def sessions_over_time_detailed(request: Request) -> JSONResponse:
    return sessions_over_time(parse_filters(request.query_params))


def _location_rows(filters: analytics.Filters):
    instance = get_instance()
    with get_sessionmaker()() as session:
        rows = analytics.count_by_location(session, filters, today(), instance.school_year_start_month)
    return rows, instance


@router.get("/api/analyse/schools")
def sessions_by_school_type(request: Request) -> JSONResponse:
    """Sessions at schools per school type (all filters)."""
    rows, instance = _location_rows(parse_filters(request.query_params))
    counts, order = analytics.by_school_type(rows, instance)
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
        counts = analytics.count_by_instructor(session, filters, today(), instance.school_year_start_month)
        people = {
            key: (name, has_image)
            for key, name, has_image in session.execute(
                select(Instructor.key, Instructor.name, Instructor.image.is_not(None))
            )
        }
    rows = instructor_ranking(counts, instance, people)
    return JSONResponse({"rows": [row.__dict__ for row in rows]})


@router.get("/instructors/{key}/image")
def instructor_image(key: str) -> Response:
    _require_ranking()
    with get_sessionmaker()() as session:
        row = session.get(Instructor, key)
    if row is None or row.image is None:
        raise HTTPException(404)
    return Response(
        row.image,
        media_type=row.image_type or "image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )
