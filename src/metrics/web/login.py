"""Login and logout: shared team password or OpenID Connect (see `metrics.auth`, `metrics.oidc`)."""

import asyncio
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from metrics import auth, oidc
from metrics.web.common import current_login, page

router = APIRouter()


def _secure(request: Request) -> bool:
    public = auth.get_settings().public_url or ""
    return request.url.scheme == "https" or public.startswith("https://")


def _logged_in(request: Request, login: auth.Login) -> RedirectResponse:
    response = RedirectResponse("/analyse", status_code=303)
    response.set_cookie(
        auth.COOKIE_NAME,
        auth.make_token(login.user, admin=login.admin),
        max_age=auth.SESSION_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_secure(request),
    )
    return response


def _login_page(request: Request, status_code: int = 200, **context) -> HTMLResponse:
    defaults = {"mode": auth.mode(), "configured": False, "failed": False, "error": None}
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
    response.set_cookie(
        oidc.STATE_COOKIE,
        auth.sign(flow, oidc.STATE_SECONDS),
        max_age=oidc.STATE_SECONDS,
        path="/login",
        httponly=True,
        samesite="lax",  # sent on the provider's top-level redirect back to us
        secure=_secure(request),
    )
    return response


@router.get("/login/callback", response_class=HTMLResponse, name="oidc_callback")
def login_callback(request: Request):
    if auth.mode() != "oidc" or not oidc.configured():
        raise HTTPException(404)
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
    if auth.mode() != "password":
        raise HTTPException(405)
    form = parse_qs((await request.body()).decode(errors="replace"))
    password = (form.get("password") or [""])[0]
    if auth.check_team_password(password):
        return _logged_in(request, auth.Login("Team"))
    await asyncio.sleep(1)  # slows down guessing
    return _login_page(request, 401, configured=auth.password_configured(), failed=True)


@router.post("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse("/", status_code=303)
    response.delete_cookie(auth.COOKIE_NAME)
    return response
