"""Contact persons at places (WM-141, PROPOSAL.md section 4.6).

A Kontaktperson (name, role, e-mail, phone) belongs to a place and is chosen on a course, like the
team. Any team member adds, changes, chooses or deletes one; there is no admin confirmation, because
a contact is not a list entry the statistics count.

Contacts are personal data of people outside the team, so they stay in two places only: the course
page and the place in Stammdaten, both for logged-in team members. They are never in an address
(IDs only), a log, an export, a chart or key figure, the calendar event or a Slack message. The
Verlauf records who added, changed, chose or deleted a contact and when, but never the values: an edit
names only the changed field.

Deleting a contact is the one sanctioned deletion here, and only on request: a person ticks the
confirmation, the row is removed, every course that named it loses it, and the Verlauf of the place
and of those courses says "Kontakt gelöscht" (who, when). No other table keeps a copy of the values,
so the next nightly dump no longer holds them (older dumps do until they are pruned, see
`metrics.backup`). Nothing deletes a contact automatically.
"""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from metrics import lists
from metrics.db import Contact, Course, Place
from metrics.instance import clean_name

TABLE = "contacts"
ADDED = "Kontakt angelegt"
CHANGED = "Kontakt geändert"
CHOSEN = "Kontaktperson gewählt"
REMOVED = "Kontaktperson entfernt"
DELETED = "Kontakt gelöscht"
OTHER_PLACE = "Kontaktperson entfernt: gehört zu einem anderen Ort"

FIELDS = (
    ("name", "Name"),
    ("role", "Funktion"),
    ("email", "E-Mail"),
    ("phone", "Telefon"),
)
MAX_LENGTH = {"name": 120, "role": 120, "email": 254, "phone": 40}
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_PHONE = re.compile(r"[0-9+()/ .\-]{3,40}")


class ContactError(ValueError):
    """A request the rules do not allow; the message is shown to the person (German)."""


def place_of(session: Session, course: Course) -> Place | None:
    """The place whose contacts a course offers: its place, or the place it was merged into."""
    return lists.resolve(session, course.place) if course.place is not None else None


def for_place(session: Session, place: Place | None) -> list[Contact]:
    if place is None:
        return []
    stmt = select(Contact).where(Contact.place_id == place.id)
    return sorted(session.scalars(stmt), key=lambda c: c.name.casefold())


def parse(form: dict[str, str]) -> tuple[dict[str, str | None], dict[str, str]]:
    """Contact values from form strings, and errors per field (German)."""
    values: dict[str, str | None] = {}
    errors: dict[str, str] = {}
    for name, _ in FIELDS:
        raw = clean_name(form.get(name)) or None
        if raw and len(raw) > MAX_LENGTH[name]:
            errors[name] = f"Höchstens {MAX_LENGTH[name]} Zeichen."
        values[name] = raw
    if not values["name"]:
        errors.setdefault("name", "Pflichtfeld")
    if values["email"] and not _EMAIL.fullmatch(values["email"]):
        errors.setdefault("email", "Keine gültige E-Mail-Adresse.")
    if values["phone"] and not _PHONE.fullmatch(values["phone"]):
        errors.setdefault("phone", "Nur Ziffern, Leerzeichen und + ( ) / - .")
    return values, errors


def _log(session: Session, table: str, row_id: int, field: str | None, by: str, reason: str) -> None:
    """A Verlauf row without values: who did what to which contact field, never the data itself."""
    lists._log(session, table, row_id, field, None, None, by, reason)


def add(session: Session, place: Place, values: dict, by: str) -> Contact:
    contact = Contact(place_id=place.id, **values)
    session.add(contact)
    session.flush()
    _log(session, TABLE, contact.id, None, by, ADDED)
    _log(session, "places", place.id, "contact", by, ADDED)
    session.flush()
    return contact


def update(session: Session, contact: Contact, values: dict, by: str) -> list[str]:
    """Change a contact; returns the changed fields. The Verlauf names each field, not its values."""
    changed = []
    for name, new in values.items():
        if getattr(contact, name) != new:
            setattr(contact, name, new)
            _log(session, TABLE, contact.id, name, by, CHANGED)
            changed.append(name)
    if changed:
        _log(session, "places", contact.place_id, "contact", by, CHANGED)
    session.flush()
    return changed


def choose(session: Session, course: Course, contact: Contact | None, by: str) -> bool:
    """Set (or with None, remove) the contact of a course; it must belong to the course's place."""
    if contact is not None:
        place = place_of(session, course)
        if place is None or contact.place_id != place.id:
            raise ContactError("Die Kontaktperson gehört nicht zum Ort dieses Kurses.")
    if course.contact_id == (contact.id if contact else None):
        return False
    course.contact = contact
    _log(session, "courses", course.id, None, by, CHOSEN if contact else REMOVED)
    session.flush()
    return True


def drop_if_elsewhere(session: Session, course: Course, by: str) -> bool:
    """After the place of a course changed: a contact of another place no longer fits."""
    contact = course.contact
    if contact is None:
        return False
    place = place_of(session, course)
    if place is not None and contact.place_id == place.id:
        return False
    course.contact = None
    _log(session, "courses", course.id, None, by, OTHER_PLACE)
    session.flush()
    return True


def move(session: Session, place: Place, target: Place, by: str, why: str) -> int:
    """A place merged into another: its contacts follow, like its courses (`lists.merge`)."""
    moved = list(session.scalars(select(Contact).where(Contact.place_id == place.id)))
    for contact in moved:
        lists._log(session, TABLE, contact.id, "place_id", place.id, target.id, by, why)
        contact.place_id = target.id
    session.flush()
    return len(moved)


def delete(session: Session, contact: Contact, by: str, really: bool) -> int:
    """Delete a contact on request, after an explicit confirmation. Every course naming it loses it;
    the Verlauf of those courses and of the place keeps "Kontakt gelöscht". Returns the number of
    courses it was removed from."""
    if not really:
        raise ContactError("Bitte bestätigen, dass die Kontaktperson gelöscht werden soll.")
    using = list(session.scalars(select(Course).where(Course.contact_id == contact.id)))
    for course in using:
        course.contact = None
        _log(session, "courses", course.id, None, by, DELETED)
    _log(session, TABLE, contact.id, None, by, DELETED)
    _log(session, "places", contact.place_id, "contact", by, DELETED)
    session.delete(contact)
    session.flush()
    return len(using)
