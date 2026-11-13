"""Database models and session handling (PROPOSAL.md section 3).

One schema for planning, tracking and analysis. The German names of the proposal map to the
tables as follows: Ort = places, Kontext = contexts, Format = formats, Person = people,
Kurs = courses, Termin = appointments, Herkunft = appointment_origins, Verlauf = history,
Vorschlag = proposals, Hinweis-Entscheidung = hint_decisions.

List entries (places, contexts, formats, people) carry a status: `vorgeschlagen` while a team
member's proposal waits for an admin, `bestätigt` afterwards. Imported names become confirmed
entries exactly as recorded; nothing is renamed or merged automatically.
"""

import datetime as dt
from decimal import Decimal
from functools import lru_cache

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Table,
    Text,
    Time,
    UniqueConstraint,
    create_engine,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from metrics.config import get_settings

# Status of a list entry.
PROPOSED = "vorgeschlagen"
CONFIRMED = "bestätigt"
LIST_STATUSES = (PROPOSED, CONFIRMED)

# Stage of a Termin. Abgeschlossen (finally confirmed) is what the charts count.
GEPLANT = "Geplant"
BESTAETIGT = "Bestätigt"
DURCHGEFUEHRT = "Durchgeführt"
ABGESCHLOSSEN = "Abgeschlossen"
ABGESAGT = "Abgesagt"
STAGES = (GEPLANT, BESTAETIGT, DURCHGEFUEHRT, ABGESCHLOSSEN, ABGESAGT)
STAGE_COUNTED = ABGESCHLOSSEN

# Recorded status of a held session in the legacy data and the Slack List.
STATUS_DONE = "Durchgeführt"


class Base(DeclarativeBase):
    pass


def _status_column() -> Mapped[str]:
    return mapped_column(String(16), default=CONFIRMED, server_default=CONFIRMED)


def _created_at() -> Mapped[dt.datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class SchoolType(Base):
    """School type codes and labels (seeds/school_types.csv); `position` is the chart order."""

    __tablename__ = "school_types"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    label: Mapped[str] = mapped_column(Text)
    position: Mapped[int] = mapped_column(Integer)


class Place(Base):
    """Ort: where a course takes place. `is_school` is None for a place nobody has described yet."""

    __tablename__ = "places"
    __table_args__ = (CheckConstraint("status IN ('vorgeschlagen', 'bestätigt')", name="ck_places_status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    is_school: Mapped[bool | None] = mapped_column(Boolean)
    school_type: Mapped[str | None] = mapped_column(String(16))  # code, see school_types
    address: Mapped[str | None] = mapped_column(Text)  # "" = deliberately none (private places)
    municipality: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = _status_column()
    created_at: Mapped[dt.datetime] = _created_at()


class Context(Base):
    """Kontext: the project or setting a course belongs to.

    Contexts with the same `page_slug` share one project page titled `page_title`.
    `teacher_training`: every course in it counts as Lehrkräfte.
    """

    __tablename__ = "contexts"
    __table_args__ = (CheckConstraint("status IN ('vorgeschlagen', 'bestätigt')", name="ck_contexts_status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    is_project: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    page_slug: Mapped[str | None] = mapped_column(String(64))
    page_title: Mapped[str | None] = mapped_column(Text)
    asks_municipalities: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    teacher_training: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    status: Mapped[str] = _status_column()
    created_at: Mapped[dt.datetime] = _created_at()


class Format(Base):
    """Format: a course we offer (name, category, usual duration).

    Legacy entries without a course name use the unnamed format of their category, so the
    category is never lost; name and category together are unique (NULL counts as a value).
    """

    __tablename__ = "formats"
    __table_args__ = (
        UniqueConstraint(
            "name", "category", name="uq_formats_name_category", postgresql_nulls_not_distinct=True
        ),
        CheckConstraint("status IN ('vorgeschlagen', 'bestätigt')", name="ck_formats_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(64))
    duration_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    overhead_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    status: Mapped[str] = _status_column()
    created_at: Mapped[dt.datetime] = _created_at()


class Person(Base):
    """Person: a team member, or a legacy first name without a Slack user (inactive)."""

    __tablename__ = "people"
    __table_args__ = (CheckConstraint("status IN ('vorgeschlagen', 'bestätigt')", name="ck_people_status"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    slack_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    email: Mapped[str | None] = mapped_column(Text, unique=True)
    kimai_user: Mapped[str | None] = mapped_column(Text)
    image: Mapped[bytes | None] = mapped_column(LargeBinary)
    image_type: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    weekly_hours: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    status: Mapped[str] = _status_column()
    created_at: Mapped[dt.datetime] = _created_at()

    @property
    def key(self) -> str:
        """The key the analysis has always used: the Slack user ID, else the recorded name."""
        return self.slack_id or self.name


class Course(Base):
    """Kurs: what the Termine of a series share. A single date is a course with one Termin."""

    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(primary_key=True)
    format_id: Mapped[int | None] = mapped_column(ForeignKey("formats.id"), index=True)
    context_id: Mapped[int | None] = mapped_column(ForeignKey("contexts.id"), index=True)
    place_id: Mapped[int | None] = mapped_column(ForeignKey("places.id"), index=True)
    class_age_text: Mapped[str | None] = mapped_column(Text)
    class_level: Mapped[int | None] = mapped_column(Integer)
    age: Mapped[int | None] = mapped_column(Integer)
    age_band: Mapped[str | None] = mapped_column(String(32))
    target_group: Mapped[str] = mapped_column(String(64), index=True)
    note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _created_at()

    format: Mapped[Format | None] = relationship()
    context: Mapped[Context | None] = relationship()
    place: Mapped[Place | None] = relationship()
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="course")


appointment_support = Table(
    "appointment_support",
    Base.metadata,
    Column("appointment_id", ForeignKey("appointments.id", ondelete="CASCADE"), primary_key=True),
    Column("person_id", ForeignKey("people.id"), primary_key=True, index=True),
)


class Appointment(Base):
    """Termin: one date of a course, the counting unit (an Einsatz is a held Termin).

    `number` is the Terminnummer; legacy rows keep the recorded one. `participants_total` is the
    recorded total (legacy before 2024-07-29 has nothing else); new Termine record the four
    counts and keep the total as their sum. `source` and `source_key` say where an imported
    row came from (e.g. `baseline`, `baseline:123`), `source_data` keeps its recorded values.
    """

    __tablename__ = "appointments"
    __table_args__ = (
        CheckConstraint(
            "stage IN ('Geplant', 'Bestätigt', 'Durchgeführt', 'Abgeschlossen', 'Abgesagt')",
            name="ck_appointments_stage",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), index=True)
    number: Mapped[int | None] = mapped_column(Integer)
    date: Mapped[dt.date | None] = mapped_column(Date, index=True)
    start_time: Mapped[dt.time | None] = mapped_column(Time)
    end_time: Mapped[dt.time | None] = mapped_column(Time)
    duration_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    stage: Mapped[str] = mapped_column(String(16), index=True)
    lead_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"), index=True)
    participants_female: Mapped[int | None] = mapped_column(Integer)
    participants_male: Mapped[int | None] = mapped_column(Integer)
    participants_diverse: Mapped[int | None] = mapped_column(Integer)
    participants_unspecified: Mapped[int | None] = mapped_column(Integer)  # keine Angabe
    participants_total: Mapped[int | None] = mapped_column(Integer)
    billed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    calendar_uid: Mapped[str | None] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(32), default="app", server_default="app", index=True)
    source_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    source_data: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    course: Mapped[Course] = relationship(back_populates="appointments")
    lead: Mapped[Person | None] = relationship(foreign_keys=[lead_id])
    support: Mapped[list[Person]] = relationship(secondary=appointment_support)
    origins: Mapped[list["AppointmentOrigin"]] = relationship(cascade="all, delete-orphan")


class AppointmentOrigin(Base):
    """Herkunft: participants of a Termin per home municipality (adding up to the total)."""

    __tablename__ = "appointment_origins"

    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), primary_key=True
    )
    municipality: Mapped[str] = mapped_column(Text, primary_key=True)
    participants: Mapped[int] = mapped_column(Integer)


class HistoryEntry(Base):
    """Verlauf: one changed field of any row, with who, when and why."""

    __tablename__ = "history"
    __table_args__ = (Index("ix_history_row", "table_name", "row_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    table_name: Mapped[str] = mapped_column(String(64))
    row_id: Mapped[int] = mapped_column(Integer)
    field: Mapped[str | None] = mapped_column(String(64))
    old_value: Mapped[str | None] = mapped_column(Text)
    new_value: Mapped[str | None] = mapped_column(Text)
    changed_by: Mapped[str] = mapped_column(Text)
    changed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    reason: Mapped[str | None] = mapped_column(Text)


class Proposal(Base):
    """Vorschlag: a proposed list entry or correction, until an admin decides."""

    __tablename__ = "proposals"
    __table_args__ = (CheckConstraint("kind IN ('eintrag', 'korrektur')", name="ck_proposals_kind"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    target_table: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[int | None] = mapped_column(Integer)
    proposed_values: Mapped[dict] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    proposed_by: Mapped[str] = mapped_column(Text)
    proposed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decision: Mapped[str | None] = mapped_column(String(32))
    decided_by: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    decision_reason: Mapped[str | None] = mapped_column(Text)


class HintDecision(Base):
    """Hinweis-Entscheidung: a warning on a Termin that someone accepted, with the reason."""

    __tablename__ = "hint_decisions"

    id: Mapped[int] = mapped_column(primary_key=True)
    rule: Mapped[str] = mapped_column(String(64))
    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id", ondelete="CASCADE"), index=True)
    decision: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    decided_by: Mapped[str] = mapped_column(Text)
    decided_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JobRun(Base):
    """One run of a scheduler job (WM-103)."""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool | None] = mapped_column(Boolean)
    message: Mapped[str | None] = mapped_column(Text)


class ImportRun(Base):
    """One legacy or baseline load, shown as the data timestamp on the page."""

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
