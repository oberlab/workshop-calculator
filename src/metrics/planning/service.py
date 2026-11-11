"""Planning workflow: saving with history, stage changes, flags and the final confirmation."""

import datetime as dt
import json
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import quote

from sqlalchemy import select
from sqlalchemy.orm import Session

from metrics.ingest.records import build_record, parse_class_age
from metrics.instance import Instance, clean_name
from metrics.planning import rules
from metrics.planning.conflicts import HIGH, LOW, MEDIUM, SEVERITY_ORDER, conflicts_for, is_active
from metrics.planning.models import (
    ABGESAGT,
    ABGESCHLOSSEN,
    BESTAETIGT,
    CAL_MISSING,
    CAL_NONE,
    CAL_OK,
    CAL_OUTDATED,
    DURCHGEFUEHRT,
    GEPLANT,
    LEAD,
    STAGES,
    SUPPORT,
    AlternativeDate,
    Course,
    CoursePerson,
    FinalSession,
    History,
    Municipality,
)
from metrics.planning.places import needs_address

SYSTEM = "System (automatisch)"
CLASS_KINDS = {
    "klasse": "Schulklasse",
    "lehrkraefte": "Lehrkräfte",
    "alter": "Durchschnittsalter (Kinder)",
    "band": "Altersgruppe (Erwachsene)",
}
AT_SCHOOL = ("klasse", "lehrkraefte")  # the only kinds offered when the place is a school
SOURCE = "planung"

# Fields per form section; which sections a stage may edit.
BASIC = [
    "date",
    "start_time",
    "end_time",
    "class_kind",
    "class_level",
    "age",
    "age_band",
    "context",
    "location",
    "course_name",
    "note",
]
DETAILS = ["category", "session_number"]
HELD = ["participants_female", "participants_male", "municipalities", "billed"]
# Requirements grow with the stage. Teilnehmende and Abrechnung only exist once the course
# took place (decided 2026-11-11); everything else can be filled in from the start.
EDITABLE = {
    GEPLANT: set(BASIC + DETAILS) | {"alternatives", "people"},
    BESTAETIGT: set(BASIC + DETAILS) | {"people"},
    DURCHGEFUEHRT: set(BASIC + DETAILS + HELD) | {"people"},
    ABGESCHLOSSEN: set(),
    ABGESAGT: set(),
}
LABELS = {
    "date": "Datum",
    "start_time": "Beginn",
    "end_time": "Ende",
    "class_kind": "Klasse/Alter (Art)",
    "class_level": "Klassenstufe",
    "age": "Alter",
    "age_band": "Altersgruppe",
    "context": "Kontext",
    "location": "Ort",
    "course_name": "Kursname",
    "note": "Notiz",
    "category": "Kurskategorie",
    "duration_hours": "Dauer",
    "session_number": "Terminnummer",
    "participants_female": "TN weiblich",
    "participants_male": "TN männlich",
    "billed": "Abgerechnet",
    "alternatives": "Alternativdaten",
    "lead": "Kursleitung",
    "support": "Unterstützung",
    "municipalities": "Herkunft",
}


class FormError(Exception):
    def __init__(self, messages: list[str]):
        super().__init__("; ".join(messages))
        self.messages = messages


# --- parsing the form ------------------------------------------------------------------------


def _one(form: dict, name: str) -> str:
    return clean_name((form.get(name) or [""])[0])


def _date(value: str, label: str, errors: list) -> dt.date | None:
    if not value:
        return None
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        errors.append(f"{label}: kein gültiges Datum")
        return None


def _time(value: str, label: str, errors: list) -> dt.time | None:
    if not value:
        return None
    try:
        return dt.time.fromisoformat(value)
    except ValueError:
        errors.append(f"{label}: keine gültige Uhrzeit")
        return None


def _int(value: str, label: str, errors: list) -> int | None:
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        errors.append(f"{label}: keine ganze Zahl")
        return None


def _display(course: Course, name: str) -> str:
    if name == "alternatives":
        return ", ".join(a.date.strftime("%d.%m.%Y") for a in course.alternatives)
    if name in (LEAD, SUPPORT):
        return ", ".join(sorted(course.keys(name)))
    if name == "municipalities":
        return "; ".join(f"{m.municipality} {m.female or 0}/{m.male or 0}" for m in course.municipalities)
    value = getattr(course, name)
    if value is None:
        return ""
    if isinstance(value, dt.date):
        return value.strftime("%d.%m.%Y")
    if isinstance(value, dt.time):
        return value.strftime("%H:%M")
    if isinstance(value, bool):
        return "ja" if value else "nein"
    return str(value)


TRACKED = BASIC + DETAILS + ["duration_hours", "billed", "alternatives", LEAD, SUPPORT, "municipalities"]


def snapshot(course: Course) -> dict[str, str]:
    return {name: _display(course, name) for name in TRACKED}


def apply_form(course: Course, form: dict, stage: str) -> None:
    """Copy the submitted values the course's stage may change; raise FormError on bad input."""
    errors: list[str] = []
    editable = EDITABLE[stage]
    if "date" in editable:
        course.date = _date(_one(form, "date"), "Datum", errors)
        course.start_time = _time(_one(form, "start_time"), "Beginn", errors)
        course.end_time = _time(_one(form, "end_time"), "Ende", errors)
        kind = _one(form, "class_kind") or None
        course.class_kind = kind if kind in CLASS_KINDS else None
        course.class_level = (
            _int(_one(form, "class_level"), "Klassenstufe", errors) if kind == "klasse" else None
        )
        # Separate inputs per kind, so a hidden one never overwrites the visible one.
        # Schulklasse: Klassenstufe only (decided 2026-11-11); children outside school: average age.
        course.age = _int(_one(form, "child_age"), "Alter", errors) if kind == "alter" else None
        course.age_band = (_one(form, "age_band") or None) if kind == "band" else None
        for name in ("context", "location", "course_name"):
            setattr(course, name, _one(form, name) or None)
        course.note = (form.get("note") or [""])[0].strip() or None
    if "category" in editable:
        course.category = _one(form, "category") or None
        course.session_number = _int(_one(form, "session_number"), "Terminnummer", errors)
    if "participants_female" in editable:
        course.participants_female = _int(_one(form, "participants_female"), "TN weiblich", errors)
        course.participants_male = _int(_one(form, "participants_male"), "TN männlich", errors)
    # Dauer is always end minus start: breaks are not recorded (decided 2026-11-11).
    if course.start_time and course.end_time and course.end_time > course.start_time:
        start = dt.datetime.combine(dt.date.min, course.start_time)
        course.duration_hours = round(
            (dt.datetime.combine(dt.date.min, course.end_time) - start).seconds / 3600, 2
        )
    else:
        course.duration_hours = None
    if "billed" in editable:
        course.billed = "billed" in form
    if "alternatives" in editable:
        dates = {
            d for v in form.get("alt_date", []) if (d := _date(clean_name(v), "Alternativdatum", errors))
        }
        dates.discard(course.date)
        course.alternatives = [AlternativeDate(date=d) for d in sorted(dates)]
    if "people" in editable:
        leads = [k for k in form.get("lead", []) if k][:1]  # one Kursleitung
        support = [k for k in dict.fromkeys(form.get("support", [])) if k and k not in leads]
        course.people = [CoursePerson(person_key=k, role=LEAD) for k in leads] + [
            CoursePerson(person_key=k, role=SUPPORT) for k in support
        ]
    if "municipalities" in editable and "mun_name" in form:
        rows = []
        for name, female, male in zip(
            form.get("mun_name", []), form.get("mun_female", []), form.get("mun_male", []), strict=False
        ):
            name = clean_name(name)
            if not name:
                continue
            rows.append(
                Municipality(
                    municipality=name,
                    female=_int(clean_name(female), f"Herkunft {name} w", errors),
                    male=_int(clean_name(male), f"Herkunft {name} m", errors),
                )
            )
        course.municipalities = rows
    if errors:
        raise FormError(errors)
    if stage in (BESTAETIGT, DURCHGEFUEHRT) and (gaps := rules.missing(course, rules.CONFIRM_FIELDS)):
        raise FormError([f"Ab „Bestätigt“ sind Pflicht: {', '.join(gaps)}"])


def record_changes(session: Session, course: Course, before: dict[str, str], user: str) -> None:
    after = snapshot(course)
    for name in TRACKED:
        if before.get(name, "") != after[name]:
            session.add(
                History(
                    course_id=course.id,
                    user=user,
                    action="geändert",
                    field=LABELS[name],
                    old=before.get(name) or None,
                    new=after[name] or None,
                )
            )


def log(
    session: Session, course_id: int, user: str, action: str, old: str | None = None, new: str | None = None
) -> None:
    session.add(History(course_id=course_id, user=user, action=action, old=old, new=new))


# --- stage changes ---------------------------------------------------------------------------


def _move(session: Session, course: Course, stage: str, user: str) -> None:
    log(session, course.id, user, "Phase", STAGES[course.stage], STAGES[stage])
    course.stage = stage


def confirm(session: Session, course: Course, chosen: dt.date | None, user: str) -> None:
    if course.stage != GEPLANT:
        raise FormError(["Nur geplante Kurse können bestätigt werden."])
    if course.alternatives:
        options = {course.date, *(a.date for a in course.alternatives)} - {None}
        if chosen not in options:
            raise FormError(["Bitte das Datum wählen, an dem der Kurs stattfindet."])
        if chosen != course.date:
            log(session, course.id, user, "geändert", _display(course, "date"), chosen.strftime("%d.%m.%Y"))
        dropped = ", ".join(d.strftime("%d.%m.%Y") for d in sorted(options - {chosen}))
        log(session, course.id, user, "Alternativdaten verworfen", dropped, None)
        course.date = chosen
        course.alternatives = []
    if gaps := rules.missing(course, rules.CONFIRM_FIELDS):
        raise FormError([f"Zum Bestätigen fehlt: {', '.join(gaps)}"])
    _move(session, course, BESTAETIGT, user)


def back_to_planned(session: Session, course: Course, user: str) -> None:
    if course.stage != BESTAETIGT:
        raise FormError(["Nur bestätigte Kurse können zurück auf „Geplant“."])
    _move(session, course, GEPLANT, user)


def mark_done(session: Session, course: Course, user: str) -> None:
    if course.stage not in (GEPLANT, BESTAETIGT):
        raise FormError(["Dieser Kurs kann nicht als durchgeführt markiert werden."])
    _move(session, course, DURCHGEFUEHRT, user)


def back_to_confirmed(session: Session, course: Course, user: str, today: dt.date) -> None:
    if course.stage != DURCHGEFUEHRT or not course.date or course.date < today:
        raise FormError(["Zurück auf „Bestätigt“ geht nur, solange das Datum nicht vorbei ist."])
    _move(session, course, BESTAETIGT, user)


def cancel(session: Session, course: Course, reason: str, user: str) -> None:
    if course.stage != BESTAETIGT:
        raise FormError(["Nur bestätigte Kurse können abgesagt werden; geplante Kurse werden gelöscht."])
    if not reason.strip():
        raise FormError(["Bitte einen Grund für die Absage angeben."])
    course.cancel_reason = reason.strip()
    log(session, course.id, user, "Absagegrund", None, course.cancel_reason)
    _move(session, course, ABGESAGT, user)


def delete(session: Session, course: Course, user: str) -> None:
    if course.stage != GEPLANT:
        raise FormError(["Nur geplante Kurse können gelöscht werden."])
    summary = f"{course.title}, {_display(course, 'date') or 'ohne Datum'}, {course.location or 'ohne Ort'}"
    log(session, course.id, user, "gelöscht", summary, None)
    session.delete(course)


def finalize(
    session: Session, course: Course, instance: Instance, vocabulary: dict, user: str, checked: bool
) -> FinalSession:
    """Write the session the metrics database would get (prototype: into its own table)."""
    if course.stage != DURCHGEFUEHRT:
        raise FormError(["Nur durchgeführte Kurse können final bestätigt werden."])
    problems = rules.final_problems(course, instance, vocabulary)
    blocking = [p.text for p in problems if p.blocking]
    if blocking:
        raise FormError(blocking)
    if any(not p.blocking for p in problems) and not checked:
        raise FormError(["Bitte die Hinweise prüfen und „Hinweise geprüft“ ankreuzen."])
    text = course.class_age_text
    record = build_record(
        instance,
        source=SOURCE,
        source_key=f"{SOURCE}:{course.id}",
        session_date=course.date,
        course_name=course.course_name,
        duration_hours=Decimal(str(course.duration_hours)),
        location=course.location,
        school_type=None,
        class_age_text=text,
        class_age=parse_class_age(text),
        participants_female=course.participants_female,
        participants_male=course.participants_male,
        participants_total=None,
        category=course.category,
        context=course.context,
        session_number=course.session_number,
        status="Durchgeführt",
        instructor=course.keys(LEAD)[0],
    )
    row = record.as_row()
    row["participant_municipalities"] = [
        {"municipality": m.municipality, "female": m.female, "male": m.male} for m in course.municipalities
    ]
    final = FinalSession(
        course_id=course.id,
        source_key=record.source_key,
        date=course.date,
        course_name=course.course_name,
        context=course.context,
        location=course.location,
        target_group=record.target_group,
        participants_total=record.participants_total,
        record_json=json.dumps(row, default=str, ensure_ascii=False, indent=2),
        confirmed_by=user,
    )
    session.add(final)
    if checked and any(not p.blocking for p in problems):
        log(session, course.id, user, "Hinweise geprüft", None, "; ".join(p.text for p in problems))
    course.finalized_by, course.finalized_at = user, dt.datetime.now(dt.UTC)
    _move(session, course, ABGESCHLOSSEN, user)
    return final


def advance(session: Session, today: dt.date) -> int:
    """Confirmed courses whose date has passed move to Durchgeführt."""
    moved = 0
    for course in session.scalars(select(Course).where(Course.stage == BESTAETIGT, Course.date < today)):
        _move(session, course, DURCHGEFUEHRT, SYSTEM)
        moved += 1
    return moved


# --- flags -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class Flag:
    severity: str
    text: str
    link: int | None = None  # other course
    href: str | None = None  # page where the flag is resolved


def flags(
    course: Course,
    others: list[Course],
    names: dict[str, str],
    instance: Instance,
    vocabulary: dict,
    today: dt.date,
    calendar_on: bool,
) -> list[Flag]:
    found: list[Flag] = []
    if course.stage == GEPLANT and course.date and course.date < today:
        found.append(Flag(HIGH, "Datum vorbei, nie bestätigt: durchgeführt oder löschen?"))
    if course.stage == DURCHGEFUEHRT:
        for problem in rules.final_problems(course, instance, vocabulary):
            found.append(Flag(HIGH if problem.blocking else LOW, problem.text))
    if is_active(course, today) and (not course.date or course.date >= today):
        active = [o for o in others if is_active(o, today)]
        for c in conflicts_for(course, active, names):
            prefix = f"{c.date:%d.%m.} Alternativtermin: " if c.alternative else ""
            found.append(Flag(c.severity, f"{prefix}{c.reason} mit „{c.other_title}“", c.other_id))
    if course.stage not in (ABGESCHLOSSEN, ABGESAGT) and needs_address(instance, course.location):
        found.append(
            Flag(
                MEDIUM,
                f"Neuer Ort „{course.location}“: Adresse fehlt (Straße, PLZ, Ort)",
                href=f"/planung/orte?ort={quote(course.location)}",
            )
        )
    if calendar_on and course.stage in (GEPLANT, BESTAETIGT):
        if course.calendar_state == CAL_MISSING:
            found.append(Flag(MEDIUM, "Kalendereintrag fehlt (gelöscht?)"))
        elif course.calendar_state == CAL_OUTDATED:
            found.append(Flag(MEDIUM, "Kalendereintrag weicht vom Kurs ab"))
        elif course.calendar_state == CAL_NONE and course.stage == BESTAETIGT:
            found.append(Flag(LOW, "Noch nicht im Kalender"))
    if calendar_on and course.stage == ABGESAGT and course.calendar_state == CAL_OK:
        found.append(Flag(LOW, "Abgesagt, steht aber noch im Kalender"))
    return sorted(found, key=lambda f: SEVERITY_ORDER[f.severity])


def top_severity(found: list[Flag]) -> str | None:
    return found[0].severity if found else None
