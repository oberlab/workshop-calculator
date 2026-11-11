"""Planning prototype: tables in their own database, never in the metrics database.

`PLANNING_DATABASE_URL` (default: a SQLite file) holds courses in planning, the people list,
the history, the demo calendar and the sessions a final confirmation would write. The tables
are created on first use; there is no migration while this is a prototype.
"""

import datetime as dt
from functools import lru_cache

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, create_engine, event, func
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker
from sqlalchemy.pool import StaticPool

from metrics.config import get_settings

GEPLANT = "geplant"
BESTAETIGT = "bestaetigt"
DURCHGEFUEHRT = "durchgefuehrt"
ABGESCHLOSSEN = "abgeschlossen"
ABGESAGT = "abgesagt"
STAGES = {
    GEPLANT: "Geplant",
    BESTAETIGT: "Bestätigt",
    DURCHGEFUEHRT: "Durchgeführt",
    ABGESCHLOSSEN: "Abgeschlossen",
    ABGESAGT: "Abgesagt",
}

LEAD = "lead"
SUPPORT = "support"

# Calendar state of a course: no event yet, event matches, event gone, event differs.
CAL_NONE, CAL_OK, CAL_MISSING, CAL_OUTDATED = "none", "ok", "missing", "outdated"


class PlanningBase(DeclarativeBase):
    pass


class Person(PlanningBase):
    """Someone who can lead or support a course. `key` matches the metrics instructor key."""

    __tablename__ = "people"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    login: Mapped[str | None] = mapped_column(Text)  # display name of the login, for "Meine Kurse"


class Course(PlanningBase):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    stage: Mapped[str] = mapped_column(String(16), default=GEPLANT, index=True)
    date: Mapped[dt.date | None] = mapped_column(index=True)
    start_time: Mapped[dt.time | None]
    end_time: Mapped[dt.time | None]
    duration_hours: Mapped[float | None] = mapped_column(Float)
    course_name: Mapped[str | None] = mapped_column(Text)
    # Klasse/Alter, structured: kind is "klasse", "alter" (children, average age), "band" (adults)
    # or "lehrkraefte".
    class_kind: Mapped[str | None] = mapped_column(String(16))
    class_level: Mapped[int | None] = mapped_column(Integer)
    age: Mapped[int | None] = mapped_column(Integer)
    age_band: Mapped[str | None] = mapped_column(String(32))
    context: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(64))
    session_number: Mapped[int | None] = mapped_column(Integer)
    participants_female: Mapped[int | None] = mapped_column(Integer)
    participants_male: Mapped[int | None] = mapped_column(Integer)
    billed: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str | None] = mapped_column(Text)
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    calendar_uid: Mapped[str | None] = mapped_column(String(128))
    calendar_state: Mapped[str] = mapped_column(String(16), default=CAL_NONE)
    calendar_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    finalized_by: Mapped[str | None] = mapped_column(Text)
    finalized_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    alternatives: Mapped[list["AlternativeDate"]] = relationship(
        cascade="all, delete-orphan", order_by="AlternativeDate.date", lazy="selectin"
    )
    people: Mapped[list["CoursePerson"]] = relationship(cascade="all, delete-orphan", lazy="selectin")
    municipalities: Mapped[list["Municipality"]] = relationship(
        cascade="all, delete-orphan", order_by="Municipality.id", lazy="selectin"
    )

    def keys(self, role: str) -> list[str]:
        return [p.person_key for p in self.people if p.role == role]

    @property
    def all_keys(self) -> set[str]:
        return {p.person_key for p in self.people}

    @property
    def class_age_text(self) -> str | None:
        """Klasse/Alter in the Slack List format, so the existing parser and rules apply."""
        if self.class_kind == "lehrkraefte":
            return "Lehrkräfte"
        if self.class_kind == "band":
            return self.age_band
        if self.class_kind == "alter":
            return f"{self.age} Jahre" if self.age is not None else None
        if self.class_kind == "klasse" and self.class_level is not None:
            return f"Kl. {self.class_level}"
        return None

    @property
    def has_class_age(self) -> bool:
        return self.class_age_text is not None

    @property
    def title(self) -> str:
        return self.course_name or self.context or "Kurs ohne Namen"


class Place(PlanningBase):
    """A place entered in planning that is not in the instance's location list yet.

    Admins later copy complete entries into `seeds/locations.csv`; until then the planning
    uses them for the target group (school or not) and stops asking for the address.
    """

    __tablename__ = "places"

    name: Mapped[str] = mapped_column(Text, primary_key=True)
    street: Mapped[str | None] = mapped_column(Text)
    zip_code: Mapped[str | None] = mapped_column(String(16))
    city: Mapped[str | None] = mapped_column(Text)
    school: Mapped[bool | None] = mapped_column(Boolean)
    school_type: Mapped[str | None] = mapped_column(String(32))
    note: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def complete(self) -> bool:
        return bool(self.street and self.zip_code and self.city)

    @property
    def address(self) -> str:
        return f"{self.street}, {self.zip_code} {self.city}" if self.complete else ""


class AlternativeDate(PlanningBase):
    __tablename__ = "alternative_dates"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    date: Mapped[dt.date]


class CoursePerson(PlanningBase):
    __tablename__ = "course_people"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    person_key: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))


class Municipality(PlanningBase):
    """Participants of a course per home municipality (projects with participant_municipalities)."""

    __tablename__ = "course_municipalities"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id", ondelete="CASCADE"), index=True)
    municipality: Mapped[str] = mapped_column(Text)
    female: Mapped[int | None] = mapped_column(Integer)
    male: Mapped[int | None] = mapped_column(Integer)


class History(PlanningBase):
    """Who changed what and when. Kept when a course is deleted (course_id stays as a number)."""

    __tablename__ = "history"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(Integer, index=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    user: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    field: Mapped[str | None] = mapped_column(Text)
    old: Mapped[str | None] = mapped_column(Text)
    new: Mapped[str | None] = mapped_column(Text)


class DemoCalendarEvent(PlanningBase):
    """The built-in demo calendar, used while no CalDAV calendar is configured."""

    __tablename__ = "demo_calendar"

    uid: Mapped[str] = mapped_column(String(128), primary_key=True)
    summary: Mapped[str] = mapped_column(Text)
    start: Mapped[dt.datetime]
    end: Mapped[dt.datetime]
    location: Mapped[str | None] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)


class CalendarOrphan(PlanningBase):
    """A future calendar event without a course in Geplant/Bestätigt, as seen by the last check."""

    __tablename__ = "calendar_orphans"

    uid: Mapped[str] = mapped_column(String(128), primary_key=True)
    summary: Mapped[str] = mapped_column(Text)
    start: Mapped[dt.datetime]
    end: Mapped[dt.datetime]
    location: Mapped[str | None] = mapped_column(Text)
    ignored_by: Mapped[str | None] = mapped_column(Text)
    seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FinalSession(PlanningBase):
    """What the final confirmation would write into the metrics `sessions` table (prototype only)."""

    __tablename__ = "final_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(Integer, unique=True)
    source_key: Mapped[str] = mapped_column(String(64))
    date: Mapped[dt.date]
    course_name: Mapped[str | None] = mapped_column(Text)
    context: Mapped[str | None] = mapped_column(Text)
    location: Mapped[str | None] = mapped_column(Text)
    target_group: Mapped[str] = mapped_column(String(64))
    participants_total: Mapped[int | None] = mapped_column(Integer)
    record_json: Mapped[str] = mapped_column(Text)  # the full record as the sync would build it
    confirmed_by: Mapped[str] = mapped_column(Text)
    confirmed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


def _sqlite_foreign_keys(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@lru_cache
def get_planning_sessionmaker() -> sessionmaker[Session]:
    url = get_settings().planning_database_url
    if url.startswith("sqlite"):
        options: dict = {"connect_args": {"check_same_thread": False}}
        if url in ("sqlite://", "sqlite:///:memory:"):  # tests: one shared in-memory database
            options["poolclass"] = StaticPool
        engine = create_engine(url, **options)
        event.listen(engine, "connect", _sqlite_foreign_keys)
    else:
        engine = create_engine(url)
    PlanningBase.metadata.create_all(engine)
    return sessionmaker(engine, expire_on_commit=False)
