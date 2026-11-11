"""The common session record and the rules shared by all sources."""

import re
from dataclasses import asdict, dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation

from metrics.instance import Instance, clean_name

SCHOOL_CLASS = "Schulklasse"
CHILDREN_OUTSIDE_SCHOOL = "Kinder & Jugendliche außerschulisch"
ADULTS = "Erwachsene"
TEACHERS = "Lehrkräfte"
UNKNOWN = "Unbekannt"
TARGET_GROUPS = [SCHOOL_CLASS, CHILDREN_OUTSIDE_SCHOOL, ADULTS, TEACHERS, UNKNOWN]

ADULT_MIN_AGE = 19

_CLASS_AGE = re.compile(r"^Kl\.\s*(\d+)\s*/\s*(\d+)\s*Jahre$")
_AGE_RANGE = re.compile(r"^(\d+)\s*-\s*(\d+)\s*Jahre$")
_AGE_OVER = re.compile(r"^über\s*(\d+)\s*Jahre$")
_AGE_ONLY = re.compile(r"^(\d+)\s*Jahre$")  # average age without a class (planning: children outside school)


@dataclass
class ClassAge:
    class_level: int | None = None
    age: int | None = None
    age_band: str | None = None
    teachers: bool = False
    parsed: bool = True


def parse_class_age(text: str | None) -> ClassAge:
    """Parse the Slack 'Klassenstufe/Durchschnittsalter' value.

    'Kl. 3 / 8 Jahre' -> class 3, age 8; '12 Jahre' -> age 12 (no class);
    '19 - 24 Jahre' and 'über 65 Jahre' -> age band; 'Lehrkräfte' -> teachers;
    empty -> nothing; anything else -> parsed=False.
    """
    value = clean_name(text)
    if not value:
        return ClassAge()
    if value == TEACHERS:
        return ClassAge(teachers=True)
    if match := _CLASS_AGE.match(value):
        return ClassAge(class_level=int(match[1]), age=int(match[2]))
    if match := _AGE_ONLY.match(value):
        return ClassAge(age=int(match[1]))
    if _AGE_RANGE.match(value) or _AGE_OVER.match(value):
        return ClassAge(age_band=value)
    return ClassAge(parsed=False)


def band_lower_age(band: str) -> int | None:
    if match := _AGE_RANGE.match(band):
        return int(match[1])
    if match := _AGE_OVER.match(band):
        return int(match[1]) + 1
    return None


def target_group(
    *,
    context: str | None,
    class_age: ClassAge,
    location_is_school: bool | None,
    teacher_contexts: frozenset[str],
) -> str:
    """Assign the target group (decided 2026-10-30: age + location).

    Teacher contexts and the 'Lehrkräfte' label -> Lehrkräfte. Age from 19 or an adult age
    band -> Erwachsene. Children (class level or age up to 18) -> Schulklasse at a school,
    außerschulisch elsewhere, Unbekannt if the location is empty or not in the location list.
    """
    if (context and context in teacher_contexts) or class_age.teachers:
        return TEACHERS
    if class_age.age_band:
        lower = band_lower_age(class_age.age_band)
        return ADULTS if lower is not None and lower >= ADULT_MIN_AGE else UNKNOWN
    is_child = class_age.class_level is not None or (
        class_age.age is not None and class_age.age < ADULT_MIN_AGE
    )
    if not is_child:
        return ADULTS if class_age.age is not None else UNKNOWN
    if location_is_school is None:
        return UNKNOWN
    return SCHOOL_CLASS if location_is_school else CHILDREN_OUTSIDE_SCHOOL


def to_int(value: str | int | float | None) -> int | None:
    if value is None or value == "" or value == "NA":
        return None
    return int(float(str(value).replace(",", ".")))


def to_decimal(value: str | int | float | None) -> Decimal | None:
    if value is None or value == "" or value == "NA":
        return None
    try:
        return Decimal(str(value).replace(",", "."))
    except InvalidOperation as error:
        raise ValueError(f"Not a number: {value!r}") from error


def to_text(value: str | None) -> str | None:
    cleaned = clean_name(value)
    return None if cleaned in ("", "NA") else cleaned


@dataclass
class SessionRecord:
    source: str
    source_key: str
    date: date | None
    course_name: str | None
    duration_hours: Decimal | None
    location: str | None
    school_type: str | None
    class_age_text: str | None
    class_level: int | None
    age: int | None
    age_band: str | None
    target_group: str
    participants_female: int | None
    participants_male: int | None
    participants_total: int | None
    category: str | None
    context: str | None
    session_number: int | None
    status: str | None
    instructor: str | None
    warnings: list[str] = field(default_factory=list, repr=False)

    def as_row(self) -> dict:
        row = asdict(self)
        row.pop("warnings")
        return row


def build_record(
    instance: Instance,
    *,
    source: str,
    source_key: str,
    session_date: date | None,
    course_name: str | None,
    duration_hours: Decimal | None,
    location: str | None,
    school_type: str | None,
    class_age_text: str | None,
    class_age: ClassAge,
    participants_female: int | None,
    participants_male: int | None,
    participants_total: int | None,
    category: str | None,
    context: str | None,
    session_number: int | None,
    status: str | None,
    instructor: str | None,
) -> SessionRecord:
    warnings = []
    location_is_school = instance.location_is_school(location)
    if location and location_is_school is None:
        warnings.append(f"unknown location: {location}")
    if not class_age.parsed:
        warnings.append(f"unreadable class/age: {class_age_text}")
    if participants_total is None and participants_female is not None and participants_male is not None:
        participants_total = participants_female + participants_male
    return SessionRecord(
        source=source,
        source_key=source_key,
        date=session_date,
        course_name=course_name,
        duration_hours=duration_hours,
        location=location,
        school_type=school_type,
        class_age_text=class_age_text,
        class_level=class_age.class_level,
        age=class_age.age,
        age_band=class_age.age_band,
        target_group=target_group(
            context=context,
            class_age=class_age,
            location_is_school=location_is_school,
            teacher_contexts=instance.teacher_contexts,
        ),
        participants_female=participants_female,
        participants_male=participants_male,
        participants_total=participants_total,
        category=category,
        context=context,
        session_number=session_number,
        status=status,
        instructor=instructor,
        warnings=warnings,
    )
