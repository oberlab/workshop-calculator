"""Equipment needs of formats and Termine (WM-140, PROPOSAL.md 4.5).

The Material list itself is a reference list (`metrics.lists.EQUIPMENT`). A format names what it
usually needs (`Format.equipment`); a new Termin copies it (`copy_needs`), and each Termin can change
its own needs on the course page. When a course gets another format, its Termine in planning whose
needs are still exactly those of the old format take the new format's needs (`follow_format`).

A kit blocks the whole day (decided 2026-11-13): `day_totals` adds up the needs of the Termine of a
day that are not cancelled, whatever their times; the hint "material-knapp" (`metrics.hints`) and the
equipment lanes of the calendar (`metrics.planning.lanes`) read it, and so does the usage table of
the team's Planungszahlen (WM-151). Equipment never counts in a public chart or key figure.
"""

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from metrics import lists
from metrics.db import (
    ABGESAGT,
    ACTIVE_STATUSES,
    Appointment,
    AppointmentEquipment,
    Course,
    Equipment,
    Format,
    FormatEquipment,
)

Needs = dict[int, int]  # equipment ID -> quantity
FIELD = "material_"  # form fields `material_<equipment ID>`; `material=1` says the form has them


def needs_of(rows: Iterable[FormatEquipment | AppointmentEquipment]) -> Needs:
    return {row.equipment_id: row.quantity for row in rows}


def text(rows: Iterable[FormatEquipment | AppointmentEquipment]) -> str:
    """How needs are shown and written to the Verlauf: "20 Calliope mini, 1 Laptopkoffer"."""
    return ", ".join(
        f"{row.quantity} {row.equipment.name}"
        for row in sorted(rows, key=lambda r: r.equipment.name.casefold())
        if row.equipment is not None
    )


def choices(session: Session, needs: Needs | None = None) -> list[Equipment]:
    """What the needs fields offer: the active equipment, plus anything already needed."""
    items = {e.id: e for e in session.scalars(select(Equipment).where(Equipment.status.in_(ACTIVE_STATUSES)))}
    for item_id in needs or {}:
        if item_id not in items and (item := session.get(Equipment, item_id)) is not None:
            items[item.id] = item
    return sorted(items.values(), key=lambda e: e.name.casefold())


def parse(session: Session, form: dict, current: Needs, errors: list[str]) -> Needs:
    """The needs entered in the `material_<id>` fields (empty or 0: not needed). Only active
    equipment can be added; what is needed already stays choosable whatever its status."""
    needs: Needs = {}
    for key, values in form.items():
        if not key.startswith(FIELD) or not lists.is_whole(key[len(FIELD) :]):
            continue
        value = values[-1] if isinstance(values, list) else values
        raw = (value or "").strip()
        if not raw:
            continue
        item = session.get(Equipment, int(key[len(FIELD) :]))
        if item is None:
            continue
        if not lists.is_whole(raw) or int(raw) > lists.MAX_COUNT:
            errors.append(f"Material {item.name}: bitte eine ganze Zahl zwischen 0 und {lists.MAX_COUNT}")
            continue
        if int(raw) == 0:
            continue
        if item.id not in current and item.status not in ACTIVE_STATUSES:
            errors.append(f"Material {item.name}: nicht (mehr) auswählbar")
            continue
        needs[item.id] = int(raw)
    return needs


def _apply(session: Session, rows: list, model, owner: dict, needs: Needs) -> list:
    """The rows for `needs`, keeping the existing row objects where an item stays."""
    existing = {row.equipment_id: row for row in rows}
    result = []
    for item_id, quantity in sorted(needs.items()):
        row = existing.get(item_id) or model(**owner, equipment_id=item_id, quantity=quantity)
        row.quantity = quantity
        row.equipment = session.get(Equipment, item_id)
        result.append(row)
    return result


def set_needs(session: Session, t: Appointment, needs: Needs) -> None:
    """Replace a Termin's needs (the caller writes the Verlauf through the Termin's snapshot)."""
    owner = {"appointment_id": t.id} if t.id else {}
    t.equipment = _apply(session, list(t.equipment), AppointmentEquipment, owner, needs)


def copy_needs(session: Session, t: Appointment, source: Format | Appointment | None) -> None:
    """A new Termin takes the needs of its format, or of the Termin it was made from (a series)."""
    set_needs(session, t, needs_of(source.equipment) if source is not None else {})


def set_format_needs(session: Session, fmt: Format, needs: Needs, by: str, admin: bool) -> bool:
    """Change what a format needs; editing follows the format's own rule (proposed entries by the
    team, confirmed ones by an admin). Returns whether anything changed; the Verlauf has it."""
    if not lists.may_edit(fmt, admin):
        raise lists.ListError("Bestätigte Einträge ändert ein Admin.")
    before = text(fmt.equipment)
    if needs_of(fmt.equipment) == needs:
        return False
    fmt.equipment = _apply(session, list(fmt.equipment), FormatEquipment, {"format_id": fmt.id}, needs)
    session.flush()
    lists._log(session, "formats", fmt.id, "equipment", before or None, text(fmt.equipment) or None, by)
    return True


def follow_format(session: Session, termine: Iterable[Appointment], old: Format | None, new: Format | None):
    """After a course changed its format: Termine whose needs are still those of the old format (or
    empty when it had none) take the new format's needs. Returns the Termine that changed."""
    old_needs = needs_of(old.equipment) if old is not None else {}
    new_needs = needs_of(new.equipment) if new is not None else {}
    changed = []
    for t in termine:
        if needs_of(t.equipment) == old_needs and old_needs != new_needs:
            set_needs(session, t, new_needs)
            changed.append(t)
    return changed


def day_totals(appointments: Iterable[Appointment]) -> dict[dt.date, dict[int, int]]:
    """date -> equipment ID -> needed that day, over the Termine that are not cancelled."""
    totals: dict[dt.date, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    for t in appointments:
        if t.date is None or t.stage == ABGESAGT:
            continue
        for row in t.equipment:
            totals[t.date][row.equipment_id] += row.quantity
    return totals


def others_on(session: Session, day: dt.date, exclude_id: int | None) -> Needs:
    """What the other Termine of a day need (not cancelled), per equipment ID: for the course page."""
    stmt = (
        select(AppointmentEquipment.equipment_id, func.sum(AppointmentEquipment.quantity))
        .join(Appointment, Appointment.id == AppointmentEquipment.appointment_id)
        .where(Appointment.date == day, Appointment.stage != ABGESAGT)
        .group_by(AppointmentEquipment.equipment_id)
    )
    if exclude_id is not None:
        stmt = stmt.where(Appointment.id != exclude_id)
    return {item_id: int(total) for item_id, total in session.execute(stmt)}


def booked_between(session: Session, start: dt.date, end: dt.date) -> list[Appointment]:
    """The Termine from `start` to `end` that need any equipment (not cancelled), for the lanes."""
    needing = select(AppointmentEquipment.appointment_id)
    stmt = (
        select(Appointment)
        .where(
            Appointment.date.between(start, end), Appointment.stage != ABGESAGT, Appointment.id.in_(needing)
        )
        .options(
            selectinload(Appointment.course).selectinload(Course.format),
            selectinload(Appointment.course).selectinload(Course.context),
            selectinload(Appointment.equipment).selectinload(AppointmentEquipment.equipment),
        )
    )
    return list(session.scalars(stmt))


def listed(session: Session) -> list[Equipment]:
    """The equipment the lanes show: active entries, by name."""
    stmt = select(Equipment).where(Equipment.status.in_(ACTIVE_STATUSES))
    return sorted(session.scalars(stmt), key=lambda e: e.name.casefold())
