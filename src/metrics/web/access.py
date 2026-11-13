"""The one access rule for every route (`PROPOSAL.md` section 7).

Every route is listed below as public, team or admin. The rule runs as an app-wide dependency on the
matched route, before the handler, so a handler never has to remember a check. A route missing from
the lists needs Team (fail closed); `tests/test_access.py` fails until it is listed.

Refusals keep the app's behaviour: a page (a GET route answering HTML) sends a visitor without login
to `/login` and shows a "Kein Zugriff" page with 403 to a team member on an admin page; everything else
answers 401 or 403 with a short message.
"""

from fastapi import HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.routing import APIRoute, iter_route_contexts

from metrics.web.common import current_login

PUBLIC = "public"
TEAM = "team"
ADMIN = "admin"

# Reachable without login: the front page and its data, login, health, theme and static files.
PUBLIC_ROUTES = {
    "GET /",
    "GET /api/sessions-over-time",
    "GET /login",
    "POST /login",
    "GET /login/callback",
    "POST /logout",
    "GET /healthz",
    "GET /theme.css",
    "GET /theme/fonts/{name}",
    "MOUNT /static",  # mounted apps bypass route dependencies, so only public ones may be mounted
}

ADMIN_ROUTES = {
    "GET /review",
    "POST /review/kalender/pruefen",
    "POST /review/kalender/ignorieren",
    "POST /review/kalender/kurs",
    "POST /review/demo/kalender/loeschen",
    "POST /review/demo/zuruecksetzen",
    "POST /review/personen",
    "POST /review/abmelden",
}

TEAM_ROUTES = {
    "GET /analyse",
    "GET /projekte/{slug}",
    "GET /api/analyse/sessions-over-time",
    "GET /api/analyse/schools",
    "GET /api/analyse/municipalities",
    "GET /api/instructor-ranking",
    "GET /instructors/{key}/image",
    "GET /planung",
    "GET /planung/neu",
    "POST /planung/neu",
    "GET /planung/{course_id:int}",
    "POST /planung/{course_id:int}",
    "POST /planung/{course_id:int}/aktion",
    "GET /api/planung/belegt",
    "GET /planung/orte",
    "POST /planung/orte",
}


def level(key: str) -> str:
    """The access level of a route key such as "GET /analyse"; unlisted routes need Team."""
    if key in PUBLIC_ROUTES:
        return PUBLIC
    if key in ADMIN_ROUTES:
        return ADMIN
    return TEAM


def route_keys(app) -> list[str]:
    """The keys of all routes of the app: one per method, or "MOUNT <path>" for a mounted app."""
    keys = []
    for route in iter_route_contexts(app.routes):
        if route.methods:
            keys += [f"{method} {route.path}" for method in sorted(route.methods)]
        else:
            keys.append(f"MOUNT {route.path}")
    return keys


def is_page(request: Request, route: APIRoute) -> bool:
    response_class = getattr(route.response_class, "value", route.response_class)
    return (
        request.method == "GET"
        and isinstance(response_class, type)
        and issubclass(response_class, HTMLResponse)
    )


class PageRefused(Exception):
    """A page the visitor may not open: handled in `app.py` (login redirect or "Kein Zugriff")."""

    def __init__(self, login_needed: bool):
        self.login_needed = login_needed


def check_access(request: Request) -> None:
    """App-wide dependency: refuse the request unless the person has the route's level."""
    # The matched route as declared in its router; routers are included without a prefix, so its path
    # is the full path. A prefixed router would not match its listing and fall back to Team.
    route = request.scope["route"]
    required = level(f"{request.method} {route.path}")
    if required == PUBLIC:
        return
    login = current_login(request)
    if login is None:
        if is_page(request, route):
            raise PageRefused(login_needed=True)
        raise HTTPException(401, "Anmeldung erforderlich")
    if required == ADMIN and not login.admin:
        if is_page(request, route):
            raise PageRefused(login_needed=False)
        raise HTTPException(403, "Nur für Admins")
