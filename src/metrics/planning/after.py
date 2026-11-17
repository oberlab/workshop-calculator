"""After the course (WM-133): the phone form for the lead, reached from Mein Bereich and, later, from
the Slack DM the day after a Termin (WM-130).

It shows only what is missing for the final confirmation from the participants' side: the four counts
when TN weiblich or TN männlich is empty, Herkunft (one number per municipality) when the Termin's
context asks for it and none is entered or the numbers do not add up. A note is always offered.

Saving goes through `workflow.save` limited to participants and Herkunft (`workflow.HELD`), so the
stage rules, the Verlauf and the hints are the same as on the course page. The note goes into the
Termin's Verlauf ("Notiz nach dem Kurs"): the course's own note is shared by all Termine of a series,
so the phone form never overwrites it.
"""

from collections import Counter
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from metrics import hints
from metrics.db import Appointment, AppointmentOrigin, Course
from metrics.planning import workflow
from metrics.planning.workflow import FormError

NOTE_REASON = "Notiz nach dem Kurs"
REQUIRED_COUNTS = ("TN weiblich", "TN männlich")  # the labels of `hints.FINAL_FIELDS`
SUGGESTED = 8  # municipality rows offered from earlier Termine
FREE_ROWS = 2  # empty rows for a further municipality


@dataclass(frozen=True)
class Gaps:
    counts: bool  # TN weiblich or TN männlich is empty
    origins: bool  # Herkunft asked for, and empty or not adding up

    @property
    def any(self) -> bool:
        return self.counts or self.origins


def open_for_entry(t: Appointment) -> bool:
    """Participants can be entered in every stage in planning (Alex, 2026-11-13)."""
    return workflow.HELD in workflow.editable(t)


def origins_total(t: Appointment) -> int:
    return sum(o.participants for o in t.origins)


def gaps(t: Appointment) -> Gaps:
    absent = set(hints.missing(t, hints.FINAL_FIELDS))
    counts = bool(absent & set(REQUIRED_COUNTS))
    origins = workflow.asks_municipalities(t) and (
        not t.origins or origins_total(t) != (hints.participants(t) or 0)
    )
    return Gaps(counts, origins)


def suggested_municipalities(session: Session, t: Appointment) -> list[str]:
    """Rows to offer: the Termin's own municipalities, those of its course, then the most frequent
    ones of the same context."""
    names = [o.municipality for o in t.origins]
    course = Counter(o.municipality for other in t.course.appointments for o in other.origins)
    names += [m for m, _ in course.most_common()]
    if t.context_id is not None:
        rows = session.execute(
            select(AppointmentOrigin.municipality, func.count())
            .join(Appointment, Appointment.id == AppointmentOrigin.appointment_id)
            .join(Course, Course.id == Appointment.course_id)
            .where(Appointment.context_id == t.context_id)
            .group_by(AppointmentOrigin.municipality)
            .order_by(func.count().desc(), AppointmentOrigin.municipality)
            .limit(SUGGESTED)
        )
        names += [m for m, _ in rows]
    unique = list(dict.fromkeys(names))
    own = len(t.origins)
    return unique[: max(SUGGESTED, own)]


def record(session: Session, t: Appointment, form: workflow.Form, by: str) -> list[str]:
    """Save the phone form; returns the changed fields (plus "note" for a note)."""
    if not open_for_entry(t):
        raise FormError(f"Ein Termin in „{t.stage}“ kann hier nicht mehr geändert werden.")
    if all(name in form for name, _ in workflow.PARTICIPANT_FIELDS):
        counts = [workflow.one(form, name) for name, _ in workflow.PARTICIPANT_FIELDS]
        if all(c in ("", "0") for c in counts):
            raise FormError("Bitte die Teilnehmenden eintragen: Alle vier Zahlen sind 0.")
    changed = workflow.save(session, t, form, by, only={workflow.HELD})
    note = (form.get("note") or [""])[0].strip()
    if note:
        workflow.log(session, t, None, None, note, by, NOTE_REASON)
        changed.append("note")
    return changed
