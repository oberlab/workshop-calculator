import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.datastructures import QueryParams

from metrics import auth, logins
from metrics.instance import load_theme
from metrics.web import app as web
from metrics.web import common, health, login, security, theme


def test_parse_filters_defaults():
    filters = common.parse_filters(QueryParams(""))
    assert (filters.bin, filters.preset, filters.categories) == ("month", "all", [])


def test_parse_filters_values():
    filters = common.parse_filters(
        QueryParams(
            "bin=quarter&period=custom&from=2024-01-01&to=2024-12-31&category=Code&category=Science&context=A"
        )
    )
    assert filters.bin == "quarter"
    assert (filters.date_from, filters.date_to) == (date(2024, 1, 1), date(2024, 12, 31))
    assert filters.categories == ["Code", "Science"]
    assert filters.contexts == ["A"]


def test_dates_only_count_for_custom_period():
    filters = common.parse_filters(QueryParams("period=all&from=2024-01-01"))
    assert filters.date_from is None


@pytest.mark.parametrize("query", ["bin=day", "period=forever", "period=custom&from=gestern"])
def test_parse_filters_rejects_invalid(query):
    with pytest.raises(HTTPException) as error:
        common.parse_filters(QueryParams(query))
    assert error.value.status_code == 400


def test_theme_css(instance):
    css = theme.theme_css_text(instance.theme)
    assert "--color-text: #333333;" in css
    assert "--font-body: system-ui, sans-serif;" in css
    # Optional dark inks: underscores in the key become dashes in the variable that style.css reads.
    assert "--color-accent-ink: #2c4d6e;" in css
    assert "--color-success-ink: #134b1c;" in css


def test_theme_without_inks_leaves_them_to_the_stylesheet(tmp_path):
    (tmp_path / "theme").mkdir()
    (tmp_path / "theme" / "tokens.toml").write_text('[color]\naccent = "#00a4c4"\n')
    css = theme.theme_css_text(load_theme(tmp_path))
    assert "--color-accent: #00a4c4;" in css
    assert "-ink" not in css


def test_stylesheet_reads_the_inks_with_derived_fallbacks():
    css = (Path(theme.__file__).parent / "static" / "style.css").read_text()
    for role in ("accent", "critical", "warning", "success"):
        assert f"--{role}-ink: var(--color-{role}-ink, color-mix(" in css


def test_healthz_without_database():
    # conftest points DATABASE_URL at a port nothing listens on.
    response = health.healthz()
    assert response.status_code == 503
    assert json.loads(response.body) == {"status": "error", "database": "unreachable"}


def test_healthz_reports_last_import(monkeypatch):
    class Result:
        def scalar(self):
            return datetime(2026, 11, 3, 9, 30, tzinfo=UTC)

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement):
            return Result()

    monkeypatch.setattr(health, "get_sessionmaker", lambda: Session)
    monkeypatch.setattr(health, "job_status", lambda session, now: {})
    monkeypatch.setenv("APP_VERSION", "v1.2.3")
    response = health.healthz()
    assert response.status_code == 200
    assert json.loads(response.body) == {
        "status": "ok",
        "database": "ok",
        "last_import": "2026-11-03T09:30:00+00:00",
        "version": "v1.2.3",
        "jobs": {},
    }


def test_overview_ignores_detail_filters():
    filters = common.parse_filters(
        QueryParams("bin=year&category=Code&context=A&target_group=Lehrkräfte"), detailed=False
    )
    assert filters.bin == "year"
    assert (filters.categories, filters.contexts, filters.target_groups) == ([], [], [])


def _settings(**overrides):
    from metrics import auth
    from metrics.config import Settings

    values = {
        "database_url": "postgresql+psycopg://unused",
        "instance_dir": Path("instance.example"),
        "slack_bot_token": None,
        "slack_list_id": None,
        "team_password_hash": auth.hash_password("richtiges-passwort"),
        "auth_mode": "password",
        "allow_password_login": True,
    }
    return Settings(**{**values, **overrides})


def memory_login_db(monkeypatch):
    """Login sessions in an in-memory SQLite database (the tests have no PostgreSQL)."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from metrics import logins

    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    logins.LoginSession.__table__.create(engine)
    maker = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(logins, "get_sessionmaker", lambda: maker)
    return maker


def _client(monkeypatch, csrf=True, login_db=True, **overrides):
    """A test client; with `csrf` it sends this site's Origin and a valid CSRF token like a browser.

    `login_db=False` keeps the login database of an earlier client (a second browser).
    """
    from fastapi.testclient import TestClient

    from metrics import auth, oidc

    settings = _settings(**overrides)
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(oidc, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "_secret", lambda: b"test-secret")
    if login_db:
        memory_login_db(monkeypatch)
    if not csrf:
        return TestClient(web.app)
    client = TestClient(web.app, headers={"Origin": "http://testserver"})
    client.cookies.set(security.CSRF_COOKIE, "test-token")
    client.headers[security.CSRF_HEADER] = "test-token"
    return client


@pytest.fixture
def client(monkeypatch):
    return _client(monkeypatch)


def test_overview_has_only_time_controls(client):
    html = client.get("/").text
    assert 'name="bin"' in html and 'name="period"' in html
    assert 'name="category"' not in html and 'name="context"' not in html
    assert 'data-api="/api/sessions-over-time"' in html


@pytest.mark.parametrize(
    "path",
    [
        "/api/analyse/sessions-over-time",
        "/api/analyse/schools",
        "/api/analyse/municipalities",
        "/api/instructor-ranking",
        "/instructors/U1/image",
    ],
)
def test_internal_endpoints_need_login(client, path):
    assert client.get(path).status_code == 401


def test_analysis_page_redirects_to_login(client):
    response = client.get("/analyse", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/login")


def test_password_login(client, monkeypatch):
    monkeypatch.setattr(login.asyncio, "sleep", lambda _: _noop())
    failed = client.post("/login", data={"password": "falsch"}, follow_redirects=False)
    assert failed.status_code == 401
    assert auth.COOKIE_NAME not in failed.cookies

    ok = client.post("/login", data={"password": "richtiges-passwort"}, follow_redirects=False)
    assert (ok.status_code, ok.headers["location"]) == (303, "/analyse")
    assert logins.lookup(ok.cookies[auth.COOKIE_NAME]).user == "Team"
    assert client.get("/login", follow_redirects=False).headers["location"] == "/analyse"

    client.post("/logout", follow_redirects=False)
    assert client.get("/analyse", follow_redirects=False).status_code == 303


def test_sidebar_without_login(client):
    html = client.get("/").text
    assert 'href="/login"' in html and "Anmelden" in html
    assert 'href="/analyse"' not in html and 'href="/projekte/' not in html and 'href="/review"' not in html


def test_team_sidebar_and_project_page(db, instance, client, monkeypatch):
    from metrics.ingest import store

    store.seed_reference(db, instance)  # the project pages come from the contexts (WM-101)
    db.commit()
    monkeypatch.setattr(login.asyncio, "sleep", lambda _: _noop())
    client.post("/login", data={"password": "richtiges-passwort"}, follow_redirects=False)
    html = client.get("/").text
    assert 'href="/analyse"' in html and 'href="/projekte/projekt-a"' in html
    assert 'href="/review"' not in html  # the shared password never makes anyone an admin
    assert client.get("/review").status_code == 403

    project = client.get("/projekte/projekt-a?category=Code")
    assert project.status_code == 200
    assert 'data-fixed="context=Projekt+A"' in project.text
    assert 'name="category"' not in project.text and 'name="context"' not in project.text
    assert 'aria-current="page"' in project.text
    assert 'role="tablist"' in project.text and 'id="view-schularten"' in project.text
    assert client.get("/projekte/unbekannt").status_code == 404


@pytest.mark.parametrize("path", ["/projekte/projekt-a", "/review"])
def test_team_pages_redirect_to_login(client, path):
    response = client.get(path, follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/login")


async def _noop():
    return None


def test_auth_off_opens_internal_endpoints(monkeypatch):
    client = _client(monkeypatch, auth_mode="off")
    assert client.get("/login", follow_redirects=False).headers["location"] == "/analyse"
    # Passes the login check; then 404 because the example instance has the ranking switched off.
    assert client.get("/api/instructor-ranking").status_code == 404


ISSUER = "https://login.example.org/realms/team"


def _fake_provider(monkeypatch, claims_override=None, groups=("/team",)):
    import base64
    import json

    from metrics import oidc

    monkeypatch.setattr(
        oidc,
        "discovery",
        lambda: {
            "issuer": ISSUER,
            "authorization_endpoint": f"{ISSUER}/auth",
            "token_endpoint": f"{ISSUER}/token",
        },
    )
    seen = {}

    def post_form(url, data):
        seen.update(data)
        claims = {
            "iss": ISSUER,
            "aud": "metrics",
            "exp": 4_000_000_000,
            "nonce": seen["nonce"],
            "name": "Alex K",
            "groups": list(groups),
            **(claims_override or {}),
        }
        body = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return {"id_token": f"header.{body}.signature", "access_token": "a"}

    monkeypatch.setattr(oidc, "_post_form", post_form)
    return seen


def _oidc_login(client, seen):
    from urllib.parse import parse_qs, urlsplit

    start = client.get("/login", follow_redirects=False)
    assert start.status_code == 303
    query = {k: v[0] for k, v in parse_qs(urlsplit(start.headers["location"]).query).items()}
    assert query["client_id"] == "metrics" and query["code_challenge_method"] == "S256"
    assert query["redirect_uri"] == "http://testserver/login/callback"
    seen["nonce"] = query["nonce"]
    return client.get(f"/login/callback?code=abc&state={query['state']}", follow_redirects=False)


OIDC = {"auth_mode": "oidc", "oidc_issuer": ISSUER, "oidc_client_id": "metrics", "oidc_client_secret": "s"}


def test_oidc_login(monkeypatch):
    client = _client(monkeypatch, **OIDC, oidc_allowed_group="team")
    seen = _fake_provider(monkeypatch)
    callback = _oidc_login(client, seen)
    assert (callback.status_code, callback.headers["location"]) == (303, "/analyse")
    assert logins.lookup(callback.cookies[auth.COOKIE_NAME]).user == "Alex K"
    assert seen["client_secret"] == "s" and seen["code"] == "abc" and seen["code_verifier"]
    assert client.post("/login", data={"password": "richtiges-passwort"}).status_code == 405


def test_oidc_admin_group(monkeypatch):
    client = _client(monkeypatch, **OIDC, oidc_allowed_group="team", oidc_admin_group="admins")
    # Admins may log in without being in the team group.
    seen = _fake_provider(monkeypatch, groups=("/admins",))
    login = logins.lookup(_oidc_login(client, seen).cookies[auth.COOKIE_NAME])
    assert login == auth.Login("Alex K", admin=True, subject="Alex K")
    assert client.get("/review").status_code == 200


def test_oidc_team_member_is_no_admin(monkeypatch):
    client = _client(monkeypatch, **OIDC, oidc_allowed_group="team", oidc_admin_group="admins")
    seen = _fake_provider(monkeypatch)
    assert logins.lookup(_oidc_login(client, seen).cookies[auth.COOKIE_NAME]).admin is False
    html = client.get("/").text
    assert 'href="/analyse"' in html and 'href="/review"' not in html
    assert client.get("/review").status_code == 403


def test_oidc_rejects_user_outside_group(monkeypatch):
    client = _client(monkeypatch, **OIDC, oidc_allowed_group="team")
    seen = _fake_provider(monkeypatch, groups=("/andere",))
    callback = _oidc_login(client, seen)
    assert callback.status_code == 403
    assert auth.COOKIE_NAME not in callback.cookies


def test_oidc_reads_custom_groups_claim(monkeypatch):
    client = _client(
        monkeypatch, **OIDC, oidc_allowed_group="metrics_team", oidc_groups_claim="metrics_groups"
    )
    # A shared scope fills "groups" with something else; the app's own mapper uses another claim.
    seen = _fake_provider(
        monkeypatch, claims_override={"metrics_groups": ["/metrics_team"]}, groups=("Editor",)
    )
    assert _oidc_login(client, seen).status_code == 303


def test_oidc_rejects_wrong_issuer(monkeypatch):
    client = _client(monkeypatch, **OIDC)
    seen = _fake_provider(monkeypatch, claims_override={"iss": "https://evil.example"})
    assert _oidc_login(client, seen).status_code == 403


def test_oidc_rejects_forged_state(monkeypatch):
    client = _client(monkeypatch, **OIDC)
    _fake_provider(monkeypatch)
    client.get("/login", follow_redirects=False)
    assert client.get("/login/callback?code=abc&state=geraten").status_code == 403


def test_unknown_auth_mode_fails(monkeypatch):
    from metrics import config

    monkeypatch.setenv("AUTH_MODE", "aus")
    with pytest.raises(ValueError):
        config._auth_mode()
