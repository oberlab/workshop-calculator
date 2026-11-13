"""Reference lists (Stammdaten): places, contexts, formats and people (WM-101, PROPOSAL.md section 3).

Any team member may propose a new entry; it is usable at once (status `vorgeschlagen`, with a
Vorschlag for the admin queue and a Verlauf row). An admin confirms it, merges it into another
entry (every course and Termin follows; the merged row stays with `merged_into_id`) or rejects it
with a reason. Every change is written to the Verlauf with who, when and why; nothing is deleted.

Rules for editing: a team member edits proposed entries; confirmed entries only an admin (a team
member's change to a confirmed entry is a correction, WM-132). A rejected entry still in use stays
on its courses, is no longer offered, and keeps its Termine from the final confirmation until a
person chooses another entry or an admin merges it into the right one (`pending_entries`).

The definitions that used to live in the instance config are read from here too: project pages
(`projects`, contexts with a `page_slug`) and the teacher contexts of the target-group rule.
"""

import datetime as dt
import re
from collections import defaultdict
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from metrics.db import (
    ACTIVE_STATUSES,
    CONFIRMED,
    MERGED,
    PROPOSED,
    REJECTED,
    Appointment,
    Context,
    Course,
    Format,
    HistoryEntry,
    Person,
    Place,
    Proposal,
    SchoolType,
    appointment_support,
)
from metrics.geo import municipality_at
from metrics.ingest.records import TEACHERS, ClassAge, target_group
from metrics.instance import Instance, MapConfig, Project, clean_name

LOGIN = "Anmeldung"  # changed_by of the automatic login link


@dataclass(frozen=True)
class Kind:
    """One list: its address part, model, labels and which course column points at it."""

    slug: str
    model: type
    label: str  # plural, e.g. "Orte"
    singular: str  # e.g. "Ort"
    intro: str


PLACES = Kind(
    "orte",
    Place,
    "Orte",
    "Ort",
    "Wo Kurse stattfinden. Adresse und die Angabe, ob der Ort eine Schule ist, bestimmen Zielgruppe, "
    "Schulart und Gemeinde in den Kennzahlen.",
)
CONTEXTS = Kind(
    "kontexte",
    Context,
    "Kontexte",
    "Kontext",
    "Projekt oder Rahmen eines Kurses. Kontexte mit Projektseite erscheinen unter Auswertung; "
    "Kurse in Fortbildungs-Kontexten zählen als Lehrkräfte.",
)
FORMATS = Kind(
    "formate",
    Format,
    "Formate",
    "Format",
    "Was wir anbieten: Kursname, Kategorie und übliche Dauer.",
)
PEOPLE = Kind(
    "personen",
    Person,
    "Personen",
    "Person",
    "Das Team. Die Anmeldung wird beim ersten Login über die E-Mail-Adresse mit der Person verknüpft.",
)
KINDS = {k.slug: k for k in (PLACES, CONTEXTS, FORMATS, PEOPLE)}
KIND_BY_MODEL = {k.model: k for k in KINDS.values()}

STATUS_LABELS = {
    PROPOSED: "Vorgeschlagen",
    CONFIRMED: "Bestätigt",
    REJECTED: "Abgelehnt",
    MERGED: "Zusammengeführt",
}
# Proposal.decision values.
DECIDED = {CONFIRMED: "bestätigt", MERGED: "zusammengeführt", REJECTED: "abgelehnt"}


class ListError(ValueError):
    """A request the rules do not allow; the message is shown to the person (German)."""


# --- Reading ---------------------------------------------------------------------------------


def label(entry) -> str:
    """How an entry is named everywhere: a format with its category."""
    if isinstance(entry, Format):
        name = entry.name or "(ohne Kursname)"
        return f"{name} ({entry.category})" if entry.category else name
    return entry.name


def _sort_key(entry) -> str:
    return label(entry).casefold()


def get(session: Session, kind: Kind, entry_id: int):
    return session.get(kind.model, entry_id)


def resolve(session: Session, entry):
    """The entry that stands for `entry`: itself, or the entry it was merged into (followed through)."""
    seen = set()
    while entry is not None and entry.merged_into_id is not None and entry.id not in seen:
        seen.add(entry.id)
        entry = session.get(type(entry), entry.merged_into_id)
    return entry


def search(session: Session, kind: Kind, query: str | None = None, status: str | None = None) -> list:
    """Entries matching the search text (name, and address, municipality, category, e-mail) and status.

    status: one of the list statuses, "aktiv" (proposed or confirmed) or None for all.
    """
    model = kind.model
    stmt = select(model)
    if status == "aktiv":
        stmt = stmt.where(model.status.in_(ACTIVE_STATUSES))
    elif status:
        stmt = stmt.where(model.status == status)
    if query := clean_name(query):
        pattern = f"%{query}%"
        columns = {
            PLACES: (Place.name, Place.address, Place.municipality),
            CONTEXTS: (Context.name, Context.page_title),
            FORMATS: (Format.name, Format.category),
            PEOPLE: (Person.name, Person.email, Person.slack_id),
        }[kind]
        stmt = stmt.where(or_(*(c.ilike(pattern) for c in columns)))
    return sorted(session.scalars(stmt), key=_sort_key)


def choices(session: Session, kind: Kind, selected_id: int | None = None) -> list:
    """What a picker offers: the active entries (people: only active team members), plus the
    selected entry whatever its status, so a form never drops a value silently."""
    model = kind.model
    stmt = select(model).where(model.status.in_(ACTIVE_STATUSES))
    if kind is PEOPLE:
        stmt = stmt.where(Person.active.is_(True))
    entries = {e.id: e for e in session.scalars(stmt)}
    if selected_id is not None and selected_id not in entries:
        if (selected := session.get(model, selected_id)) is not None:
            entries[selected.id] = selected
    return sorted(entries.values(), key=_sort_key)


def school_types(session: Session) -> dict[str, str]:
    return dict(
        session.execute(select(SchoolType.code, SchoolType.label).order_by(SchoolType.position)).all()
    )


def categories(session: Session) -> list[str]:
    found = session.scalars(select(Format.category).where(Format.category.is_not(None)).distinct())
    return sorted(found, key=str.casefold)


def describe(entry, types: dict[str, str]) -> str:
    """One short line about an entry for pickers and lists."""
    parts: list[str] = []
    if isinstance(entry, Place):
        if entry.is_school:
            parts.append(types.get(entry.school_type, entry.school_type) if entry.school_type else "Schule")
        elif entry.is_school is False:
            parts.append("keine Schule")
        if entry.municipality:
            parts.append(entry.municipality)
        elif entry.address:
            parts.append(entry.address)
    elif isinstance(entry, Context):
        if entry.page_slug:
            parts.append(f"Projektseite {entry.page_title or entry.name}")
        if entry.teacher_training:
            parts.append("zählt als Lehrkräfte")
    elif isinstance(entry, Format):
        if entry.duration_hours is not None:
            parts.append(f"Dauer {_number(entry.duration_hours)} h")
    elif isinstance(entry, Person):
        if not entry.active:
            parts.append("inaktiv")
    if entry.status == PROPOSED:
        parts.append("wartet auf Bestätigung")
    elif entry.status == REJECTED:
        parts.append("abgelehnt, bitte ersetzen")
    return " · ".join(parts)


def _number(value: Decimal | float | None) -> str:
    if value is None:
        return ""
    text = f"{Decimal(str(value)).normalize():f}"
    return text.replace(".", ",")


@dataclass(frozen=True)
class Usage:
    courses: int = 0
    appointments: int = 0
    final: int = 0  # Abgeschlossen, i.e. in the statistics


def usage(session: Session, kind: Kind, ids: list[int] | None = None) -> dict[int, Usage]:
    """How many courses and Termine use each entry (people: Termine as lead or support)."""
    final = func.count().filter(Appointment.stage == "Abgeschlossen")
    if kind is PEOPLE:
        lead = select(
            Appointment.lead_id.label("pid"), Appointment.id, Appointment.course_id, Appointment.stage
        )
        support = select(
            appointment_support.c.person_id.label("pid"),
            Appointment.id,
            Appointment.course_id,
            Appointment.stage,
        ).join(Appointment, Appointment.id == appointment_support.c.appointment_id)
        both = lead.union(support).subquery()
        stmt = select(
            both.c.pid,
            func.count(func.distinct(both.c.course_id)),
            func.count(func.distinct(both.c.id)),
            func.count(func.distinct(both.c.id)).filter(both.c.stage == "Abgeschlossen"),
        ).group_by(both.c.pid)
        if ids is not None:
            stmt = stmt.where(both.c.pid.in_(ids))
    else:
        column = {PLACES: Course.place_id, CONTEXTS: Course.context_id, FORMATS: Course.format_id}[kind]
        stmt = (
            select(column, func.count(func.distinct(Course.id)), func.count(Appointment.id), final)
            .select_from(Course)
            .outerjoin(Appointment, Appointment.course_id == Course.id)
            .where(column.is_not(None))
            .group_by(column)
        )
        if ids is not None:
            stmt = stmt.where(column.in_(ids))
    return {row[0]: Usage(row[1], row[2], row[3]) for row in session.execute(stmt) if row[0] is not None}


def place_problems(place: Place, types: dict[str, str], map_config: MapConfig | None) -> list[str]:
    """What a place still lacks for the statistics (formerly `metrics check-locations`)."""
    problems = []
    if place.is_school is None:
        problems.append("Schule? offen")
    elif place.is_school and not place.school_type:
        problems.append("Schulart fehlt")
    if place.school_type and place.school_type not in types:
        problems.append(f"Schulart {place.school_type} unbekannt")
    if place.address is None:
        problems.append("Adresse fehlt")
    elif place.address != "" and not place.municipality:
        problems.append("Gemeinde fehlt")
    if map_config and place.lat is not None and place.lon is not None:
        found = municipality_at(map_config.municipalities, place.lon, place.lat)
        names = {m.name for m in map_config.municipalities}
        if found and place.municipality != found:
            problems.append(f"Gemeinde passt nicht zu den Koordinaten ({found})")
        elif not found and place.municipality in names:
            problems.append("Koordinaten außerhalb der Karte")
    return problems


def history(session: Session, kind: Kind, entry_id: int) -> list[HistoryEntry]:
    return list(
        session.scalars(
            select(HistoryEntry)
            .where(HistoryEntry.table_name == kind.model.__tablename__, HistoryEntry.row_id == entry_id)
            .order_by(HistoryEntry.changed_at.desc(), HistoryEntry.id.desc())
        )
    )


def open_proposal(session: Session, entry) -> Proposal | None:
    return session.scalars(
        select(Proposal)
        .where(
            Proposal.kind == "eintrag",
            Proposal.target_table == entry.__tablename__,
            Proposal.target_id == entry.id,
        )
        .order_by(Proposal.id.desc())
    ).first()


def proposed_counts(session: Session) -> dict[str, int]:
    """Proposed entries per list, for the sidebar (keys `list_<slug>`)."""
    counts = {}
    for kind in KINDS.values():
        n = session.scalar(select(func.count()).select_from(kind.model).where(kind.model.status == PROPOSED))
        counts[f"list_{kind.slug}"] = n or 0
    return counts


# --- Definitions read from the contexts (formerly the instance config) ------------------------


def projects(session: Session) -> list[Project]:
    """Project pages: active contexts with a page address, grouped by it, in the order they were added."""
    pages: dict[str, list[Context]] = {}
    stmt = (
        select(Context)
        .where(Context.page_slug.is_not(None), Context.status.in_(ACTIVE_STATUSES))
        .order_by(Context.id)
    )
    for context in session.scalars(stmt):
        pages.setdefault(context.page_slug, []).append(context)
    return [
        Project(
            slug,
            next((c.page_title for c in contexts if c.page_title), contexts[0].name),
            tuple(c.name for c in contexts),
            any(c.asks_municipalities for c in contexts),
        )
        for slug, contexts in pages.items()
    ]


def project(session: Session, slug: str) -> Project | None:
    return next((p for p in projects(session) if p.slug == slug), None)


def teacher_contexts(session: Session) -> frozenset[str]:
    return frozenset(session.scalars(select(Context.name).where(Context.teacher_training.is_(True))))


def with_contexts(session: Session, instance: Instance) -> Instance:
    """The instance with projects and teacher contexts as defined in the contexts table."""
    return replace(instance, projects=projects(session), teacher_contexts=teacher_contexts(session))


def course_target_group(course: Course) -> str:
    """The target-group rule for a course on the new model: its stored class/age, its context's
    teacher-training flag and its place's school flag (the same rule the imports used)."""
    class_age = ClassAge(
        class_level=course.class_level,
        age=course.age,
        age_band=course.age_band,
        teachers=clean_name(course.class_age_text) == TEACHERS,
    )
    context = course.context
    return target_group(
        context=context.name if context else None,
        class_age=class_age,
        location_is_school=course.place.is_school if course.place else None,
        teacher_contexts=frozenset({context.name}) if context and context.teacher_training else frozenset(),
    )


def _recompute_target_groups(session: Session, courses, by: str, reason: str) -> int:
    changed = 0
    for course in courses:
        new = course_target_group(course)
        if new != course.target_group:
            _log(session, "courses", course.id, "target_group", course.target_group, new, by, reason)
            course.target_group = new
            changed += 1
    return changed


# --- Final confirmation of a Termin waits for its list entries -------------------------------


@dataclass(frozen=True)
class Pending:
    """A list entry of a Termin that is not confirmed yet."""

    role: str  # Ort, Kontext, Format, Kursleitung, Unterstützung
    kind: Kind
    entry: object

    @property
    def text(self) -> str:
        state = {
            PROPOSED: "wartet auf Bestätigung",
            REJECTED: "wurde abgelehnt und muss ersetzt werden",
            MERGED: "wurde zusammengeführt und muss ersetzt werden",
        }.get(self.entry.status, self.entry.status)
        return f"{self.role} „{label(self.entry)}“ {state}"


def pending_entries(appointment: Appointment) -> list[Pending]:
    """The list entries of a Termin (its course's place, context and format, its lead and support)
    that are not confirmed. Final confirmation waits until this is empty."""
    course = appointment.course
    used = [
        ("Ort", PLACES, course.place),
        ("Kontext", CONTEXTS, course.context),
        ("Format", FORMATS, course.format),
        ("Kursleitung", PEOPLE, appointment.lead),
        *(("Unterstützung", PEOPLE, person) for person in appointment.support),
    ]
    return [Pending(role, kind, e) for role, kind, e in used if e is not None and e.status != CONFIRMED]


def entries_confirmed(appointment: Appointment) -> bool:
    return not pending_entries(appointment)


# --- Values from forms -----------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    name: str
    label: str
    type: str = "text"  # text, yesno, school_type, bool, number, email, category
    required: bool = False  # for a new proposal
    help: str | None = None


FIELDS: dict[Kind, tuple[Field, ...]] = {
    PLACES: (
        Field("name", "Name", required=True),
        Field("is_school", "Schule?", "yesno", required=True),
        Field("school_type", "Schulart", "school_type", help="Pflicht bei Schulen"),
        Field("address", "Adresse", required=True, help="Straße Hausnummer, PLZ Ort"),
        Field("no_address", "Bewusst ohne Adresse (z. B. Online)", "bool"),
        Field("municipality", "Gemeinde"),
        Field("lat", "Breitengrad", "number"),
        Field("lon", "Längengrad", "number"),
    ),
    CONTEXTS: (
        Field("name", "Name", required=True),
        Field("is_project", "Projekt", "bool"),
        Field("page_slug", "Adresse der Projektseite", help="z. B. jugendlab; leer = keine eigene Seite"),
        Field("page_title", "Titel der Projektseite"),
        Field("asks_municipalities", "Fragt nach den Heimatgemeinden", "bool"),
        Field("teacher_training", "Fortbildung: zählt als Lehrkräfte", "bool"),
    ),
    FORMATS: (
        Field("name", "Kursname", required=True),
        Field("category", "Kategorie", "category", required=True),
        Field("duration_hours", "Übliche Dauer (h)", "number", required=True),
        Field("overhead_hours", "Vor- und Nachbereitung (h)", "number"),
    ),
    PEOPLE: (
        Field("name", "Name", required=True),
        Field("email", "E-Mail", "email", help="Verknüpft die Anmeldung beim ersten Login"),
        Field("slack_id", "Slack-ID"),
        Field("kimai_user", "Kimai-Nutzer"),
        Field("active", "Aktiv im Team", "bool"),
        Field("weekly_hours", "Wochenstunden", "number"),
    ),
}

# Fields a new proposal asks for (the rest is filled in on the list page).
PROPOSAL_FIELDS: dict[Kind, tuple[str, ...]] = {
    PLACES: ("name", "is_school", "school_type", "address", "municipality"),
    CONTEXTS: ("name",),
    FORMATS: ("name", "category", "duration_hours"),
    PEOPLE: ("name", "email"),
}

_SLUG = re.compile(r"[a-z0-9][a-z0-9-]*")
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def form_values(kind: Kind, entry) -> dict[str, str]:
    """An entry's values as form strings."""
    values = {}
    for f in FIELDS[kind]:
        if f.name == "no_address":
            values[f.name] = "1" if entry.address == "" else ""
            continue
        value = getattr(entry, f.name)
        if f.type == "yesno":
            values[f.name] = {True: "ja", False: "nein"}.get(value, "")
        elif f.type == "bool":
            values[f.name] = "1" if value else ""
        elif f.type == "number":
            values[f.name] = _number(value)
        else:
            values[f.name] = value or ""
    return values


def _decimal(text: str) -> Decimal:
    try:
        value = Decimal(text.replace(",", "."))
    except InvalidOperation as error:
        raise ValueError from error
    if not value.is_finite():
        raise ValueError
    return value


def parse(
    kind: Kind, form: dict[str, str], types: dict[str, str], new: bool
) -> tuple[dict[str, object], dict[str, str]]:
    """Model values from form strings, and errors per field (German). `new`: a proposal, which must
    have every required detail; an edit may leave optional details empty."""
    names = PROPOSAL_FIELDS[kind] if new else tuple(f.name for f in FIELDS[kind])
    fields = [f for f in FIELDS[kind] if f.name in names]
    values: dict[str, object] = {}
    errors: dict[str, str] = {}
    for f in fields:
        raw = clean_name(form.get(f.name))
        if f.type == "yesno":
            values[f.name] = {"ja": True, "nein": False}.get(raw)
        elif f.type == "bool":
            values[f.name] = raw in ("1", "on", "ja", "true")
        elif f.type == "number":
            if not raw:
                values[f.name] = None
            else:
                try:
                    number = _decimal(raw)
                except ValueError:
                    errors[f.name] = "Bitte eine Zahl eingeben."
                    continue
                if f.name in ("lat", "lon"):
                    values[f.name] = float(number)
                elif number < 0 or number > 1000:
                    errors[f.name] = "Bitte eine Zahl zwischen 0 und 1000 eingeben."
                else:
                    values[f.name] = number
        else:
            values[f.name] = raw or None
        if f.required and new and values[f.name] in (None, ""):
            errors.setdefault(f.name, "Pflichtfeld")
    if not new:
        # Legacy formats may lack a course name (the unnamed format of a category), never both.
        needed = ("name", "category") if kind is FORMATS else ("name",)
        if all(not values.get(n) for n in needed):
            errors.setdefault("name", "Pflichtfeld")

    if kind is PLACES:
        if values.get("is_school") and not values.get("school_type"):
            errors.setdefault("school_type", "Bei einer Schule bitte die Schulart wählen.")
        if values.get("school_type") and values["school_type"] not in types:
            errors.setdefault("school_type", "Unbekannte Schulart.")
        if values.get("is_school") is False:
            values["school_type"] = None
        if values.pop("no_address", False):
            values["address"] = ""  # deliberately none, unlike an unknown address (None)
    if kind is CONTEXTS:
        slug = values.get("page_slug")
        if slug and not _SLUG.fullmatch(str(slug)):
            errors["page_slug"] = "Nur Kleinbuchstaben, Ziffern und Bindestriche."
    if kind is FORMATS and new and values.get("duration_hours") == 0:
        errors["duration_hours"] = "Bitte die übliche Dauer angeben."
    if kind is PEOPLE:
        email = values.get("email")
        if email and not _EMAIL.fullmatch(str(email)):
            errors["email"] = "Keine gültige E-Mail-Adresse."
        if not new and values.get("weekly_hours") is not None and values["weekly_hours"] > 80:
            errors["weekly_hours"] = "Höchstens 80 Stunden."
    return values, errors


def _duplicate(session: Session, kind: Kind, values: dict, exclude_id: int | None = None):
    """An existing entry the values would clash with (same name, case ignored)."""
    model = kind.model
    if kind is FORMATS:
        name = values.get("name")
        stmt = select(Format).where(
            func.lower(Format.name) == str(name).lower() if name else Format.name.is_(None),
            Format.category.is_not_distinct_from(values.get("category")),
        )
    elif kind is PEOPLE:
        conditions = [func.lower(Person.email) == str(values["email"]).lower()] if values.get("email") else []
        if values.get("slack_id"):
            conditions.append(Person.slack_id == values["slack_id"])
        if not conditions:
            return None
        stmt = select(Person).where(or_(*conditions))
    else:
        stmt = select(model).where(func.lower(model.name) == str(values.get("name") or "").lower())
    if exclude_id is not None:
        stmt = stmt.where(model.id != exclude_id)
    return session.scalars(stmt).first()


# --- Writing ---------------------------------------------------------------------------------


def _log(
    session: Session,
    table: str,
    row_id: int,
    field: str | None,
    old,
    new,
    by: str,
    reason: str | None = None,
) -> None:
    session.add(
        HistoryEntry(
            table_name=table,
            row_id=row_id,
            field=field,
            old_value=None if old is None else str(old),
            new_value=None if new is None else str(new),
            changed_by=by,
            reason=reason,
        )
    )


def _json(value):
    return str(value) if isinstance(value, Decimal) else value


@dataclass
class Proposed:
    """The result of a proposal: the entry to use, and whether it is new."""

    entry: object
    created: bool
    note: str | None = None


def propose(session: Session, kind: Kind, values: dict, by: str, reason: str | None = None) -> Proposed:
    """Add a proposed entry (usable at once) with its Vorschlag and Verlauf row.

    If the entry exists already (same name, case ignored), nothing is added: an active entry is
    used as it is, a merged one through the entry it was merged into; a rejected one is refused.
    """
    existing = _duplicate(session, kind, values)
    if existing is not None:
        if existing.status == REJECTED:
            raise ListError(f"„{label(existing)}“ wurde abgelehnt; bitte einen anderen Eintrag wählen.")
        target = resolve(session, existing)
        note = f"„{label(existing)}“ gibt es schon"
        if target is not existing:
            note += f"; zusammengeführt in „{label(target)}“"
        return Proposed(target, False, note + ".")
    if kind is PEOPLE:
        values = {"active": True, **values}
    entry = kind.model(**values, status=PROPOSED)
    session.add(entry)
    session.flush()
    table = kind.model.__tablename__
    session.add(
        Proposal(
            kind="eintrag",
            target_table=table,
            target_id=entry.id,
            proposed_values={k: _json(v) for k, v in values.items()},
            reason=reason,
            proposed_by=by,
        )
    )
    _log(session, table, entry.id, "status", None, PROPOSED, by, reason or "neu vorgeschlagen")
    session.flush()
    return Proposed(entry, True)


def may_edit(entry, admin: bool) -> bool:
    if entry.status == MERGED:
        return False
    return admin or entry.status == PROPOSED


def update(session: Session, kind: Kind, entry, values: dict, by: str, admin: bool) -> list[str]:
    """Change an entry's details; returns the changed fields. Every change goes to the Verlauf, and
    the target groups of the courses follow a changed school or teacher-training flag."""
    if not may_edit(entry, admin):
        raise ListError("Bestätigte Einträge ändert ein Admin.")
    if (other := _duplicate(session, kind, values, exclude_id=entry.id)) is not None:
        raise ListError(f"„{label(other)}“ gibt es schon.")
    table = kind.model.__tablename__
    changed = []
    for name, new in values.items():
        old = getattr(entry, name)
        if old == new:
            continue
        _log(session, table, entry.id, name, old, new, by)
        setattr(entry, name, new)
        changed.append(name)
    session.flush()
    if {"is_school", "teacher_training"} & set(changed):
        column = Course.place_id if kind is PLACES else Course.context_id
        courses = session.scalars(select(Course).where(column == entry.id))
        _recompute_target_groups(session, courses, by, f"{kind.singular} „{label(entry)}“ geändert")
    return changed


def _decide(session: Session, entry, decision: str, by: str, reason: str | None) -> None:
    now = dt.datetime.now(dt.UTC)
    proposals = session.scalars(
        select(Proposal).where(
            Proposal.kind == "eintrag",
            Proposal.target_table == entry.__tablename__,
            Proposal.target_id == entry.id,
            Proposal.decision.is_(None),
        )
    )
    for proposal in proposals:
        proposal.decision = DECIDED[decision]
        proposal.decided_by = by
        proposal.decided_at = now
        proposal.decision_reason = reason


def missing_details(kind: Kind, entry) -> list[str]:
    """Required details an entry lacks before it can be confirmed."""
    missing = []
    if kind is PLACES:
        if entry.is_school is None:
            missing.append("Schule?")
        elif entry.is_school and not entry.school_type:
            missing.append("Schulart")
        if entry.address is None:
            missing.append("Adresse (oder „bewusst ohne Adresse“)")
    elif kind is FORMATS:
        if not entry.category:
            missing.append("Kategorie")
        if entry.duration_hours is None:
            missing.append("Dauer")
    return missing


def confirm(session: Session, kind: Kind, entry, by: str, reason: str | None = None) -> None:
    """Admin: a proposed (or earlier rejected) entry becomes a permanent part of the list."""
    if entry.status not in (PROPOSED, REJECTED):
        raise ListError("Nur vorgeschlagene oder abgelehnte Einträge können bestätigt werden.")
    if missing := missing_details(kind, entry):
        raise ListError("Vor dem Bestätigen ergänzen: " + ", ".join(missing) + ".")
    _log(session, kind.model.__tablename__, entry.id, "status", entry.status, CONFIRMED, by, reason)
    entry.status = CONFIRMED
    _decide(session, entry, CONFIRMED, by, reason)
    session.flush()


def reject(session: Session, kind: Kind, entry, by: str, reason: str) -> int:
    """Admin: reject a proposal with a reason. Returns the number of courses still using it."""
    if entry.status != PROPOSED:
        raise ListError("Nur vorgeschlagene Einträge können abgelehnt werden; sonst zusammenführen.")
    if not clean_name(reason):
        raise ListError("Bitte einen Grund angeben.")
    _log(session, kind.model.__tablename__, entry.id, "status", entry.status, REJECTED, by, reason)
    entry.status = REJECTED
    _decide(session, entry, REJECTED, by, reason)
    session.flush()
    return usage(session, kind, [entry.id]).get(entry.id, Usage()).courses


def merge(session: Session, kind: Kind, entry, target, by: str, reason: str | None) -> int:
    """Admin: `entry` is the same as `target`. Every course and Termin moves to `target` (each with
    a Verlauf row); the entry stays as a row with status `zusammengeführt`. Returns the number of
    moved courses (people: Termine)."""
    if entry.status == MERGED:
        raise ListError("Dieser Eintrag ist schon zusammengeführt.")
    if target is None or target.id == entry.id or target.status not in ACTIVE_STATUSES:
        raise ListError("Bitte einen anderen, aktiven Eintrag als Ziel wählen.")
    text = f"zusammengeführt: „{label(entry)}“ in „{label(target)}“"
    why = f"{text}: {reason}" if clean_name(reason) else text
    moved = 0
    if kind is PEOPLE:
        moved = _move_person(session, entry, target, by, why)
    else:
        column = {PLACES: "place_id", CONTEXTS: "context_id", FORMATS: "format_id"}[kind]
        courses = list(session.scalars(select(Course).where(getattr(Course, column) == entry.id)))
        for course in courses:
            _log(session, "courses", course.id, column, entry.id, target.id, by, why)
            setattr(course, column, target.id)
        session.flush()
        for course in courses:
            session.refresh(course)
        if kind in (PLACES, CONTEXTS):
            _recompute_target_groups(session, courses, by, why)
        moved = len(courses)
    table = kind.model.__tablename__
    _log(session, table, entry.id, "status", entry.status, MERGED, by, why)
    _log(session, table, entry.id, "merged_into_id", None, target.id, by, why)
    entry.status = MERGED
    entry.merged_into_id = target.id
    _decide(session, entry, MERGED, by, why)
    session.flush()
    return moved


def _move_person(session: Session, person: Person, target: Person, by: str, why: str) -> int:
    appointments = set(session.scalars(select(Appointment).where(Appointment.lead_id == person.id)))
    appointments |= set(
        session.scalars(select(Appointment).where(Appointment.support.any(Person.id == person.id)))
    )
    for appointment in appointments:
        if appointment.lead_id == person.id:
            _log(session, "appointments", appointment.id, "lead_id", person.id, target.id, by, why)
            appointment.lead_id = target.id
            appointment.lead = target
        if person in appointment.support:
            old = sorted(p.id for p in appointment.support)
            support = [p for p in appointment.support if p.id != person.id]
            if target.id != appointment.lead_id and target not in support:
                support.append(target)
            appointment.support = support
            new = sorted(p.id for p in support)
            _log(session, "appointments", appointment.id, "support", old, new, by, why)
    session.flush()
    return len(appointments)


# --- Login and person ------------------------------------------------------------------------


def link_login(session: Session, subject: str, email: str | None) -> Person | None:
    """The person of a login. At the first login the person is found by the (verified) e-mail of
    the login, exactly and case ignored, and linked to the login for good; never by name."""
    linked = session.scalars(select(Person).where(Person.login_subject == subject)).first()
    if linked is not None:
        return resolve(session, linked)
    if not email or not subject:
        return None
    matches = list(
        session.scalars(
            select(Person).where(
                func.lower(Person.email) == email.strip().lower(), Person.status.in_(ACTIVE_STATUSES)
            )
        )
    )
    if len(matches) != 1 or matches[0].login_subject is not None:
        return None
    person = matches[0]
    person.login_subject = subject
    _log(
        session,
        "people",
        person.id,
        "login_subject",
        None,
        subject,
        LOGIN,
        "bei der ersten Anmeldung über die E-Mail-Adresse verknüpft",
    )
    session.flush()
    return person


def person_for_login(session: Session, subject: str) -> Person | None:
    linked = session.scalars(select(Person).where(Person.login_subject == subject)).first()
    return resolve(session, linked) if linked is not None else None


def counts_by_status(session: Session, kind: Kind) -> dict[str, int]:
    rows = session.execute(select(kind.model.status, func.count()).group_by(kind.model.status))
    counts = defaultdict(int, dict(rows.all()))
    return {status: counts[status] for status in STATUS_LABELS}
