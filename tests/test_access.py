"""The central access rule: every route is classified, and none opens without the right role."""

import pytest

from metrics.web import access
from metrics.web.app import app
from tests.test_web import _client

# Values for the path parameters, so every route can be requested.
PARAMS = {
    "{slug}": "projekt-a",
    "{course_id:int}": "1",
    "{key}": "U1",
    "{name}": "x",
    "{kind}": "orte",
    "{entry_id:int}": "1",
}


def _keys() -> list[str]:
    return access.route_keys(app)


def _url(path: str) -> str:
    for placeholder, value in PARAMS.items():
        path = path.replace(placeholder, value)
    assert "{" not in path, f"no test value for a parameter in {path}"
    return path


def _protected(level: str) -> list[tuple[str, str]]:
    return [
        (key.split(" ", 1)[0], _url(key.split(" ", 1)[1]))
        for key in _keys()
        if access.level(key) == level or (level == access.TEAM and access.level(key) == access.ADMIN)
    ]


def test_every_route_is_classified():
    keys = _keys()
    listed = access.PUBLIC_ROUTES | access.TEAM_ROUTES | access.ADMIN_ROUTES
    assert sorted(set(keys) - listed) == [], "add the new route to a list in metrics/web/access.py"
    assert sorted(listed - set(keys)) == [], "a listed route no longer exists"
    assert not access.PUBLIC_ROUTES & access.TEAM_ROUTES
    assert not access.PUBLIC_ROUTES & access.ADMIN_ROUTES
    assert not access.TEAM_ROUTES & access.ADMIN_ROUTES


def test_mounted_apps_are_public():
    # The rule runs as a route dependency, which a mounted app (static files) never sees.
    mounts = [key for key in _keys() if key.startswith("MOUNT ")]
    assert mounts and all(access.level(key) == access.PUBLIC for key in mounts)


def test_unlisted_routes_need_team():
    assert access.level("GET /neu-und-vergessen") == access.TEAM


@pytest.mark.parametrize(("method", "url"), _protected(access.TEAM))
def test_no_route_opens_without_login(monkeypatch, method, url):
    response = _client(monkeypatch).request(method, url, follow_redirects=False)
    if response.status_code == 303:  # a page sends the visitor to the login
        assert method == "GET" and response.headers["location"] == "/login"
    else:
        assert response.status_code == 401


@pytest.mark.parametrize(("method", "url"), _protected(access.ADMIN))
def test_admin_routes_refuse_team_members(monkeypatch, method, url):
    client = _client(monkeypatch)
    assert (
        client.post("/login", data={"password": "richtiges-passwort"}, follow_redirects=False).status_code
        == 303
    )
    response = client.request(method, url, follow_redirects=False)
    assert response.status_code == 403
