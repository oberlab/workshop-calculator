"""The web application: one router per area, all behind the central access rule (`access.py`).

`/` is the public overview: chart and key figures, only time bin and period can be chosen.
Behind the team login: `/mein-bereich` (where a login lands), `/analyse` (all filters and the
internal views), one page per selected project (`/projekte/<slug>`, the analysis limited to the
project's contexts), the planning (`/planung`, `/kalender`, `/team`), the Stammdaten (`/stammdaten/<liste>`)
and, for admins only, `/pruefen` and the Personen page.
A sidebar on every page holds the login and the navigation.
"""

from contextlib import asynccontextmanager

import anyio.to_thread
from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from metrics.db import WEB_THREADS
from metrics.web import (
    analyse,
    health,
    kalender,
    lists,
    login,
    mine,
    people,
    planning,
    pruefen,
    public,
    sessions,
    team,
    theme,
)
from metrics.web.access import PageRefused, check_access
from metrics.web.common import HERE, page
from metrics.web.security import SecurityHeaders, check_csrf


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Routes run in this many threads at most; the database pool is sized to match (`metrics.db`).
    anyio.to_thread.current_default_thread_limiter().total_tokens = WEB_THREADS
    yield


app = FastAPI(
    title="Einsätze",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    # Every route: first the access rule, then the CSRF and Origin check for POSTs.
    dependencies=[Depends(check_access), Depends(check_csrf)],
)
app.add_middleware(SecurityHeaders)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

# `people` before `lists`: its fixed /stammdaten/personen routes must match before /stammdaten/{kind}.
AREAS = (
    public,
    mine,
    analyse,
    planning,
    kalender,
    team,
    people,
    lists,
    pruefen,
    sessions,
    login,
    theme,
    health,
)
for area in AREAS:
    app.include_router(area.router)


@app.exception_handler(PageRefused)
def page_refused(request: Request, refused: PageRefused):
    if refused.login_needed:
        return RedirectResponse("/login", status_code=303)
    return page(request, "forbidden.html", {}, status_code=403)
