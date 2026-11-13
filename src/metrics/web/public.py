"""The public overview: chart and key figures, only time bin and period can be chosen.

The data endpoint is reachable without login, so its answers are cached for a few minutes and each
client address may ask only so often: the front page cannot be used to load the database.
"""

import threading
import time

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from metrics.web import security
from metrics.web.common import page, page_context, parse_filters, sessions_over_time

router = APIRouter()

CACHE_SECONDS = 300
CACHE_ENTRIES = 256  # custom periods make many keys possible; the oldest are dropped
DATA_LIMIT = security.RateLimit(limit=60, window=60)

_cache: dict[tuple, tuple[float, bytes]] = {}
_lock = threading.Lock()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    filters = parse_filters(request.query_params, detailed=False)
    return page(request, "index.html", page_context(filters, api="/api/sessions-over-time"))


@router.get("/api/sessions-over-time")
def overview_data(request: Request) -> Response:
    DATA_LIMIT.check(request)
    # Overview: other filter parameters are ignored, so the open endpoint never slices the data further.
    filters = parse_filters(request.query_params, detailed=False)
    key = (filters.bin, filters.preset, filters.date_from, filters.date_to)
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
    if cached and cached[0] > now:
        body = cached[1]
    else:
        body = sessions_over_time(filters).body
        with _lock:
            _cache[key] = (now + CACHE_SECONDS, body)
            while len(_cache) > CACHE_ENTRIES:
                _cache.pop(next(iter(_cache)))
    return Response(
        body, media_type="application/json", headers={"Cache-Control": f"public, max-age={CACHE_SECONDS}"}
    )
