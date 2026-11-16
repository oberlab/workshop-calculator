"""Corrections of abgeschlossen Termine (WM-132): proposed by the team, confirmed by an admin.

An Abgeschlossen Termin (legacy included) is locked because the charts count it. To change it, a team
member proposes a correction with a reason (`propose`); it is stored as a Vorschlag of kind
`korrektur` and changes nothing. An admin confirms it in "Prüfen" (`confirm`), which applies it with
one Verlauf row per field naming both people, or rejects it with a reason (`reject`).

What can be corrected (`FIELDS`): the course's format, context, place, class/age and note (shared by
every Termin of the course) and the Termin's date, start, end, lead, support and participants (the
four counts, or the recorded total of a legacy Termin that has no split). A Vorschlag keeps, per
field, the value it found and the proposed one (`{field: {"old", "new", "old_text", "new_text"}}`).
Confirming refuses when a field has changed since the proposal, or when a chosen list entry is not
confirmed (nothing unconfirmed enters the charts).
"""

import datetime as dt
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from metrics import courses, hints, lists
from metrics.db import (
    ABGESCHLOSSEN,
    ACTIVE_STATUSES,
    CONFIRMED,
    REJECTED,
    Appointment,
    Context,
    Format,
    Person,
    Place,
    Proposal,
)
from metrics.ingest.records import parse_class_age, to_text
from metrics.instance import clean_name
from metrics.planning import workflow
from metrics.planning.workflow import PARTICIPANT_FIELDS, FormError

KIND = "korrektur"
TABLE = "appointments"
COUNTS = tuple(name for name, _ in PARTICIPANT_FIELDS)

# The fields that hold a list entry, and its model. Course fields first, as on the form.
ENTRY_FIELDS = {"format": Format, "context": Context, "place": Place, "lead": Person}
COURSE_FIELDS = ("format", "context", "place", "class_age_text", "note")
TERMIN_FIELDS = ("date", "start_time", "end_time", "lead", "support", *COUNTS, "participants_total")
FIELDS = COURSE_FIELDS + TERMIN_FIELDS
LABELS = workflow.LABELS


class CorrectionError(FormError):
    """A correction the rules do not allow; the messages are shown to the person (German)."""


# --- Values: JSON-friendly ("raw") and as shown ---------------------------------------------------


def _owner(t: Appointment, name: str):
    return t.course if name in COURSE_FIELDS else t


def current(t: Appointment) -> dict:
    """The correctable values of a Termin as stored in a Vorschlag (IDs, ISO dates, "HH:MM")."""
    values = {}
    for name in FIELDS:
        if name in ENTRY_FIELDS:
            entry = getattr(_owner(t, name), name)
            values[name] = entry.id if entry is not None else None
        elif name == "support":
            values[name] = sorted(p.id for p in t.support)
        elif name == "date":
            values[name] = t.date.isoformat() if t.date else None
        elif name in ("start_time", "end_time"):
            value = getattr(t, name)
            values[name] = value.strftime("%H:%M") if value else None
        else:
            values[name] = getattr(_owner(t, name), name)
    return values


def show(session: Session, name: str, value) -> str:
    """A raw value as the Verlauf shows it."""
    if value is None or value == []:
        return ""
    if name in ENTRY_FIELDS:
        entry = session.get(ENTRY_FIELDS[name], value)
        return lists.label(entry) if entry is not None else f"#{value}"
    if name == "support":
        people = [session.get(Person, i) for i in value]
        return ", ".join(sorted(p.name for p in people if p is not None))
    if name == "date":
        return dt.date.fromisoformat(value).strftime("%d.%m.%Y")
    return str(value)


def only_total(t: Appointment) -> bool:
    """A legacy Termin with a recorded total and no split: its total is corrected directly."""
    return all(getattr(t, name) is None for name in COUNTS) and t.participants_total is not None


# --- Reading the form -----------------------------------------------------------------------------


def _entry_id(session: Session, model, label: str, value: str, was, errors: list) -> int | None:
    """The ID of an active list entry chosen in a select (or the one already set)."""
    if not value:
        return None
    if not value.isdigit():
        errors.append(f"{label}: ungültige Auswahl")
        return was
    entry = session.get(model, int(value))
    if entry is None or (entry.id != was and entry.status not in ACTIVE_STATUSES):
        errors.append(f"{label}: nicht (mehr) auswählbar")
        return was
    return entry.id


def _total(t: Appointment, values: dict, form: workflow.Form, errors: list) -> int | None:
    """The total after the correction. A legacy total can be more than its split (people recorded
    in neither count), so a changed count moves the total by the difference instead of replacing it;
    a first split of a total-only Termin replaces the total."""
    old = [getattr(t, name) for name in COUNTS]
    new = [values[name] for name in COUNTS]
    if any(c is not None for c in new):
        added = sum(c or 0 for c in new) - sum(c or 0 for c in old)
        had_split = any(c is not None for c in old)
        if had_split and t.participants_total is not None:
            return t.participants_total + added
        return sum(c or 0 for c in new)
    if any(c is not None for c in old):
        errors.append("Teilnehmende: ein abgeschlossener Termin braucht die Zahlen")
        return t.participants_total
    if "participants_total" in form:
        return workflow._count(workflow.one(form, "participants_total"), "TN gesamt", errors)
    return t.participants_total


def parse(session: Session, t: Appointment, form: workflow.Form) -> dict:
    """The values the correction form asks for (fields missing from the form keep theirs)."""
    one = lambda name: workflow.one(form, name)  # noqa: E731
    values = current(t)
    errors: list[str] = []
    for name in ENTRY_FIELDS:
        if name in form:
            model = ENTRY_FIELDS[name]
            values[name] = _entry_id(session, model, LABELS[name], one(name), values[name], errors)
    if "class_age_text" in form:
        values["class_age_text"] = to_text(one("class_age_text"))
    if "note" in form:
        values["note"] = (form.get("note") or [""])[0].strip() or None
    if "date" in form:
        day = workflow.parse_date(one("date"), "Datum", errors)
        if day is None and not errors:
            errors.append("Datum: ein abgeschlossener Termin braucht ein Datum")
        values["date"] = day.isoformat() if day else values["date"]
    for name, label in (("start_time", "Beginn"), ("end_time", "Ende")):
        if name in form:
            time = workflow._time(one(name), label, errors)
            values[name] = time.strftime("%H:%M") if time else None
    start, end = values["start_time"], values["end_time"]
    stored = current(t)
    if (start, end) != (stored["start_time"], stored["end_time"]) and start and end and end <= start:
        errors.append("Ende: muss nach dem Beginn liegen")
    # The form marks its support checkboxes with `team`, so ticking nobody clears the support.
    if "support" in form or "team" in form:
        ids = []
        had = set(values["support"])
        for value in dict.fromkeys(clean_name(v) for v in form.get("support", [])):
            was = int(value) if value.isdigit() and int(value) in had else None
            person = _entry_id(session, Person, "Unterstützung", value, was, errors)
            if person is not None and person != values["lead"]:
                ids.append(person)
        values["support"] = sorted(set(ids))
    for name, label in PARTICIPANT_FIELDS:
        if name in form:
            values[name] = workflow._count(one(name), f"TN {label}", errors)
    values["participants_total"] = _total(t, values, form, errors)
    if errors:
        raise CorrectionError(errors)
    return values


# --- Proposing, confirming, rejecting -------------------------------------------------------------


def _summary(changes: dict) -> str:
    return "; ".join(
        f"{LABELS[name]} {c['old_text'] or 'leer'} → {c['new_text'] or 'leer'}" for name, c in changes.items()
    )


def propose(session: Session, t: Appointment, form: workflow.Form, reason: str, by: str) -> Proposal:
    """A team member's correction of an Abgeschlossen Termin: stored, not applied."""
    if t.stage != ABGESCHLOSSEN:
        raise CorrectionError(
            "Korrekturen gibt es nur für abgeschlossene Termine; die anderen werden direkt geändert."
        )
    reason = clean_name(reason)
    if not reason:
        raise CorrectionError("Bitte einen Grund für die Korrektur angeben.")
    before, after = current(t), parse(session, t, form)
    changes = {
        name: {
            "old": before[name],
            "new": after[name],
            "old_text": show(session, name, before[name]),
            "new_text": show(session, name, after[name]),
        }
        for name in FIELDS
        if before[name] != after[name]
    }
    if not changes:
        raise CorrectionError("Die Korrektur ändert nichts.")
    proposal = Proposal(
        kind=KIND,
        target_table=TABLE,
        target_id=t.id,
        proposed_values=changes,
        reason=reason,
        proposed_by=by,
    )
    session.add(proposal)
    workflow.log(session, t, None, None, _summary(changes), by, f"Korrektur vorgeschlagen: „{reason}“")
    session.flush()
    return proposal


def open_for(session: Session, appointment_ids: list[int] | None) -> list[Proposal]:
    """The undecided corrections of these Termine (all of them with None), oldest first."""
    stmt = select(Proposal).where(Proposal.kind == KIND, Proposal.decision.is_(None))
    if appointment_ids is not None:
        stmt = stmt.where(Proposal.target_table == TABLE, Proposal.target_id.in_(appointment_ids or [-1]))
    return list(session.scalars(stmt.order_by(Proposal.proposed_at, Proposal.id)))


def _open(session: Session, proposal_id: int) -> tuple[Proposal, Appointment]:
    proposal = session.get(Proposal, proposal_id)
    if proposal is None or proposal.kind != KIND or proposal.target_table != TABLE:
        raise CorrectionError("Unbekannte Korrektur.")
    if proposal.decision is not None:
        raise CorrectionError(f"Über diese Korrektur ist schon entschieden ({proposal.decision}).")
    t = session.get(Appointment, proposal.target_id)
    if t is None:
        raise CorrectionError("Der Termin dieser Korrektur gibt es nicht mehr.")
    return proposal, t


def _decide(proposal: Proposal, decision: str, by: str, reason: str | None) -> None:
    proposal.decision = lists.DECIDED[decision]
    proposal.decided_by = by
    proposal.decided_at = dt.datetime.now(dt.UTC)
    proposal.decision_reason = reason


def _apply(session: Session, t: Appointment, changes: dict) -> None:
    course = t.course
    for name, change in changes.items():
        new = change["new"]
        if name in ENTRY_FIELDS:
            setattr(_owner(t, name), name, session.get(ENTRY_FIELDS[name], new) if new is not None else None)
        elif name == "support":
            t.support = [p for i in new if (p := session.get(Person, i)) is not None]
        elif name == "date":
            t.date = dt.date.fromisoformat(new)
        elif name in ("start_time", "end_time"):
            setattr(t, name, dt.time.fromisoformat(new) if new else None)
        elif name == "class_age_text":
            parsed = parse_class_age(new)
            course.class_age_text = new
            course.class_level, course.age, course.age_band = parsed.class_level, parsed.age, parsed.age_band
        else:
            setattr(_owner(t, name), name, new)
    if {"start_time", "end_time"} & set(changes) and t.start_time and t.end_time:
        t.duration_hours = workflow._duration(t)
    if {"format", "context", "place", "class_age_text"} & set(changes):
        course.target_group = lists.course_target_group(course)


def confirm(session: Session, proposal_id: int, by: str, reason: str | None = None) -> Appointment:
    """Admin: apply a correction. Every changed field is a Verlauf row by the admin that names the
    person who proposed it. Returns the Termin."""
    proposal, t = _open(session, proposal_id)
    if t.stage != ABGESCHLOSSEN:
        raise CorrectionError("Der Termin ist nicht mehr abgeschlossen; bitte die Korrektur ablehnen.")
    changes = proposal.proposed_values
    now = current(t)
    moved = [LABELS[name] for name, c in changes.items() if now.get(name) != c["old"]]
    if moved:
        raise CorrectionError(
            f"Seit dem Vorschlag geändert: {', '.join(moved)}. Bitte ablehnen und neu vorschlagen lassen."
        )
    for name in ENTRY_FIELDS:
        if name in changes and changes[name]["new"] is not None:
            entry = session.get(ENTRY_FIELDS[name], changes[name]["new"])
            if entry is None or entry.status != CONFIRMED:
                raise CorrectionError(f"{LABELS[name]} „{changes[name]['new_text']}“ ist nicht bestätigt.")
    for i in changes.get("support", {}).get("new") or []:
        person = session.get(Person, i)
        if person is None or person.status != CONFIRMED:
            raise CorrectionError(f"Unterstützung „{show(session, 'lead', i)}“ ist nicht bestätigt.")
    reason = clean_name(reason) or None
    why = f"Korrektur von {proposal.proposed_by}: „{proposal.reason}“"
    if reason:
        why += f"; bestätigt: „{reason}“"
    course = t.course
    before_course, before = workflow.course_snapshot(course), workflow.appointment_snapshot(t)
    total = t.participants_total
    _apply(session, t, changes)
    session.flush()
    workflow._record(session, "courses", course.id, before_course, workflow.course_snapshot(course), by, why)
    workflow._record(session, TABLE, t.id, before, workflow.appointment_snapshot(t), by, why)
    if total != t.participants_total:
        workflow.log(session, t, "participants_total", total, t.participants_total, by, why)
    _decide(proposal, CONFIRMED, by, reason)
    courses.renumber(session, course, by)
    hints.refresh(session, [a.id for a in course.appointments])
    return t


def reject(session: Session, proposal_id: int, by: str, reason: str) -> Appointment:
    """Admin: reject a correction with a reason; nothing changes but the Vorschlag and the Verlauf."""
    reason = clean_name(reason)
    if not reason:
        raise CorrectionError("Bitte einen Grund für die Ablehnung angeben.")
    proposal, t = _open(session, proposal_id)
    _decide(proposal, REJECTED, by, reason)
    workflow.log(
        session,
        t,
        None,
        None,
        _summary(proposal.proposed_values),
        by,
        f"Korrektur von {proposal.proposed_by} abgelehnt: „{reason}“",
    )
    session.flush()
    return t


# --- For the pages --------------------------------------------------------------------------------


@dataclass(frozen=True)
class Waiting:
    """A correction in the "Prüfen" queue, with its Termin."""

    proposal: Proposal
    appointment: Appointment
    title: str
    changes: list[tuple[str, str, str]]  # label, old, new


def changes(proposal: Proposal) -> list[tuple[str, str, str]]:
    return [
        (LABELS.get(name, name), c.get("old_text") or "", c.get("new_text") or "")
        for name, c in proposal.proposed_values.items()
    ]


def waiting(session: Session) -> list[Waiting]:
    found = []
    for proposal in open_for(session, None):
        t = session.get(Appointment, proposal.target_id) if proposal.target_table == TABLE else None
        if t is not None:
            found.append(Waiting(proposal, t, workflow.title(t), changes(proposal)))
    return found
