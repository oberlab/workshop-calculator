"""The public overview: chart and key figures, only time bin and period can be chosen."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from metrics.web.common import page, page_context, parse_filters, sessions_over_time

router = APIRouter()


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    filters = parse_filters(request.query_params, detailed=False)
    return page(request, "index.html", page_context(filters, api="/api/sessions-over-time"))


@router.get("/api/sessions-over-time")
def overview_data(request: Request) -> JSONResponse:
    # Overview: other filter parameters are ignored, so the open endpoint never slices the data further.
    return sessions_over_time(parse_filters(request.query_params, detailed=False))
