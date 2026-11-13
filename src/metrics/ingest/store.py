"""Writing reference data and imported records into the tables.

Every record becomes a course with one Termin. Names become confirmed list entries exactly as
recorded (nothing is renamed or merged); a legacy first name that the instance maps to a Slack
user (`seeds/instructors.csv`, a recorded decision) is that user.
"""

from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from metrics.db import (
    ABGESAGT,
    ABGESCHLOSSEN,
    GEPLANT,
    STATUS_DONE,
    Appointment,
    Context,
    Course,
    Format,
    ImportRun,
    Person,
    Place,
    SchoolType,
)
from metrics.ingest.baseline import SOURCE_BASELINE
from metrics.ingest.instructors import SLACK_USER_ID
from metrics.ingest.legacy import LEGACY_SOURCES
from metrics.ingest.records import SessionRecord
from metrics.instance import Instance


def _log(session: Session, kind: str, rows: int, message: str) -> None:
    session.add(ImportRun(kind=kind, finished_at=datetime.now(UTC), rows=rows, message=message))


def stage_for_status(status: str | None) -> str:
    """Stage of an imported row: held rows are final (Abgeschlossen)."""
    if status == STATUS_DONE:
        return ABGESCHLOSSEN
    if status == ABGESAGT:
        return ABGESAGT
    return GEPLANT


def seed_reference(session: Session, instance: Instance) -> None:
    """Add the instance's reference data as confirmed list entries; existing entries stay as they are.

    seeds/locations.csv and school_types.csv -> places and school types; `[[projects]]` and
    `teacher_contexts` -> contexts; seeds/instructors.csv -> people (Slack users).
    """
    codes = set(session.scalars(select(SchoolType.code)))
    position = len(codes)
    for code, label in instance.school_types.items():
        if code not in codes:
            session.add(SchoolType(code=code, label=label, position=position))
            position += 1

    places = set(session.scalars(select(Place.name)))
    for name, is_school in instance.locations.items():
        if name in places:
            continue
        lat, lon = instance.location_coordinates.get(name, (None, None))
        session.add(
            Place(
                name=name,
                is_school=is_school,
                school_type=instance.school_type(name),
                address=instance.location_addresses.get(name),
                municipality=instance.municipality(name),
                lat=lat,
                lon=lon,
            )
        )

    contexts: dict[str, dict] = {}
    for name in instance.teacher_contexts:
        contexts.setdefault(name, {})["teacher_training"] = True
    for project in instance.projects:
        for name in project.contexts:
            contexts.setdefault(name, {}).update(
                is_project=True,
                page_slug=project.slug,
                page_title=project.title,
                asks_municipalities=project.participant_municipalities,
            )
    existing = set(session.scalars(select(Context.name)))
    for name, values in contexts.items():
        if name not in existing:
            session.add(Context(name=name, **values))

    slack_ids = set(session.scalars(select(Person.slack_id).where(Person.slack_id.is_not(None))))
    for slack_id in sorted(set(instance.instructor_aliases.values()) - slack_ids):
        session.add(Person(name=slack_id, slack_id=slack_id, active=True))
    session.flush()


class _Lists:
    """Looks up list entries by recorded name and adds missing ones."""

    def __init__(self, session: Session, instance: Instance):
        self.session = session
        self.instance = instance
        self.places = {p.name: p for p in session.scalars(select(Place))}
        self.contexts = {c.name: c for c in session.scalars(select(Context))}
        self.formats = {(f.name, f.category): f for f in session.scalars(select(Format))}
        self.people = {p.key: p for p in session.scalars(select(Person))}

    def _get(self, cache: dict, key, make):
        if key not in cache:
            cache[key] = make()
            self.session.add(cache[key])
        return cache[key]

    def place(self, name: str | None) -> Place | None:
        return self._get(self.places, name, lambda: Place(name=name)) if name else None

    def context(self, name: str | None) -> Context | None:
        return self._get(self.contexts, name, lambda: Context(name=name)) if name else None

    def format(self, name: str | None, category: str | None) -> Format:
        return self._get(self.formats, (name, category), lambda: Format(name=name, category=category))

    def person(self, recorded: str | None) -> Person | None:
        key = self.instance.instructor_key(recorded)
        if not key:
            return None
        slack = bool(SLACK_USER_ID.match(key))
        return self._get(
            self.people, key, lambda: Person(name=key, slack_id=key if slack else None, active=slack)
        )


def write_records(session: Session, records: list[SessionRecord], instance: Instance) -> None:
    """One course with one Termin per record."""
    lists = _Lists(session, instance)
    for record in records:
        course = Course(
            format=lists.format(record.course_name, record.category),
            context=lists.context(record.context),
            place=lists.place(record.location),
            class_age_text=record.class_age_text,
            class_level=record.class_level,
            age=record.age,
            age_band=record.age_band,
            target_group=record.target_group,
        )
        session.add(
            Appointment(
                course=course,
                number=record.session_number,
                date=record.date,
                duration_hours=record.duration_hours,
                stage=stage_for_status(record.status),
                lead=lists.person(record.instructor),
                participants_female=record.participants_female,
                participants_male=record.participants_male,
                participants_total=record.participants_total,
                source=record.source,
                source_key=record.source_key,
                # Recorded values without a column of their own.
                source_data={
                    "status": record.status,
                    "instructor": record.instructor,
                    "school_type": record.school_type,
                },
            )
        )
    session.flush()


class BaselineFrozen(Exception):
    """A load would change data covered by the frozen baseline."""


def baseline_end(session: Session):
    """Last date of the frozen baseline, or None while no baseline is loaded."""
    return session.execute(
        select(func.max(Appointment.date)).where(Appointment.source == SOURCE_BASELINE)
    ).scalar()


def _delete_legacy(session: Session) -> None:
    """Remove the Termine of earlier legacy loads (and their courses) before loading the files again."""
    course_ids = list(
        session.scalars(select(Appointment.course_id).where(Appointment.source.in_(LEGACY_SOURCES)))
    )
    session.execute(delete(Appointment).where(Appointment.source.in_(LEGACY_SOURCES)))
    if course_ids:
        session.execute(delete(Course).where(Course.id.in_(course_ids), ~Course.appointments.any()))


def replace_legacy(session: Session, records: list[SessionRecord], instance: Instance) -> None:
    """Replace all legacy rows (not the baseline).

    Once a baseline is loaded, only entries dated after its last date may be loaded this way
    (quarterly hand-overs); anything older belongs to the frozen baseline and is refused.
    """
    end = baseline_end(session)
    if end is not None:
        older = sorted({r.date for r in records if r.date is None or r.date <= end}, key=str)
        if older:
            raise BaselineFrozen(
                f"The baseline is frozen up to {end}; {len(older)} date(s) in these files are not after it "
                f"(first: {older[0]}). Load only newer exports."
            )
    _delete_legacy(session)
    seed_reference(session, instance)
    write_records(session, records, instance)
    _log(session, "legacy-load", len(records), f"{len(records)} legacy rows loaded")


def load_baseline(
    session: Session, records: list[SessionRecord], instance: Instance, version: str, sha256: str
) -> None:
    """Load the baseline once; it replaces the legacy rows it was made from and is never replaced.

    The legacy rows hold the same entries (the baseline was exported from them), so nothing is
    lost; a second baseline load is refused.
    """
    if baseline_end(session) is not None:
        raise BaselineFrozen("A baseline is already loaded and frozen; it cannot be loaded again.")
    _delete_legacy(session)
    seed_reference(session, instance)
    write_records(session, records, instance)
    _log(
        session,
        "baseline-load",
        len(records),
        f"baseline {version} loaded and frozen: {len(records)} rows, sha256 {sha256}",
    )


def summary(session: Session, sources: tuple[str, ...]) -> dict[str, Counter]:
    """Termine per year and stage, per target group, for the CLI report."""
    rows = session.execute(
        select(Appointment.date, Appointment.stage, Course.target_group)
        .join(Course, Appointment.course_id == Course.id)
        .where(Appointment.source.in_(sources))
    ).all()
    return {
        "per_year_and_status": Counter((d.year if d else None, stage) for d, stage, _ in rows),
        "per_target_group": Counter(group for _, _, group in rows),
    }
