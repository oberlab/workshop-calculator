"""What a course needs in each stage, and the checks before the final confirmation.

Missing fields and blocking rules keep a course from moving on; warnings are shown and must be
marked as checked before the final confirmation. The vocabulary for the spelling warnings
(earlier course names, contexts, places) comes from `metrics.planning.vocabulary`.
"""

from dataclasses import dataclass

from metrics.ingest.records import ADULT_MIN_AGE
from metrics.instance import Instance
from metrics.names_check import suggestion
from metrics.planning.models import Course

CONFIRM_FIELDS = {
    "date": "Datum",
    "start_time": "Beginn",
    "end_time": "Ende",
    "context": "Kontext",
    "class_age": "Klasse/Alter",
    "location": "Ort",
}
FINAL_FIELDS = {
    **CONFIRM_FIELDS,
    "course_name": "Kursname",
    "category": "Kurskategorie",
    "duration_hours": "Dauer",
    "session_number": "Terminnummer",
    "participants_female": "TN weiblich",
    "participants_male": "TN männlich",
    "lead": "Kursleitung",
}

AGE_BANDS = ["19 - 24 Jahre", "25 - 34 Jahre", "35 - 49 Jahre", "50 - 64 Jahre", "über 65 Jahre"]


@dataclass(frozen=True)
class Problem:
    text: str
    blocking: bool = True


def _filled(course: Course, name: str) -> bool:
    if name == "class_age":
        return course.has_class_age
    if name == "lead":
        return bool(course.keys("lead"))
    value = getattr(course, name)
    return value is not None and value != ""


def missing(course: Course, fields: dict[str, str]) -> list[str]:
    return [label for name, label in fields.items() if not _filled(course, name)]


def wants_municipalities(course: Course, instance: Instance) -> bool:
    return any(p.participant_municipalities and course.context in p.contexts for p in instance.projects)


def final_problems(course: Course, instance: Instance, vocabulary: dict[str, set[str]]) -> list[Problem]:
    """Everything that stands between a held course and its final confirmation."""
    problems = [Problem(f"Fehlt: {', '.join(m)}") for m in [missing(course, FINAL_FIELDS)] if m]
    if course.class_kind == "klasse" and course.class_level is not None and course.age is None:
        problems.append(Problem("Bei einer Schulklasse fehlt das Durchschnittsalter"))
    if course.class_kind == "alter" and course.age is not None and course.age >= ADULT_MIN_AGE:
        problems.append(Problem("Durchschnittsalter (Kinder) ist 19 oder mehr: Altersgruppe wählen"))
    if course.start_time and course.end_time and course.end_time <= course.start_time:
        problems.append(Problem("Ende liegt vor dem Beginn"))
    if course.duration_hours is not None and not 0.25 <= course.duration_hours <= 12:
        problems.append(Problem("Dauer muss zwischen 0,25 und 12 Stunden liegen"))
    female, male = course.participants_female, course.participants_male
    if female is not None and male is not None and female + male <= 0:
        problems.append(Problem("Keine Teilnehmenden eingetragen"))
    if course.session_number is not None and course.session_number < 1:
        problems.append(Problem("Terminnummer muss mindestens 1 sein"))
    if wants_municipalities(course, instance):
        rows = course.municipalities
        if not rows:
            problems.append(Problem("Herkunft der Teilnehmenden fehlt"))
        else:
            f = sum(r.female or 0 for r in rows)
            m = sum(r.male or 0 for r in rows)
            if (f, m) != (female or 0, male or 0):
                problems.append(
                    Problem(
                        f"Herkunft ergibt {f} w / {m} m, eingetragen sind {female or 0} w / {male or 0} m"
                    )
                )
    problems += warnings(course, instance, vocabulary)
    return problems


def warnings(course: Course, instance: Instance, vocabulary: dict[str, set[str]]) -> list[Problem]:
    """New spellings and unknown places: allowed, but someone should look at them."""
    found = []
    for name, label in (("course_name", "Kursname"), ("context", "Kontext"), ("location", "Ort")):
        value = getattr(course, name)
        known = vocabulary.get(name, set())
        if value and known and value not in known:
            hint = suggestion(value, known, {})
            text = f"Neuer {label} „{value}“" + (f", ähnlich: „{hint}“" if hint else "")
            found.append(Problem(text, blocking=False))
    if (
        course.location
        and instance.location_is_school(course.location)
        and course.class_kind in ("alter", "band")
    ):
        found.append(
            Problem(
                f"„{course.location}“ ist eine Schule: Schulklasse oder Lehrkräfte wählen", blocking=False
            )
        )
    # A place with an address but without the school question: the target group stays unknown.
    # (Places without an address get their own flag, see metrics.planning.places.)
    if (
        course.location
        and course.location in instance.location_addresses
        and instance.location_is_school(course.location) is None
    ):
        found.append(
            Problem(f"Ort „{course.location}“: Schule ja/nein fehlt (Zielgruppe unbekannt)", blocking=False)
        )
    return found
