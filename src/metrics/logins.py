"""Server-side login sessions (`PROPOSAL.md` section 7).

The cookie holds only a random token; the database keeps a hash of it with who logged in, their
role at login time and the times. A session lasts at most 12 hours and ends after 2 hours without a
request; logout and an admin's "Überall abmelden" end it at once. Every request looks the session
up, so an ended session is refused at the next request. Ended sessions are kept with the time and
who ended them. The scheduler deletes a session row 30 days after it expired (`prune`, decided by
Alex on 2026-11-13); nothing else deletes rows.
"""

import datetime as dt
import hashlib
import logging
import secrets
from dataclasses import dataclass

from sqlalchemy import Boolean, DateTime, String, Text, delete, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, mapped_column

from metrics.auth import Login
from metrics.db import Base, get_sessionmaker

LIFETIME = dt.timedelta(hours=12)
IDLE = dt.timedelta(hours=2)
TOUCH = dt.timedelta(minutes=1)  # last_seen_at is written at most this often
RETENTION = dt.timedelta(days=30)  # how long a session row is kept after it expired

log = logging.getLogger(__name__)


class LoginSession(Base):
    """One login in one browser. Kept apart from the domain models (WM-100)."""

    __tablename__ = "login_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    subject: Mapped[str] = mapped_column(Text, index=True)
    user_name: Mapped[str] = mapped_column(Text)
    admin: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    ended_by: Mapped[str | None] = mapped_column(Text)


@dataclass(frozen=True)
class Active:
    """An open session as the review page shows it."""

    subject: str
    user_name: str
    admin: bool
    created_at: dt.datetime
    last_seen_at: dt.datetime


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _utc(value: dt.datetime) -> dt.datetime:
    # SQLite (tests) returns naive datetimes; PostgreSQL returns aware ones.
    return value if value.tzinfo else value.replace(tzinfo=dt.UTC)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _open(row: LoginSession, now: dt.datetime) -> bool:
    return row.ended_at is None and _utc(row.expires_at) > now and _utc(row.last_seen_at) + IDLE > now


def start(login: Login, now: dt.datetime | None = None) -> str:
    """Open a session for `login` and return the token for the cookie."""
    now = now or _now()
    token = secrets.token_urlsafe(32)
    with get_sessionmaker()() as session:
        session.add(
            LoginSession(
                token_hash=_hash(token),
                subject=login.subject or login.user,
                user_name=login.user,
                admin=login.admin,
                created_at=now,
                last_seen_at=now,
                expires_at=now + LIFETIME,
            )
        )
        session.commit()
    return token


def lookup(token: str | None, now: dt.datetime | None = None) -> Login | None:
    """Who is logged in with this token, or None if the session is unknown, ended or timed out."""
    if not token:
        return None
    now = now or _now()
    try:
        with get_sessionmaker()() as session:
            row = session.get(LoginSession, _hash(token))
            if row is None or not _open(row, now):
                return None
            if now - _utc(row.last_seen_at) > TOUCH:
                row.last_seen_at = now
                session.commit()
            return Login(row.user_name, row.admin, row.subject)
    except SQLAlchemyError:
        # Fail closed: without the database nobody is logged in. No details in the log.
        log.warning("login session lookup failed: database error")
        return None


def end(token: str | None, now: dt.datetime | None = None) -> None:
    """Logout: end this session."""
    if not token:
        return
    with get_sessionmaker()() as session:
        row = session.get(LoginSession, _hash(token))
        if row is not None and row.ended_at is None:
            row.ended_at, row.ended_by = now or _now(), "Abmelden"
            session.commit()


def end_all(subject: str, ended_by: str, now: dt.datetime | None = None) -> int:
    """ "Überall abmelden": end every session of one person not ended yet; returns how many."""
    with get_sessionmaker()() as session:
        result = session.execute(
            update(LoginSession)
            .where(LoginSession.subject == subject, LoginSession.ended_at.is_(None))
            .values(ended_at=now or _now(), ended_by=ended_by)
        )
        session.commit()
        return result.rowcount or 0


def active(now: dt.datetime | None = None) -> list[Active]:
    """Open sessions, newest first."""
    now = now or _now()
    with get_sessionmaker()() as session:
        rows = session.scalars(
            select(LoginSession)
            .where(LoginSession.ended_at.is_(None))
            .order_by(LoginSession.last_seen_at.desc())
        )
        return [
            Active(r.subject, r.user_name, r.admin, _utc(r.created_at), _utc(r.last_seen_at))
            for r in rows
            if _open(r, now)
        ]


def prune(now: dt.datetime) -> int:
    """Delete session rows whose lifetime ended more than RETENTION ago; returns how many.

    `expires_at` is the latest moment a session can have been valid (logout, "Überall abmelden"
    and the idle timeout only end it earlier), so every row is kept for at least 30 days after
    its last possible use.
    """
    with get_sessionmaker()() as session, session.begin():
        result = session.execute(delete(LoginSession).where(LoginSession.expires_at < now - RETENTION))
    return result.rowcount
