"""Appointment series (WM-111, PROPOSAL.md 4.2): more Termine for a course, edits for "dieser und
alle folgenden", and the bulk actions "Alle bestätigen" and "Restliche absagen".

- Shared fields (format, context, place, class/age, note) are on the course; each Termin has its own
  date, times and team and can deviate. New Termine copy the times and the team of a template
  Termin (the one open on the course page) and start in Geplant.
- "Termine hinzufügen": weekly or every two weeks, n times from a first date, or picked dates.
  Holidays are not skipped (decided 2026-11-13): single dates are moved or deleted by hand.
- The Terminnummer is computed from the date order (`courses.renumber`); the charts count the
  participants of number 1 (`analytics.totals`).
- "Dieser und alle folgenden" applies the changed date (as a shift by the same number of days),
  times and team of a Termin to the later Termine that are still in planning (Geplant, Bestätigt);
  every changed Termin gets its own Verlauf rows.
- Bulk actions go through the single-Termin steps of `metrics.planning.workflow`, so the rules are
  the same; Termine that cannot take the step are skipped and named.

The calendar is written by the web layer after the change is committed: one update per changed
Termin and action (`metrics.planning.caldav`).
"""

import datetime as dt
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from metrics import courses, hints
from metrics.db import BESTAETIGT, GEPLANT, Appointment, Course
from metrics.instance import clean_name
from metrics.planning import workflow
from metrics.planning.workflow import TEAM, TIMES, Form, FormError

ONCE, WEEKLY, BIWEEKLY, DATES = "einmal", "woechentlich", "zweiwoechentlich", "daten"
RHYTHMS = {
    WEEKLY: ("wöchentlich", 7),
    BIWEEKLY: ("alle zwei Wochen", 14),
}
MAX_TERMINE = 52  # one school year of weekly Termine and some
FOLLOWING = "folgende"  # form value of "dieser und alle folgenden"
PLANNING = (GEPLANT, BESTAETIGT)  # the stages "dieser und alle folgenden" changes


# --- Dates ---------------------------------------------------------------------------------------


def series_dates(first: dt.date, rhythm: str, count: int) -> list[dt.date]:
    """`count` dates from `first`, every week or every two weeks."""
    step = dt.timedelta(days=RHYTHMS[rhythm][1])
    return [first + i * step for i in range(count)]


def parse_dates(form: Form, errors: list[str], *, prefix: str = "") -> list[dt.date]:
    """The dates a "Termine hinzufügen" form asks for: `rhythmus` (weekly, every two weeks, picked
    dates), `erster` and `anzahl`, or the `datum` fields. `prefix` names the fields of the new course
    form, where the first date is the Termin's own date (`date`) and is not repeated."""
    rhythm = workflow.one(form, f"{prefix}rhythmus") or ONCE
    if rhythm == ONCE:
        return []
    if rhythm == DATES:
        days = {
            d
            for v in form.get(f"{prefix}datum", [])
            if (d := workflow.parse_date(v.strip(), "Datum", errors))
        }
        if not days and not errors:
            errors.append("Bitte mindestens ein Datum wählen.")
        if len(days) > MAX_TERMINE:
            errors.append(f"Höchstens {MAX_TERMINE} Termine auf einmal.")
        return sorted(days)
    if rhythm not in RHYTHMS:
        errors.append("Unbekannter Rhythmus.")
        return []
    first_name = "date" if prefix else "erster"
    first = workflow.parse_date(workflow.one(form, first_name), "Erster Termin", errors)
    if first is None and not errors:
        errors.append("Für eine Reihe braucht es das Datum des ersten Termins.")
    raw = workflow.one(form, f"{prefix}anzahl")
    count = int(raw) if raw.isdigit() else 0
    if not 1 <= count <= MAX_TERMINE:
        errors.append(f"Anzahl der Termine: zwischen 1 und {MAX_TERMINE}.")
        return []
    if first is None:
        return []
    return series_dates(first, rhythm, count)


# --- Adding Termine ------------------------------------------------------------------------------


def add(
    session: Session, course: Course, dates: list[dt.date], template: Appointment | None, by: str
) -> list[Appointment]:
    """New Geplant Termine on these dates, with the times and team of `template`."""
    if not dates:
        raise FormError("Keine Termine zum Hinzufügen.")
    added = []
    for day in dates:
        t = courses.add_appointment(
            session,
            course,
            by=by,
            date=day,
            start_time=template.start_time if template else None,
            end_time=template.end_time if template else None,
            lead_id=template.lead_id if template else None,
            support_ids=tuple(p.id for p in template.support) if template else (),
            stage=GEPLANT,
        )
        t.duration_hours = workflow._duration(t)
        workflow._record(session, "appointments", t.id, {}, workflow.appointment_snapshot(t), by)
        added.append(t)
    courses.renumber(session, course, by)
    hints.refresh(session, [t.id for t in course.appointments])
    return added


def create(session: Session, form: Form, by: str) -> Appointment:
    """A new course from the course form; with `reihe_rhythmus` and `reihe_anzahl` as a series in one
    step (the first Termin is the form's date, the others follow it). Returns the first Termin."""
    errors: list[str] = []
    dates = parse_dates(form, errors, prefix="reihe_")
    if errors:
        raise FormError(errors)
    t = workflow.create(session, form, by)
    later = [d for d in dates if d != t.date]
    if later:
        add(session, t.course, later, t, by)
    return t


# --- Dieser und alle folgenden -------------------------------------------------------------------


def following(t: Appointment) -> list[Appointment]:
    """The Termine after `t` in date order that are still in planning (Geplant, Bestätigt)."""
    ordered = courses.in_order(t.course.appointments)
    later = ordered[ordered.index(t) + 1 :] if t in ordered else []
    return [a for a in later if a.stage in PLANNING]


def save(session: Session, t: Appointment, form: Form, by: str, scope: str = "") -> list[Appointment]:
    """Save the course form for `t` (`workflow.save`); with `scope` "folgende" the changed date (as a
    shift), times and team go to the following Termine too. Returns the Termine that changed besides
    `t` (for the calendar)."""
    later = following(t) if scope == FOLLOWING and t.stage in PLANNING else []
    old_date, old_times = t.date, (t.start_time, t.end_time)
    old_lead, old_support = t.lead, {p.id for p in t.support}
    workflow.save(session, t, form, by)
    if not later:
        return []
    shift = t.date - old_date if t.date and old_date and t.date != old_date else None
    new_times = (t.start_time, t.end_time) if (t.start_time, t.end_time) != old_times else None
    new_lead = t.lead != old_lead
    new_support = {p.id for p in t.support} != old_support
    if not (shift or new_times or new_lead or new_support):
        return []
    reason = f"wie {label(t)}, dieser und alle folgenden"
    changed = []
    for other in later:
        allowed = workflow.editable(other)
        before = workflow.appointment_snapshot(other)
        if TIMES in allowed:
            if shift and other.date:
                other.date = other.date + shift
            if new_times:
                other.start_time, other.end_time = new_times
            other.duration_hours = workflow._duration(other)
        if TEAM in allowed:
            if new_lead:
                other.lead = t.lead
            if new_support or new_lead:
                support = list(t.support) if new_support else list(other.support)
                other.support = [p for p in support if p is not other.lead]
        try:
            workflow._required_for_stage(other)
        except FormError as error:
            raise FormError(f"Termin am {workflow._show(other.date)}: {error.messages[0]}") from error
        if workflow._record(
            session, "appointments", other.id, before, workflow.appointment_snapshot(other), by, reason
        ):
            changed.append(other)
    courses.renumber(session, t.course, by)
    hints.refresh(session, [a.id for a in t.course.appointments])
    return changed


# --- Bulk actions --------------------------------------------------------------------------------


@dataclass
class Bulk:
    """What a bulk action did: the Termine it changed and the ones it skipped, with why."""

    done: list[Appointment] = field(default_factory=list)
    skipped: list[tuple[Appointment, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)  # summaries of deleted Geplant Termine


def label(t: Appointment) -> str:
    """How a bulk message names a Termin."""
    return (
        f"Termin {t.number} ({workflow._show(t.date)})" if t.number else f"Termin am {workflow._show(t.date)}"
    )


def confirm_all(session: Session, course: Course, by: str) -> Bulk:
    """ "Alle bestätigen": every Geplant Termin that has what Bestätigt needs. Termine with
    alternative dates are confirmed one by one (the page asks which date)."""
    result = Bulk()
    for t in courses.in_order(course.appointments):
        if t.stage != GEPLANT:
            continue
        if t.alternatives:
            result.skipped.append((t, "hat Alternativdaten, bitte einzeln bestätigen"))
            continue
        if gaps := hints.missing(t, hints.CONFIRM_FIELDS):
            result.skipped.append((t, f"fehlt {', '.join(gaps)}"))
            continue
        workflow.confirm(session, t, None, by)
        result.done.append(t)
    if not result.done and not result.skipped:
        raise FormError("Kein geplanter Termin zum Bestätigen.")
    return result


def remaining(course: Course, today: dt.date) -> list[Appointment]:
    """The Termine still to come: in planning (Geplant, Bestätigt), dated today or later or undated."""
    return [
        t
        for t in courses.in_order(course.appointments)
        if t.stage in PLANNING and (t.date is None or t.date >= today)
    ]


def cancel_remaining(
    session: Session, course: Course, reason: str, by: str, today: dt.date, delete_planned: bool = False
) -> Bulk:
    """ "Restliche absagen": cancel every remaining Bestätigt Termin with the reason. Geplant ones are
    never cancelled (WM-90: they are deleted instead); with `delete_planned` they are deleted, which
    the Verlauf keeps, otherwise they are skipped."""
    reason = clean_name(reason)
    if not reason:
        raise FormError("Bitte einen Grund für die Absage angeben.")
    todo = remaining(course, today)
    if not todo:
        raise FormError("Es gibt keine kommenden Termine mehr.")
    result = Bulk()
    for t in todo:
        if t.stage == BESTAETIGT:
            workflow.cancel(session, t, reason, by)
            result.done.append(t)
        elif delete_planned:
            result.deleted.append(workflow.summary(t))
            workflow.log(session, t, None, None, reason, by, "Grund der Löschung")
            workflow.delete(session, t, by, really=True)
        else:
            result.skipped.append((t, "geplant: wird gelöscht, nicht abgesagt"))
    return result
