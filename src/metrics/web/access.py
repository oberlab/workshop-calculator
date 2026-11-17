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

from metrics.web import hosts
from metrics.web.common import current_login

PUBLIC = "public"
TEAM = "team"
ADMIN = "admin"

# Reachable without login: the front page and its data, the privacy notice and the imprint (WM-153),
# login, health, theme, icon and static files.
PUBLIC_ROUTES = {
    "GET /",
    "GET /api/sessions-over-time",
    "GET /datenschutz",
    "GET /impressum",
    "GET /login",
    "POST /login",
    "GET /login/start",
    "GET /login/callback",
    "POST /logout",
    "GET /abgemeldet",
    "GET /healthz",
    "GET /theme.css",
    "GET /theme/fonts/{name}",
    "GET /favicon.ico",
    "MOUNT /static",  # mounted apps bypass route dependencies, so only public ones may be mounted
}

# What answers on the front address when the front page has one of its own (FRONT_URL, WM-154): the
# front page and what it needs. Login and logout only work on the team address (PUBLIC_URL).
FRONT_ROUTES = {
    "GET /",
    "GET /api/sessions-over-time",
    "GET /datenschutz",
    "GET /impressum",
    "GET /healthz",
    "GET /theme.css",
    "GET /theme/fonts/{name}",
    "GET /favicon.ico",
    "MOUNT /static",
}

ADMIN_ROUTES = {
    # Who is logged in, on the Personen page (moved from the review page, WM-110).
    "POST /stammdaten/personen/abmelden",
    # Stammdaten: the admin decisions on list entries (WM-101).
    "POST /stammdaten/{kind}/{entry_id:int}/bestaetigen",
    "POST /stammdaten/{kind}/{entry_id:int}/zusammenfuehren",
    "POST /stammdaten/{kind}/{entry_id:int}/ablehnen",
    # Prüfen: the queue and accepting a warning (WM-113).
    "GET /pruefen",
    "POST /pruefen/akzeptieren",
    # Prüfen: calendar events without a Termin (WM-110).
    "POST /pruefen/kalender/pruefen",
    "POST /pruefen/kalender/ignorieren",
    "POST /pruefen/kalender/kurs",
    # Prüfen: confirming or rejecting a proposed correction of an abgeschlossen Termin (WM-132).
    "POST /pruefen/korrektur/bestaetigen",
    "POST /pruefen/korrektur/ablehnen",
    # Personen: the team overview, kept by hand (WM-105); the people picker stays Team.
    "GET /stammdaten/personen",
    "POST /stammdaten/personen",
    "POST /stammdaten/personen/{entry_id:int}",
}

TEAM_ROUTES = {
    # The team part of the privacy notice (WM-153): data about team members, contact persons and
    # participants; the short public part is `GET /datenschutz`.
    "GET /datenschutz/team",
    "GET /analyse",
    "GET /projekte/{slug}",
    "GET /api/analyse/sessions-over-time",
    # KPI tiles with the comparison to the previous period (WM-150), like the other analysis data.
    "GET /api/analyse/key-figures",
    "GET /api/analyse/schools",
    "GET /api/analyse/municipalities",
    # Herkunft map on project pages whose context asks for home municipalities (WM-134).
    "GET /api/analyse/origins",
    "GET /api/instructor-ranking",
    # Standard views (WM-151): breakdowns by context, format, school and class/age, the cumulative
    # count and the point map of places, like the other analysis data.
    "GET /api/analyse/by/{dimension}",
    "GET /api/analyse/cumulative",
    "GET /api/analyse/places",
    # Planungszahlen (WM-151): names team members (hours per person) and courses, so team only, never
    # public; read only.
    "GET /analyse/planung",
    "GET /api/analyse/planung/stunden",
    "GET /instructors/{key}/image",
    # Tabelle (WM-152): the held Termine with chosen columns and their CSV. Team members' names may
    # appear; contacts (WM-141) never do (`metrics.table`).
    "GET /tabelle",
    "GET /tabelle.csv",
    # Planning (WM-110): course list, course page, actions; what a stage allows is checked in
    # `metrics.planning.workflow`. Team members may accept warnings on the course page.
    "GET /planung",
    "GET /planung/neu",
    "POST /planung/neu",
    "GET /planung/{course_id:int}",
    "POST /planung/{course_id:int}",
    "POST /planung/{course_id:int}/aktion",
    "POST /planung/{course_id:int}/akzeptieren",
    # Corrections (WM-132): anyone in the team proposes one; admins decide in "Prüfen".
    "POST /planung/{course_id:int}/korrektur",
    "GET /planung/termin/{appointment_id:int}",
    "GET /api/planung/belegt",
    # Mein Bereich and the calendar (WM-112): read only; "mine" is the Person linked to the login.
    "GET /mein-bereich",
    "GET /kalender",
    "GET /kalender/termin/{appointment_id:int}",
    # After the course (WM-133): the phone form for participants, Herkunft and a note; any team
    # member may enter them, like on the course page. Saving goes through `metrics.planning.workflow`.
    "GET /mein-bereich/nach-dem-kurs/{appointment_id:int}",
    "POST /mein-bereich/nach-dem-kurs/{appointment_id:int}",
    # Team (WM-131): lanes, workload and "Wer ist frei?" for everyone in the team; read only.
    "GET /team",
    # Series (WM-111): add Termine, bulk actions over the Termine of a course.
    "POST /planung/{course_id:int}/termine",
    "POST /planung/{course_id:int}/alle",
    # Stammdaten: read, propose, edit (proposed entries; confirmed ones only admins, checked in
    # `metrics.lists.update`), and the picker. `{kind}` never serves `personen` here (404): the
    # Personen page has its own admin routes above, which match first.
    "GET /stammdaten/{kind}",
    "POST /stammdaten/{kind}",
    "POST /stammdaten/{kind}/{entry_id:int}",
    "GET /stammdaten/{kind}/auswahl",
    "POST /stammdaten/{kind}/auswahl",
    # Contact persons (WM-141): personal data of people outside the team, so never public. Any team
    # member adds, changes, chooses or deletes (on request) a contact; `metrics.contacts`.
    "POST /planung/{course_id:int}/kontakt",
    "POST /stammdaten/orte/{place_id:int}/kontakte",
    "POST /stammdaten/orte/{place_id:int}/kontakte/{contact_id:int}",
    "POST /stammdaten/orte/{place_id:int}/kontakte/{contact_id:int}/loeschen",
    # Formate: the equipment a format needs (WM-140); like editing the format, team members change it
    # on proposed formats, admins on confirmed ones (checked in `metrics.equipment.set_format_needs`).
    "POST /stammdaten/formate/{entry_id:int}/material",
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
    key = f"{request.method} {route.path}"
    if hosts.on_front_host(request) and key not in FRONT_ROUTES:
        raise HTTPException(404)
    required = level(key)
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
