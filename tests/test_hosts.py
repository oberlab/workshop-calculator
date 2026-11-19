"""Two addresses (WM-154): the front page on FRONT_URL, the team part on PUBLIC_URL; and the app's own
login and "Abgemeldet" pages."""

import pytest

from metrics import auth, config, logins
from metrics.web import access, login
from metrics.web import app as web
from tests.test_web import ISSUER, OIDC, _client, _fake_provider, _noop

FRONT = "http://zahlen.example.org"
TEAM = "http://testserver"  # the test client's own address
SPLIT = {"front_url": FRONT, "public_url": TEAM}


def _front_client(monkeypatch, **overrides):
    client = _client(monkeypatch, **SPLIT, **overrides)
    client.base_url = FRONT
    client.headers["Origin"] = FRONT
    return client


def _log_in(client, monkeypatch):
    monkeypatch.setattr(login.asyncio, "sleep", lambda _: _noop())
    client.post("/login", data={"password": "richtiges-passwort"}, follow_redirects=False)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"FRONT_URL": "https://zahlen.example.org"}, "needs PUBLIC_URL"),
        (
            {"FRONT_URL": "https://zahlen.example.org", "PUBLIC_URL": "https://zahlen.example.org/team"},
            "different host names",
        ),
    ],
)
def test_front_url_needs_its_own_team_address(monkeypatch, env, message):
    monkeypatch.delenv("PUBLIC_URL", raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    config.get_settings.cache_clear()
    try:
        with pytest.raises(ValueError, match=message):
            config.get_settings()
    finally:
        config.get_settings.cache_clear()


def test_front_url_setting(monkeypatch):
    monkeypatch.setenv("FRONT_URL", "https://zahlen.example.org/")
    monkeypatch.setenv("PUBLIC_URL", "https://kurse.example.org")
    config.get_settings.cache_clear()
    try:
        assert config.get_settings().front_url == "https://zahlen.example.org"
    finally:
        config.get_settings.cache_clear()


def test_front_routes_are_public():
    assert access.FRONT_ROUTES <= access.PUBLIC_ROUTES
    assert set(access.FRONT_ROUTES) <= set(access.route_keys(web.app))


def test_front_address_serves_only_the_front_page(monkeypatch):
    client = _front_client(monkeypatch)
    for path in ("/", "/datenschutz", "/impressum", "/theme.css", "/favicon.ico"):
        assert client.get(path).status_code == 200, path
    assert client.get("/healthz").status_code != 404  # 503 here: the tests have no database
    assert client.get("/static/style.css").status_code == 200
    for path in ("/login", "/login/start", "/abgemeldet", "/mein-bereich", "/analyse", "/datenschutz/team"):
        assert client.get(path, follow_redirects=False).status_code == 404, path
    assert client.post("/login", data={"password": "richtiges-passwort"}).status_code == 404
    assert client.post("/logout").status_code == 404


def test_front_address_is_a_site_of_its_own(monkeypatch):
    """No sidebar on the front address, on every page; the team login only in the footer."""
    client = _front_client(monkeypatch)
    for path in ("/", "/datenschutz", "/impressum"):
        html = client.get(path).text
        assert 'class="shell shell-bare shell-wide"' in html and 'id="sidebar"' not in html, path
        assert '<a class="brand" href="/">Zahlen</a>' in html, path
        footer = html.split('class="page-foot"')[1]
        assert f'<a href="{TEAM}/login">Anmelden (Team)</a>' in footer, path


def test_team_address_shows_the_overview_after_login(monkeypatch):
    client = _client(monkeypatch, **SPLIT)
    response = client.get("/", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/login")
    login_page = client.get("/login").text
    assert f'href="{FRONT}/"' in login_page
    assert '<a class="brand" href="/">Kursplaner</a>' in login_page

    _log_in(client, monkeypatch)
    html = client.get("/", follow_redirects=False).text  # "Übersicht" inside the team part
    assert 'id="sidebar"' in html and 'data-api="/api/sessions-over-time"' in html
    assert 'href="/" aria-current="page"' in html.split("</nav>")[0]
    assert "Anmelden (Team)" not in html and f'href="{FRONT}/"' not in html
    assert 'href="/datenschutz/team"' in client.get("/impressum").text  # logged in on the team address


def test_one_address_without_front_url(monkeypatch):
    client = _client(monkeypatch)
    html = client.get("/", follow_redirects=False).text
    assert 'id="sidebar"' in html and "Anmelden (Team)" not in html  # as before: one site
    assert 'href="/"' in client.get("/login").text


def test_oidc_login_page_before_the_provider(monkeypatch):
    client = _client(monkeypatch, **OIDC)
    page = client.get("/login", follow_redirects=False)
    assert page.status_code == 200 and 'href="/login/start"' in page.text
    _fake_provider(monkeypatch)
    start = client.get("/login/start", follow_redirects=False)
    assert start.status_code == 303 and start.headers["location"].startswith(f"{ISSUER}/auth?")


def test_login_start_without_oidc_goes_to_the_login_page(monkeypatch):
    client = _client(monkeypatch)
    response = client.get("/login/start", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/login")


def test_logout_ends_on_the_logged_out_page(monkeypatch):
    client = _client(monkeypatch)
    _log_in(client, monkeypatch)
    token = client.cookies[auth.COOKIE_NAME]
    assert client.get("/abgemeldet", follow_redirects=False).headers["location"] == "/mein-bereich"
    response = client.post("/logout", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/abgemeldet")
    assert logins.lookup(token) is None
    client.cookies.delete(auth.COOKIE_NAME)
    html = client.get("/abgemeldet").text
    assert "Du bist abgemeldet." in html and 'href="/login"' in html
    assert "Anmeldedienst" not in html  # the note about the provider only with OpenID Connect


def test_login_and_logged_out_pages_have_no_sidebar(monkeypatch):
    client = _client(monkeypatch)
    for path in ("/login", "/abgemeldet"):
        html = client.get(path).text
        assert 'class="shell shell-bare"' in html and 'id="sidebar"' not in html, path
        assert "data-menu-toggle" not in html and 'class="account"' not in html, path
    assert 'id="sidebar"' in client.get("/").text


def test_logged_out_page_mentions_the_provider_session(monkeypatch):
    html = _client(monkeypatch, **OIDC).get("/abgemeldet").text
    assert "Beim Anmeldedienst selbst" in html
