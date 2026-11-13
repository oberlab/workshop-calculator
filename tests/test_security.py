"""CSRF and Origin checks, security headers, rate limits, password switch and ended sessions."""

import json

import pytest

from metrics import auth
from metrics.web import login, public, security
from tests.test_web import OIDC, _client, _fake_provider, _noop, _oidc_login


def _form_post(client, origin="http://testserver", token="echt", cookie="echt", **data):
    """A browser-like form POST to the login: Origin header, CSRF cookie and form field."""
    client.cookies.set(security.CSRF_COOKIE, cookie)
    headers = {"Origin": origin} if origin else {}
    form = {"password": "richtiges-passwort", **({"csrf_token": token} if token else {}), **data}
    return client.post("/login", data=form, headers=headers, follow_redirects=False)


def test_form_post_from_this_site_passes(monkeypatch):
    assert _form_post(_client(monkeypatch, csrf=False)).status_code == 303


@pytest.mark.parametrize(
    "kwargs",
    [
        {"origin": "https://evil.example"},  # cross-site POST
        {"origin": None},  # no Origin at all
        {"origin": "null"},
        {"token": None},  # no CSRF token
        {"token": "falsch"},  # token does not match the cookie
        {"cookie": ""},  # no CSRF cookie
    ],
)
def test_forged_posts_are_refused(monkeypatch, kwargs):
    response = _form_post(_client(monkeypatch, csrf=False), **kwargs)
    assert response.status_code == 403
    assert auth.COOKIE_NAME not in response.cookies


def test_header_token_for_htmx(monkeypatch):
    client = _client(monkeypatch, csrf=False)
    client.cookies.set(security.CSRF_COOKIE, "echt")
    headers = {"Origin": "http://testserver", security.CSRF_HEADER: "echt"}
    response = client.post(
        "/login", data={"password": "richtiges-passwort"}, headers=headers, follow_redirects=False
    )
    assert response.status_code == 303


def test_pages_set_the_token_and_every_form_carries_it(db, monkeypatch):
    client = _client(monkeypatch, auth_mode="off", csrf=False)
    response = client.get("/planung/neu")
    token = response.cookies[security.CSRF_COOKIE]
    assert response.text.count("<form") == response.text.count(f'name="csrf_token" value="{token}"')
    assert f'"X-CSRF-Token": "{token}"' in response.text  # htmx sends it as a header
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie


def test_security_headers_everywhere(monkeypatch):
    client = _client(monkeypatch)
    for path in ("/", "/login", "/static/style.css", "/analyse", "/gibt-es-nicht"):
        headers = client.get(path, follow_redirects=False).headers
        csp = headers["content-security-policy"]
        assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
        assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "same-origin"
        assert "camera=()" in headers["permissions-policy"]


def test_no_inline_scripts_or_handlers():
    import re
    from pathlib import Path

    for template in (Path(security.__file__).parent / "templates").glob("*.html"):
        html = template.read_text()
        assert not re.search(r"<script(?![^>]*\bsrc=)", html), template.name
        assert not re.search(r"\son[a-z]+\s*=", html), template.name
        assert "javascript:" not in html, template.name


def test_session_cookie_is_secure_over_https(monkeypatch):
    client = _client(monkeypatch, public_url="https://zahlen.example.org", csrf=False)
    client.cookies.set(security.CSRF_COOKIE, "echt")
    response = client.post(
        "/login",
        data={"password": "richtiges-passwort", "csrf_token": "echt"},
        headers={"Origin": "https://zahlen.example.org"},
        follow_redirects=False,
    )
    cookie = response.headers["set-cookie"].lower()
    assert "secure" in cookie and "httponly" in cookie and "samesite=lax" in cookie


def test_login_rate_limit(monkeypatch):
    monkeypatch.setattr(login.asyncio, "sleep", lambda _: _noop())
    client = _client(monkeypatch)
    codes = [client.post("/login", data={"password": "falsch"}).status_code for _ in range(12)]
    assert codes[:10] == [401] * 10 and codes[10:] == [429, 429]


def test_public_data_is_cached_and_rate_limited(monkeypatch):
    from fastapi.responses import JSONResponse

    calls = []

    def fake(filters):
        calls.append(filters.bin)
        return JSONResponse({"bin": filters.bin})

    monkeypatch.setattr(public, "sessions_over_time", fake)
    client = _client(monkeypatch)
    first = client.get("/api/sessions-over-time?bin=year")
    assert json.loads(first.text) == {"bin": "year"} and "max-age=300" in first.headers["cache-control"]
    client.get("/api/sessions-over-time?bin=year&category=Code")  # ignored filter, same answer
    client.get("/api/sessions-over-time?bin=quarter")
    assert calls == ["year", "quarter"]
    codes = [client.get("/api/sessions-over-time").status_code for _ in range(60)]
    assert codes.count(429) > 0


def test_password_login_is_off_unless_allowed(monkeypatch):
    client = _client(monkeypatch, allow_password_login=False)
    assert "ALLOW_PASSWORD_LOGIN=true" in client.get("/login").text
    assert client.post("/login", data={"password": "richtiges-passwort"}).status_code == 405


def test_ended_admin_session_is_refused_at_the_next_request(db, monkeypatch):
    client = _client(monkeypatch, **OIDC, oidc_admin_group="admins")
    seen = _fake_provider(monkeypatch, claims_override={"sub": "sub-alex"}, groups=("/admins",))
    assert _oidc_login(client, seen).status_code == 303
    assert client.get("/pruefen").status_code == 200

    # A second admin ends all of Alex's sessions on the Personen page.
    other = _client(monkeypatch, login_db=False, **OIDC, oidc_admin_group="admins")
    seen = _fake_provider(monkeypatch, claims_override={"sub": "sub-bea", "name": "Bea"}, groups=("/admins",))
    _oidc_login(other, seen)
    people = other.get("/stammdaten/personen").text
    assert 'value="sub-alex"' in people and "Überall abmelden" in people
    ended = other.post("/stammdaten/personen/abmelden", data={"subject": "sub-alex"}, follow_redirects=False)
    assert ended.status_code == 303

    response = client.get("/pruefen", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/login")
    assert other.get("/pruefen").status_code == 200


def test_access_log_without_address_or_query():
    import logging

    from metrics.web.logs import PrivateAccessFormatter

    formatter = PrivateAccessFormatter('"%(request_line)s" %(status_code)s', use_colors=False)
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        "",
        0,
        '%s - "%s %s HTTP/%s" %d',
        ("203.0.113.7:5555", "GET", "/login/callback?code=geheim&state=x", "1.1", 303),
        None,
    )
    line = formatter.format(record)
    assert line == '"GET /login/callback HTTP/1.1" 303 See Other'
    assert "203.0.113.7" not in line and "geheim" not in line


def test_every_post_form_in_the_templates_has_the_token():
    import re
    from pathlib import Path

    for template in (Path(security.__file__).parent / "templates").glob("*.html"):
        html = template.read_text()
        for form in re.finditer(r"<form\b[^>]*>(.*?)</form>", html, re.S | re.I):
            if re.search(r'method="post"', form.group(0)[:300], re.I):
                assert 'name="csrf_token" value="{{ csrf_token }}"' in form.group(1), template.name
