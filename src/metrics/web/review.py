"""The admin review page; its planning actions live in the planning router."""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from metrics.web import planning
from metrics.web.common import page

router = APIRouter()


@router.get("/review", response_class=HTMLResponse)
def review(request: Request):
    return page(request, "review.html", {"planning": planning.review_context(request)})
