"""The team's analysis: `/analyse` with all filters and the internal views, one page per selected
project (`/projekte/<slug>`, the analysis limited to the project's contexts) and their data endpoints.
"""

from urllib.parse import urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from sqlalchemy import select

from metrics import analytics, charts, lists
from metrics.db import Person, get_sessionmaker
from metrics.ingest.records import TARGET_GROUPS
from metrics.instance import Project, get_instance
from metrics.ranking import instructor_ranking
from metrics.web.common import page, page_context, parse_filters, sessions_over_time, today

router = APIRouter()


def _options(session) -> dict:
    options = analytics.filter_options(session)
    return {
        "categories": charts.category_order(get_instance().theme, options.categories),
        "contexts": options.contexts,
        "target_groups": [g for g in TARGET_GROUPS if g in options.target_groups],
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
    with get_sessionmaker()() as session:
        project = lists.project(session, slug)
    if project is None:
        raise HTTPException(404, "Unbekanntes Projekt")
    return _analysis_page(request, project)


@router.get("/api/analyse/sessions-over-time")
def sessions_over_time_detailed(request: Request) -> JSONResponse:
    return sessions_over_time(parse_filters(request.query_params))


def _place_rows(filters: analytics.Filters):
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
