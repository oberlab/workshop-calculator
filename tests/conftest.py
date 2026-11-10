from pathlib import Path

import pytest

from metrics import auth, oidc
from metrics.config import get_settings
from metrics.db import get_sessionmaker
from metrics.instance import Instance, get_instance, load_instance

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE_INSTANCE = ROOT / "instance.example"

# Settings a developer shell may have loaded from .env; tests must not depend on them.
_ENVIRONMENT = (
    "SLACK_BOT_TOKEN",
    "SLACK_LIST_ID",
    "AUTH_MODE",
    "TEAM_PASSWORD_HASH",
    "SESSION_SECRET",
    "OIDC_ISSUER",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
    "OIDC_ALLOWED_GROUP",
    "OIDC_ADMIN_GROUP",
    "OIDC_GROUPS_CLAIM",
    "PUBLIC_URL",
    "APP_VERSION",
)


# The cached functions as imported; tests may replace them on the module while they run.
_CACHED = (get_settings, get_instance, get_sessionmaker, auth._secret, oidc.discovery)


def _clear_caches() -> None:
    for cached in _CACHED:
        cached.cache_clear()


@pytest.fixture(autouse=True)
def public_example_only(monkeypatch):
    """Every test sees the public example instance and no private settings.

    The app defaults to INSTANCE_DIR=instance, the private directory on a developer PC,
    so without this a test could pass or fail depending on whose machine runs it. The
    database URL points at a port nothing listens on: a test that reaches the database
    by accident fails at once instead of reading real data.
    """
    for name in _ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("INSTANCE_DIR", str(EXAMPLE_INSTANCE))
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://test:test@127.0.0.1:9/none")
    _clear_caches()
    yield
    _clear_caches()


@pytest.fixture
def instance() -> Instance:
    """The public example instance: synthetic locations, mappings and theme."""
    return load_instance(EXAMPLE_INSTANCE)
