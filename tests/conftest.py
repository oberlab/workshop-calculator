import os
import subprocess
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, make_url, text
from sqlalchemy.orm import Session

from metrics import auth, oidc
from metrics.config import get_settings
from metrics.db import get_sessionmaker
from metrics.instance import Instance, get_instance, load_instance
from metrics.planning import vocabulary
from metrics.planning.models import get_planning_sessionmaker
from metrics.web import login, public

ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
EXAMPLE_INSTANCE = ROOT / "instance.example"

# Settings a developer shell may have loaded from .env; tests must not depend on them.
_ENVIRONMENT = (
    "SLACK_BOT_TOKEN",
    "SLACK_LIST_ID",
    "AUTH_MODE",
    "TEAM_PASSWORD_HASH",
    "ALLOW_PASSWORD_LOGIN",
    "SESSION_SECRET",
    "OIDC_ISSUER",
    "OIDC_CLIENT_ID",
    "OIDC_CLIENT_SECRET",
    "OIDC_ALLOWED_GROUP",
    "OIDC_ADMIN_GROUP",
    "OIDC_GROUPS_CLAIM",
    "PUBLIC_URL",
    "APP_VERSION",
    "PLANNING_DATABASE_URL",
    "CALDAV_URL",
    "CALDAV_USERNAME",
    "CALDAV_PASSWORD",
)


# The cached functions as imported; tests may replace them on the module while they run.
_CACHED = (
    get_settings,
    get_instance,
    get_sessionmaker,
    get_planning_sessionmaker,
    auth._secret,
    oidc.discovery,
)


def _clear_caches() -> None:
    for cached in _CACHED:
        cached.cache_clear()
    vocabulary.clear_cache()
    # In-process rate limits and the public data cache must not carry over between tests.
    login.LOGIN_LIMIT.reset()
    public.DATA_LIMIT.reset()
    public.clear_cache()


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
    # The planning prototype gets a fresh in-memory database per test.
    monkeypatch.setenv("PLANNING_DATABASE_URL", "sqlite://")
    _clear_caches()
    yield
    _clear_caches()


@pytest.fixture
def instance() -> Instance:
    """The public example instance: synthetic locations, mappings and theme."""
    return load_instance(EXAMPLE_INSTANCE)


# --- PostgreSQL ---------------------------------------------------------------------------
# Tests that need the database use the `db` fixture. The server comes from TEST_DATABASE_URL
# or from the `db-test` service in compose.test.yaml; each run creates its own database there
# and drops it at the end. Without a server these tests are skipped locally and fail in CI.


def _test_server_url() -> str | None:
    if url := os.environ.get("TEST_DATABASE_URL"):
        return url
    try:
        result = subprocess.run(
            ["docker", "compose", "-f", str(ROOT / "compose.test.yaml"), "port", "db-test", "5432"],
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    address = result.stdout.strip()
    return f"postgresql+psycopg://test:test@{address}/test" if address else None


class PgServer:
    def __init__(self, url: str):
        self.url = make_url(url)
        self.admin = create_engine(self.url, isolation_level="AUTOCOMMIT")
        self.created: list[str] = []

    def create_database(self) -> str:
        name = f"wm_test_{uuid.uuid4().hex[:12]}"
        with self.admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        self.created.append(name)
        return self.url.set(database=name).render_as_string(hide_password=False)

    def drop_all(self) -> None:
        with self.admin.connect() as conn:
            for name in self.created:
                conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        self.admin.dispose()


@pytest.fixture(scope="session")
def pg_server():
    url = _test_server_url()
    if url is None:
        message = "no test database: run `docker compose -f compose.test.yaml up -d --wait`"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)
    server = PgServer(url)
    yield server
    server.drop_all()


def alembic_upgrade(url: str, revision: str = "head", instance_dir: Path = EXAMPLE_INSTANCE) -> None:
    """Run the migrations against `url` (the migrations read DATABASE_URL and INSTANCE_DIR)."""
    from alembic import command
    from alembic.config import Config

    saved = {k: os.environ.get(k) for k in ("DATABASE_URL", "INSTANCE_DIR")}
    os.environ.update(DATABASE_URL=url, INSTANCE_DIR=str(instance_dir))
    get_settings.cache_clear()
    try:
        command.upgrade(Config(str(ROOT / "alembic.ini")), revision)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        get_settings.cache_clear()


@pytest.fixture(scope="session")
def migrated_url(pg_server) -> str:
    url = pg_server.create_database()
    alembic_upgrade(url)
    return url


@pytest.fixture
def db(migrated_url, monkeypatch):
    """A session on a migrated, empty PostgreSQL database; the app's sessionmaker uses it too."""
    monkeypatch.setenv("DATABASE_URL", migrated_url)
    get_sessionmaker.cache_clear()
    engine = create_engine(migrated_url)
    with Session(engine) as session:
        yield session
    with engine.begin() as conn:
        tables = conn.execute(
            text(
                "SELECT tablename FROM pg_tables"
                " WHERE schemaname = 'public' AND tablename <> 'alembic_version'"
            )
        ).scalars()
        conn.execute(text(f"TRUNCATE {', '.join(tables)} RESTART IDENTITY CASCADE"))
    engine.dispose()
    get_sessionmaker.cache_clear()
