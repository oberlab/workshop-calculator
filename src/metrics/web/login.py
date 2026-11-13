"""Login and logout: OpenID Connect or the shared team password (see `metrics.auth`, `metrics.oidc`).

A login opens a server-side session (`metrics.logins`); the groups are read from the provider at
every login. Both login steps are rate-limited per client address.
"""

import asyncio
import logging
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy.exc import SQLAlchemyError

from metrics import auth, lists, logins, oidc
from metrics.db import get_sessionmaker
from metrics.web import security
from metrics.web.common import current_login, page

router = APIRouter()
log = logging.getLogger(__name__)

# Generous for people, slow for guessing: Keycloak has its own brute-force protection on top.
LOGIN_LIMIT = security.RateLimit(limit=10, window=300)


def _link_person(login: auth.Login) -> None:
    """Link the login to its Person by e-mail at the first login; a failure never stops a login."""
    if not login.email:
        return
    try:
        with get_sessionmaker()() as session, session.begin():
            lists.link_login(session, login.subject, login.email)
    except SQLAlchemyError:
        log.warning("Could not link the login to a person")


def _logged_in(request: Request, login: auth.Login) -> RedirectResponse:
    response = RedirectResponse("/analyse", status_code=303)
    token = logins.start(login)
    _link_person(login)
    security.set_cookie(request, response, auth.COOKIE_NAME, token, int(logins.LIFETIME.total_seconds()))
    return response


def _login_page(request: Request, status_code: int = 200, **context) -> HTMLResponse:
    defaults = {
        "mode": auth.mode(),
        "configured": False,
        "allowed": auth.password_login_allowed(),
        "failed": False,
        "error": None,
    }
    return page(request, "login.html", {**defaults, **context}, status_code=status_code)


def _callback_url(request: Request) -> str:
    # Behind a reverse proxy the app may not see the public host and scheme; PUBLIC_URL fixes that.
    public = auth.get_settings().public_url
    return f"{public}/login/callback" if public else str(request.url_for("oidc_callback"))


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if current_login(request) is not None:
        return RedirectResponse("/analyse", status_code=303)
    if auth.mode() == "password":
        return _login_page(request, configured=auth.password_configured())
    if not oidc.configured():
        return _login_page(request)
    try:
        url, flow = oidc.start(_callback_url(request))
    except OSError:
        return _login_page(request, 503, configured=True, error="Der Anmeldedienst ist nicht erreichbar.")
    response = RedirectResponse(url, status_code=303)
    # SameSite=Lax: the cookie comes back on the provider's top-level redirect to the callback.
    security.set_cookie(
        request,
        response,
        oidc.STATE_COOKIE,
        auth.sign(flow, oidc.STATE_SECONDS),
        oidc.STATE_SECONDS,
        "/login",
    )
    return response


@router.get("/login/callback", response_class=HTMLResponse, name="oidc_callback")
def login_callback(request: Request):
    if auth.mode() != "oidc" or not oidc.configured():
        raise HTTPException(404)
    LOGIN_LIMIT.check(request)
    flow = auth.unsign(request.cookies.get(oidc.STATE_COOKIE))
    try:
        user = oidc.finish(dict(request.query_params), flow, _callback_url(request))
    except oidc.OidcError as error:
        response = _login_page(request, 403, configured=True, error=str(error))
    else:
        response = _logged_in(request, user)
    response.delete_cookie(oidc.STATE_COOKIE, path="/login")
    return response


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request):
    if not auth.password_login_allowed():
        raise HTTPException(405)
    LOGIN_LIMIT.check(request)
    form = parse_qs((await request.body()).decode(errors="replace"))
    password = (form.get("password") or [""])[0]
    if auth.check_team_password(password):
        return _logged_in(request, auth.Login("Team"))
    await asyncio.sleep(1)  # slows down guessing
    return _login_page(request, 401, configured=auth.password_configured(), failed=True)


@router.post("/logout")
def logout(request: Request) -> RedirectResponse:
    logins.end(request.cookies.get(auth.COOKIE_NAME))
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(
        auth.COOKIE_NAME, httponly=True, samesite="lax", secure=security.is_secure(request)
    )
    return response
