import datetime as dt

from metrics import db, logins
from metrics.auth import Login
from tests.test_web import memory_login_db

T0 = dt.datetime(2026, 11, 13, 8, 0, tzinfo=dt.UTC)


def test_cookie_holds_only_a_random_token(monkeypatch):
    maker = memory_login_db(monkeypatch)
    token = logins.start(Login("Alex K", admin=True, subject="sub-1"), now=T0)
    assert "Alex" not in token and len(token) >= 40
    with maker() as session:
        (row,) = session.query(logins.LoginSession).all()
    assert row.token_hash != token  # the database keeps only a hash
    assert logins.lookup(token, now=T0) == Login("Alex K", admin=True, subject="sub-1")


def test_idle_timeout_and_lifetime(monkeypatch):
    memory_login_db(monkeypatch)
    token = logins.start(Login("Alex K", subject="sub-1"), now=T0)
    # Used every 90 minutes: stays open until the 12 hours are over.
    for minutes in range(90, 12 * 60, 90):
        assert logins.lookup(token, now=T0 + dt.timedelta(minutes=minutes)) is not None
    assert logins.lookup(token, now=T0 + logins.LIFETIME + dt.timedelta(seconds=1)) is None

    idle = logins.start(Login("Alex K", subject="sub-1"), now=T0)
    assert logins.lookup(idle, now=T0 + logins.IDLE + dt.timedelta(seconds=1)) is None


def test_logout_and_sign_out_everywhere(monkeypatch):
    maker = memory_login_db(monkeypatch)
    first = logins.start(Login("Alex K", admin=True, subject="sub-1"), now=T0)
    second = logins.start(Login("Alex K", admin=True, subject="sub-1"), now=T0)
    other = logins.start(Login("Bea", subject="sub-2"), now=T0)

    logins.end(first, now=T0)
    assert logins.lookup(first, now=T0) is None
    assert sorted(a.subject for a in logins.active(now=T0)) == ["sub-1", "sub-2"]

    assert logins.end_all("sub-1", ended_by="Bea", now=T0) == 1
    assert logins.lookup(second, now=T0) is None
    assert logins.lookup(other, now=T0) is not None
    with maker() as session:
        ended = {r.ended_by for r in session.query(logins.LoginSession) if r.ended_at}
    assert ended == {"Abmelden", "Bea"}  # rows are kept with who ended them


def test_unknown_token_and_database_error(monkeypatch):
    memory_login_db(monkeypatch)
    assert logins.lookup(None) is None
    assert logins.lookup("geraten") is None
    # conftest points DATABASE_URL at a port nothing listens on: fail closed, nobody is logged in.
    monkeypatch.setattr(logins, "get_sessionmaker", db.get_sessionmaker)
    assert logins.lookup("geraten") is None


def test_sessions_on_postgresql(db):
    # The migrated table (0004) on the test server; the app's sessionmaker uses it through `db`.
    token = logins.start(Login("Alex K", admin=True, subject="sub-1"), now=T0)
    assert logins.lookup(token, now=T0 + dt.timedelta(minutes=5)).admin is True
    assert logins.end_all("sub-1", ended_by="Bea", now=T0 + dt.timedelta(minutes=6)) == 1
    assert logins.lookup(token, now=T0 + dt.timedelta(minutes=7)) is None
    row = db.get(logins.LoginSession, logins._hash(token))
    assert row.ended_by == "Bea" and row.ended_at.tzinfo is not None


def test_rows_are_deleted_30_days_after_they_expired(db):
    old = logins.start(Login("Alex K", admin=False, subject="sub-1"), now=T0)
    recent = logins.start(Login("Bea", admin=False, subject="sub-2"), now=T0 + dt.timedelta(days=1))
    # T0's session expired at T0 + 12 h; 30 days later plus a minute it goes, the other stays.
    assert logins.prune(T0 + dt.timedelta(hours=12, days=30, minutes=1)) == 1
    assert db.get(logins.LoginSession, logins._hash(old)) is None
    assert db.get(logins.LoginSession, logins._hash(recent)) is not None
