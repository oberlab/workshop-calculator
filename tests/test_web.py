from datetime import date
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.datastructures import QueryParams

from metrics.web import app as web


def test_parse_filters_defaults():
    filters = web.parse_filters(QueryParams(""))
    assert (filters.bin, filters.preset, filters.categories) == ("month", "all", [])


def test_parse_filters_values():
    filters = web.parse_filters(
        QueryParams(
            "bin=quarter&period=custom&from=2024-01-01&to=2024-12-31&category=Code&category=Science&context=A"
        )
    )
    assert filters.bin == "quarter"
    assert (filters.date_from, filters.date_to) == (date(2024, 1, 1), date(2024, 12, 31))
    assert filters.categories == ["Code", "Science"]
    assert filters.contexts == ["A"]


def test_dates_only_count_for_custom_period():
    filters = web.parse_filters(QueryParams("period=all&from=2024-01-01"))
    assert filters.date_from is None


@pytest.mark.parametrize("query", ["bin=day", "period=forever", "period=custom&from=gestern"])
def test_parse_filters_rejects_invalid(query):
    with pytest.raises(HTTPException) as error:
        web.parse_filters(QueryParams(query))
    assert error.value.status_code == 400


def test_theme_css(instance):
    css = web._theme_css(instance.theme)
    assert "--color-text: #333333;" in css
    assert "--font-body: system-ui, sans-serif;" in css


def test_healthz():
    assert web.healthz() == {"status": "ok"}


def test_overview_ignores_detail_filters():
    filters = web.parse_filters(
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
    }
    return Settings(**{**values, **overrides})


def _client(monkeypatch, **overrides):
    from fastapi.testclient import TestClient

    from metrics import auth

    settings = _settings(**overrides)
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(auth, "_secret", lambda: b"test-secret")
    return TestClient(web.app)


@pytest.fixture
def client(monkeypatch):
    return _client(monkeypatch)


def test_overview_has_only_time_controls(client):
    html = client.get("/").text
    assert 'name="bin"' in html and 'name="period"' in html
    assert 'name="category"' not in html and 'name="context"' not in html
    assert 'data-api="/api/sessions-over-time"' in html


@pytest.mark.parametrize(
    "path", ["/api/analyse/sessions-over-time", "/api/instructor-ranking", "/instructors/U1/image"]
)
def test_internal_endpoints_need_login(client, path):
    assert client.get(path).status_code == 401


def test_analysis_page_redirects_to_login(client):
    response = client.get("/analyse", follow_redirects=False)
    assert (response.status_code, response.headers["location"]) == (303, "/login")


def test_password_login(client, monkeypatch):
    monkeypatch.setattr(web.asyncio, "sleep", lambda _: _noop())
    failed = client.post("/login", data={"password": "falsch"}, follow_redirects=False)
    assert failed.status_code == 401
    assert "metrics_team" not in failed.cookies

    ok = client.post("/login", data={"password": "richtiges-passwort"}, follow_redirects=False)
    assert (ok.status_code, ok.headers["location"]) == (303, "/analyse")
    assert web.auth.token_user(ok.cookies["metrics_team"]) == "Team"
    assert client.get("/login", follow_redirects=False).headers["location"] == "/analyse"

    client.post("/logout", follow_redirects=False)
    assert client.get("/analyse", follow_redirects=False).status_code == 303


async def _noop():
    return None


def test_auth_off_opens_internal_endpoints(monkeypatch, instance):
    monkeypatch.setattr(web, "get_instance", lambda: instance)
    client = _client(monkeypatch, auth_mode="off")
    assert client.get("/login", follow_redirects=False).headers["location"] == "/analyse"
    # Passes the login check; then 404 because the example instance has the ranking switched off.
    assert client.get("/api/instructor-ranking").status_code == 404


def test_unknown_auth_mode_fails(monkeypatch):
    from metrics import config

    monkeypatch.setenv("AUTH_MODE", "aus")
    with pytest.raises(ValueError):
        config._auth_mode()
