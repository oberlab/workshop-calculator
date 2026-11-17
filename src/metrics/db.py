"""Database models and session handling (PROPOSAL.md section 3).

One schema for planning, tracking and analysis. The German names of the proposal map to the
tables as follows: Ort = places, Kontext = contexts, Format = formats, Person = people,
Kurs = courses, Termin = appointments, Herkunft = appointment_origins, Verlauf = history,
Vorschlag = proposals, Hinweis-Entscheidung = hint_decisions, Hinweis = hints,
Kontaktperson = contacts, Material = equipment (with the needs of a format in format_equipment and
of a Termin in appointment_equipment).

List entries (places, contexts, formats, people, equipment) carry a status: `vorgeschlagen` while a team
member's proposal waits for an admin, `bestätigt` afterwards. An admin may instead merge an entry
into another one (`zusammengeführt`, `merged_into_id` names the entry that replaces it; its courses
and Termine follow) or reject a proposal (`abgelehnt`). Both rows stay; nothing is deleted.
Imported names become confirmed entries exactly as recorded; nothing is renamed or merged
automatically.
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
    case,
    create_engine,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.hybrid import hybrid_property
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, sessionmaker

from metrics.config import get_settings

# Status of a list entry. Only active entries (proposed or confirmed) are offered for new choices.
PROPOSED = "vorgeschlagen"
CONFIRMED = "bestätigt"
REJECTED = "abgelehnt"
MERGED = "zusammengeführt"
ACTIVE_STATUSES = (PROPOSED, CONFIRMED)
LIST_STATUSES = (PROPOSED, CONFIRMED, REJECTED, MERGED)

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


def _list_checks(table: str) -> tuple[CheckConstraint, CheckConstraint]:
    """Status values of a list entry, and `merged_into_id` set exactly for a merged entry."""
    return (
        CheckConstraint(
            "status IN ('vorgeschlagen', 'bestätigt', 'abgelehnt', 'zusammengeführt')",
            name=f"ck_{table}_status",
        ),
        CheckConstraint(
            "(status = 'zusammengeführt') = (merged_into_id IS NOT NULL)", name=f"ck_{table}_merged"
        ),
    )


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
    __table_args__ = _list_checks("places")

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    is_school: Mapped[bool | None] = mapped_column(Boolean)
    school_type: Mapped[str | None] = mapped_column(String(16))  # code, see school_types
    address: Mapped[str | None] = mapped_column(Text)  # "" = deliberately none (private places)
    municipality: Mapped[str | None] = mapped_column(Text)
    lat: Mapped[float | None] = mapped_column(Float)
    lon: Mapped[float | None] = mapped_column(Float)
    # Extra hours per person on a Termin here (travel), for the workload estimate (WM-131).
    travel_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    status: Mapped[str] = _status_column()
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("places.id"))
    created_at: Mapped[dt.datetime] = _created_at()


class Context(Base):
    """Kontext: the project or setting a course belongs to.

    Contexts with the same `page_slug` share one project page titled `page_title`.
    `teacher_training`: every course in it counts as Lehrkräfte.
    """

    __tablename__ = "contexts"
    __table_args__ = _list_checks("contexts")

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    is_project: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    page_slug: Mapped[str | None] = mapped_column(String(64))
    page_title: Mapped[str | None] = mapped_column(Text)
    asks_municipalities: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    teacher_training: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    status: Mapped[str] = _status_column()
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("contexts.id"))
    created_at: Mapped[dt.datetime] = _created_at()


class Format(Base):
    """Format: a course we offer (name, category, usual duration).

    Legacy entries without a course name use the unnamed format of their category, so the
    category is never lost; name and category together are unique (NULL counts as a value).
    `continuous` (fortlaufend, e.g. JugendLab): every app Termin of its courses counts its
    participants, not only Terminnummer 1 (WM-114, `metrics.metrics.COUNTS_PARTICIPANTS`).
    """

    __tablename__ = "formats"
    __table_args__ = (
        UniqueConstraint(
            "name", "category", name="uq_formats_name_category", postgresql_nulls_not_distinct=True
        ),
        *_list_checks("formats"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None] = mapped_column(Text)
    category: Mapped[str | None] = mapped_column(String(64))
    duration_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    overhead_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    continuous: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    status: Mapped[str] = _status_column()
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("formats.id"))
    created_at: Mapped[dt.datetime] = _created_at()

    # What the format usually needs (WM-140); a new Termin copies it.
    equipment: Mapped[list["FormatEquipment"]] = relationship(
        cascade="all, delete-orphan", order_by="FormatEquipment.equipment_id"
    )


class Equipment(Base):
    """Material: a piece of critical equipment (e.g. a kit of Calliope mini) and how many there are.

    Only critical equipment is listed (PROPOSAL.md 4.5). `quantity` is the number available; None
    while a proposal does not say it yet (then nothing can be short). Equipment never counts in a
    chart or key figure.
    """

    __tablename__ = "equipment"
    __table_args__ = _list_checks("equipment")

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text, unique=True)
    quantity: Mapped[int | None] = mapped_column(Integer)
    note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = _status_column()
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("equipment.id"))
    created_at: Mapped[dt.datetime] = _created_at()


class FormatEquipment(Base):
    """How much of a piece of equipment a format usually needs."""

    __tablename__ = "format_equipment"
    __table_args__ = (CheckConstraint("quantity > 0", name="ck_format_equipment_quantity"),)

    format_id: Mapped[int] = mapped_column(ForeignKey("formats.id", ondelete="CASCADE"), primary_key=True)
    equipment_id: Mapped[int] = mapped_column(ForeignKey("equipment.id"), primary_key=True, index=True)
    quantity: Mapped[int] = mapped_column(Integer)

    equipment: Mapped[Equipment] = relationship()


class Person(Base):
    """Person: a team member, or a legacy first name without a Slack user (inactive)."""

    __tablename__ = "people"
    __table_args__ = _list_checks("people")

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(Text)
    slack_id: Mapped[str | None] = mapped_column(String(32), unique=True)
    email: Mapped[str | None] = mapped_column(Text, unique=True)
    kimai_user: Mapped[str | None] = mapped_column(Text)
    # The Kimai user ID, set by an admin when the e-mail in Kimai differs (WM-142); the Kimai job
    # matches this first, then the e-mail.
    kimai_user_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    image: Mapped[bytes | None] = mapped_column(LargeBinary)
    image_type: Mapped[str | None] = mapped_column(String(64))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    weekly_hours: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    # The login ("sub" of OpenID Connect) linked to this person by e-mail at its first login.
    login_subject: Mapped[str | None] = mapped_column(Text, unique=True)
    status: Mapped[str] = _status_column()
    merged_into_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"))
    created_at: Mapped[dt.datetime] = _created_at()

    @property
    def key(self) -> str:
        """The key the analysis has always used: the Slack user ID, else the recorded name."""
        return self.slack_id or self.name


class Absence(Base):
    """Abwesenheit (WM-142): one day a person is away, read from Kimai and never edited in the app.

    One row per Kimai absence and day. An absence that is no longer in Kimai is not deleted: the
    Kimai job sets `withdrawn_at`, and only rows without it count; if it comes back, the mark goes.
    `kind` is the German kind (Urlaub, Krank, …); only the person themselves sees it, everyone else
    sees "abwesend" (`metrics.absences.label`). Kimai's comment or reason is never stored.
    """

    __tablename__ = "absences"
    __table_args__ = (
        UniqueConstraint("kimai_key", "day", name="uq_absences_kimai_key_day"),
        Index("ix_absences_person_day", "person_id", "day"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    person_id: Mapped[int] = mapped_column(ForeignKey("people.id"))
    day: Mapped[dt.date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(Text)
    half_day: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    kimai_key: Mapped[str] = mapped_column(Text)  # Kimai's absence ID
    first_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    withdrawn_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))

    person: Mapped[Person] = relationship()


class Contact(Base):
    """Kontaktperson at a place (WM-141), e.g. the class teacher at a school; chosen on a course.

    Personal data of people outside the team, kept to what planning needs: never in exports, charts,
    the calendar, Slack or the Verlauf text (the Verlauf records only that a contact was added,
    changed, chosen or deleted). Deleted on request by a person (`metrics.contacts.delete`): the row
    goes, the courses that named it keep "Kontakt gelöscht" in their Verlauf.
    """

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    place_id: Mapped[int] = mapped_column(ForeignKey("places.id"), index=True)
    name: Mapped[str] = mapped_column(Text)
    role: Mapped[str | None] = mapped_column(Text)  # e.g. Klassenleitung, Sekretariat
    email: Mapped[str | None] = mapped_column(Text)
    phone: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[dt.datetime] = _created_at()

    place: Mapped[Place] = relationship()


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
    # Kontaktperson (WM-141): planning only, never counted or exported.
    contact_id: Mapped[int | None] = mapped_column(ForeignKey("contacts.id", ondelete="SET NULL"), index=True)
    # Workload estimate (WM-131): optional values of the rule for this course's Termine; empty
    # means inherited (Format or place, then `[workload]`), see `metrics.planning.workload`.
    lead_factor: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    preparation_factor: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    lead_overhead_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    support_factor: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    support_overhead_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    travel_hours: Mapped[Decimal | None] = mapped_column(Numeric(6, 2))
    created_at: Mapped[dt.datetime] = _created_at()

    format: Mapped[Format | None] = relationship()
    context: Mapped[Context | None] = relationship()
    place: Mapped[Place | None] = relationship()
    contact: Mapped[Contact | None] = relationship()
    appointments: Mapped[list["Appointment"]] = relationship(back_populates="course")


appointment_support = Table(
    "appointment_support",
    Base.metadata,
    Column("appointment_id", ForeignKey("appointments.id", ondelete="CASCADE"), primary_key=True),
    Column("person_id", ForeignKey("people.id"), primary_key=True, index=True),
)


def _course_value(name: str) -> hybrid_property:
    """A Termin's effective value of a course field: its frozen value once `frozen_at` is set (even
    when that is empty), else the course's. In SQL it needs the course joined
    (`Appointment.course_id == Course.id`), as every query over Termine with their course has."""
    frozen = f"frozen_{name}"

    def value(self):
        return getattr(self, frozen) if self.frozen_at is not None else getattr(self.course, name)

    def expression(cls):
        return case((cls.frozen_at.is_not(None), getattr(cls, frozen)), else_=getattr(Course, name))

    return hybrid_property(value, expr=expression)


class Appointment(Base):
    """Termin: one date of a course, the counting unit (an Einsatz is a held Termin).

    `number` is the Terminnummer; legacy rows keep the recorded one. `participants_total` is the
    recorded total (legacy before 2024-07-29 has nothing else); new Termine record the four
    counts and keep the total as their sum. `source` and `source_key` say where an imported
    row came from (e.g. `baseline`, `baseline:123`), `source_data` keeps its recorded values.

    Frozen course values (WM-135): the course's format, context, place and class/age apply to all its
    Termine, until the course changes while some of them are Abgeschlossen or Abgesagt. Then those
    Termine first keep the values they had (`frozen_*`, `frozen_at`; `courses.keep_values`), so the
    figures of the past do not move. Read a Termin's values through the effective attributes
    (`t.place`, `t.place_id`, `t.target_group`, ...; in SQL `Appointment.place_id` and so on), never
    through `t.course` directly: they give the frozen value if there is one, else the course's.
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
    cancel_reason: Mapped[str | None] = mapped_column(Text)
    # The event in the course calendar (WM-110): its UID, and what the last check found.
    calendar_uid: Mapped[str | None] = mapped_column(String(255))
    calendar_state: Mapped[str | None] = mapped_column(String(16))  # ok, missing, outdated
    calendar_checked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String(32), default="app", server_default="app", index=True)
    source_key: Mapped[str | None] = mapped_column(String(128), unique=True)
    source_data: Mapped[dict | None] = mapped_column(JSONB)
    # Frozen course values (WM-135): set together, only when the course changes after this Termin was
    # Abgeschlossen or Abgesagt; NULL in `frozen_at` means the Termin follows its course.
    frozen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    frozen_format_id: Mapped[int | None] = mapped_column(ForeignKey("formats.id"), index=True)
    frozen_context_id: Mapped[int | None] = mapped_column(ForeignKey("contexts.id"), index=True)
    frozen_place_id: Mapped[int | None] = mapped_column(ForeignKey("places.id"), index=True)
    frozen_class_age_text: Mapped[str | None] = mapped_column(Text)
    frozen_class_level: Mapped[int | None] = mapped_column(Integer)
    frozen_age: Mapped[int | None] = mapped_column(Integer)
    frozen_age_band: Mapped[str | None] = mapped_column(String(32))
    frozen_target_group: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    course: Mapped[Course] = relationship(back_populates="appointments")
    frozen_format: Mapped[Format | None] = relationship(foreign_keys=[frozen_format_id])
    frozen_context: Mapped[Context | None] = relationship(foreign_keys=[frozen_context_id])
    frozen_place: Mapped[Place | None] = relationship(foreign_keys=[frozen_place_id])
    lead: Mapped[Person | None] = relationship(foreign_keys=[lead_id])
    support: Mapped[list[Person]] = relationship(secondary=appointment_support)
    origins: Mapped[list["AppointmentOrigin"]] = relationship(cascade="all, delete-orphan")
    alternatives: Mapped[list["AlternativeDate"]] = relationship(
        cascade="all, delete-orphan", order_by="AlternativeDate.date"
    )
    calendar_responses: Mapped[list["CalendarResponse"]] = relationship(
        cascade="all, delete-orphan", order_by="CalendarResponse.email"
    )
    equipment: Mapped[list["AppointmentEquipment"]] = relationship(
        cascade="all, delete-orphan", order_by="AppointmentEquipment.equipment_id"
    )

    # The effective course values (see the class docstring).
    format_id = _course_value("format_id")
    context_id = _course_value("context_id")
    place_id = _course_value("place_id")
    class_age_text = _course_value("class_age_text")
    class_level = _course_value("class_level")
    age = _course_value("age")
    age_band = _course_value("age_band")
    target_group = _course_value("target_group")

    @property
    def has_frozen_values(self) -> bool:
        return self.frozen_at is not None

    @property
    def format(self) -> Format | None:
        return self.frozen_format if self.has_frozen_values else self.course.format

    @property
    def context(self) -> Context | None:
        return self.frozen_context if self.has_frozen_values else self.course.context

    @property
    def place(self) -> Place | None:
        return self.frozen_place if self.has_frozen_values else self.course.place


class AppointmentEquipment(Base):
    """How much of a piece of equipment a Termin needs (copied from its format, changeable).

    A kit blocks the whole day (decided 2026-11-13): the hint "material-knapp" adds up the needs
    of the Termine of a day, whatever their times.
    """

    __tablename__ = "appointment_equipment"
    __table_args__ = (CheckConstraint("quantity > 0", name="ck_appointment_equipment_quantity"),)

    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), primary_key=True
    )
    equipment_id: Mapped[int] = mapped_column(ForeignKey("equipment.id"), primary_key=True, index=True)
    quantity: Mapped[int] = mapped_column(Integer)

    equipment: Mapped[Equipment] = relationship()


class AlternativeDate(Base):
    """A further possible date of a Geplant Termin; "Bestätigen" asks which date it is."""

    __tablename__ = "appointment_alternatives"

    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), primary_key=True
    )
    date: Mapped[dt.date] = mapped_column(Date, primary_key=True)


# Calendar states of a Termin's event (`Appointment.calendar_state`).
CAL_OK, CAL_MISSING, CAL_OUTDATED = "ok", "missing", "outdated"


class CalendarResponse(Base):
    """The answer of an invited person (ATTENDEE PARTSTAT), as the last calendar check read it."""

    __tablename__ = "calendar_responses"

    appointment_id: Mapped[int] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), primary_key=True
    )
    email: Mapped[str] = mapped_column(Text, primary_key=True)
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id"), index=True)
    partstat: Mapped[str] = mapped_column(String(32))  # NEEDS-ACTION, ACCEPTED, DECLINED, TENTATIVE
    seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    person: Mapped[Person | None] = relationship()


class CalendarOrphan(Base):
    """A future event in the course calendar without a Termin, as the last calendar check saw it.

    Bookkeeping of the check: rows of events that are gone are removed by the next check, unless an
    admin ignored them (that decision is kept). Times are local (Europe/Berlin), without zone.
    """

    __tablename__ = "calendar_orphans"

    uid: Mapped[str] = mapped_column(String(255), primary_key=True)
    summary: Mapped[str] = mapped_column(Text)
    starts_at: Mapped[dt.datetime] = mapped_column(DateTime)
    ends_at: Mapped[dt.datetime] = mapped_column(DateTime)
    location: Mapped[str | None] = mapped_column(Text)
    seen_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ignored_by: Mapped[str | None] = mapped_column(Text)
    ignored_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


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
    # The finding it accepts (`hints.finding_key`); NULL: the whole rule on this Termin (older rows).
    finding_key: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text)
    decided_by: Mapped[str] = mapped_column(Text)
    decided_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class StoredHint(Base):
    """Hinweis: one finding of a rule on a Termin, as last computed by `metrics.hints`.

    Derived data: `metrics.hints.refresh` replaces the rows of the Termine it recomputes, so these
    rows are a cache of the rules, not a record. Decisions live in `hint_decisions`.
    """

    __tablename__ = "hints"
    __table_args__ = (Index("ix_hints_rule_level", "rule", "level"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    appointment_id: Mapped[int] = mapped_column(ForeignKey("appointments.id", ondelete="CASCADE"), index=True)
    rule: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(16))  # Kritisch, Wichtig, Info
    blocking: Mapped[bool] = mapped_column(Boolean)
    text: Mapped[str] = mapped_column(Text)
    # The other Termin of a conflict or a likely duplicate.
    other_appointment_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="CASCADE"), index=True
    )
    # Which finding this is (`hints.finding_key`): what an accepted decision is tied to.
    key: Mapped[str | None] = mapped_column(Text)
    computed_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class JobRun(Base):
    """One run of a scheduler job (WM-103)."""

    __tablename__ = "job_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(64), index=True)
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool | None] = mapped_column(Boolean)
    message: Mapped[str | None] = mapped_column(Text)


class SlackMessage(Base):
    """One Slack message of the bot (WM-130), recorded before it is sent so it goes out at most once.

    `key` names the occasion (e.g. the reminder for one Termin, date and person); a second row with
    the same key cannot exist. `status`: `sending` while the request runs, `sent`, `failed` (Slack
    answered with an error, so nothing was posted: tried again on a later run), `unclear` (the request
    broke off, it may have arrived: never tried again), `undeliverable` (a permanent error of the
    recipient, e.g. a stale Slack ID: never tried again). The text itself is not kept.
    """

    __tablename__ = "slack_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    key: Mapped[str] = mapped_column(String(128), unique=True)
    appointment_id: Mapped[int | None] = mapped_column(
        ForeignKey("appointments.id", ondelete="SET NULL"), index=True
    )
    person_id: Mapped[int | None] = mapped_column(ForeignKey("people.id", ondelete="SET NULL"))
    channel: Mapped[str] = mapped_column(String(32))  # a Slack user ID (DM) or channel ID
    status: Mapped[str] = mapped_column(String(16))
    attempts: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    error: Mapped[str | None] = mapped_column(Text)  # Slack's error code or the exception type
    ts: Mapped[str | None] = mapped_column(String(32))  # Slack's message timestamp
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class ImportRun(Base):
    """One legacy or baseline load, shown as the data timestamp on the page."""

    __tablename__ = "import_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    rows: Mapped[int | None] = mapped_column(Integer)
    message: Mapped[str | None] = mapped_column(Text)


# The web app runs its routes in at most WEB_THREADS threads (set in `metrics.web.app`), plus the
# event loop's own. A request may hold two connections at once (its session and the sidebar's in
# `web.common.page`), so the pool has room for one per thread and one more: some request can always
# finish. With fewer, many parallel requests each held one connection and waited for a second until
# the pool timeout (seen in the ZAP active scan, WM-153).
WEB_THREADS = 20
POOL_SIZE = 5
POOL_OVERFLOW = WEB_THREADS + 2 - POOL_SIZE


@lru_cache
def get_sessionmaker() -> sessionmaker[Session]:
    # The timeout keeps /healthz and the pages from hanging when the database does not answer.
    engine = create_engine(
        get_settings().database_url,
        pool_pre_ping=True,
        pool_size=POOL_SIZE,
        max_overflow=POOL_OVERFLOW,
        connect_args={"connect_timeout": 10},
    )
    return sessionmaker(engine, expire_on_commit=False)
