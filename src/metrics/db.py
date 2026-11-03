"""Database models and session handling."""

import datetime as dt
from decimal import Decimal
from functools import lru_cache

from sqlalchemy import DateTime, Integer, LargeBinary, Numeric, String, Text, create_engine, func
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from metrics.config import get_settings

STATUS_DONE = "Durchgeführt"


class Base(DeclarativeBase):
    pass


class SessionRow(Base):
    """One held or planned session (Einsatz), the unit everything is counted in."""

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    source: Mapped[str] = mapped_column(String(32), index=True)
    source_key: Mapped[str] = mapped_column(String(128), unique=True)
    source_state: Mapped[str] = mapped_column(String(32), default="active", server_default="active")
    date: Mapped[dt.date | None] = mapped_column(index=True)
    course_name: Mapped[str | None] = mapped_column(Text)
    duration_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    location: Mapped[str | None] = mapped_column(Text)
    school_type: Mapped[str | None] = mapped_column(String(32))
    class_age_text: Mapped[str | None] = mapped_column(Text)
    class_level: Mapped[int | None] = mapped_column(Integer)
    age: Mapped[int | None] = mapped_column(Integer)
    age_band: Mapped[str | None] = mapped_column(String(32))
    target_group: Mapped[str] = mapped_column(String(64), index=True)
    participants_female: Mapped[int | None] = mapped_column(Integer)
    participants_male: Mapped[int | None] = mapped_column(Integer)
    participants_total: Mapped[int | None] = mapped_column(Integer)
    category: Mapped[str | None] = mapped_column(String(64), index=True)
    context: Mapped[str | None] = mapped_column(Text, index=True)
    session_number: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str | None] = mapped_column(String(32), index=True)
    instructor: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class Instructor(Base):
    """A person who leads sessions: Slack user (name and picture from Slack) or legacy first name.

    `key` is the Slack user ID, or the recorded first name for people without a Slack user.
    """

    __tablename__ = "instructors"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    image: Mapped[bytes | None] = mapped_column(LargeBinary)
    image_type: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ImportRun(Base):
    """One legacy load or Slack sync, shown as the data timestamp on the page."""

    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    rows: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    # The timeout keeps /healthz and the pages from hanging when the database does not answer.
    engine = create_engine(
        get_settings().database_url, pool_pre_ping=True, connect_args={"connect_timeout": 10}
    )
    return sessionmaker(engine, expire_on_commit=False)
