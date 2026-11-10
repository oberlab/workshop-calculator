"""The legacy baseline: all reviewed data up to a cut-off date in one CSV file.

`metrics export-baseline` writes it from the reviewed legacy files; `metrics load-baseline`
loads it once. After that the baseline is frozen: it is never replaced, and later legacy loads
(quarterly hand-overs) may only add entries dated after it.

The file holds the recorded values (UTF-8, comma-separated, dates ISO, decimals with a point).
Derived values (class level and age from the text, target group) are computed again on load,
exactly as for the original files, so the instance lookup tables still apply.
"""

import csv
import hashlib
from decimal import Decimal
from pathlib import Path

from metrics.ingest.legacy import _date
from metrics.ingest.records import ClassAge, SessionRecord, build_record, parse_class_age, to_int, to_text
from metrics.instance import Instance

SOURCE_BASELINE = "baseline"

COLUMNS = [
    "source_key",
    "date",
    "course_name",
    "duration_hours",
    "location",
    "school_type",
    "class_age_text",
    "class_level",
    "age",
    "participants_female",
    "participants_male",
    "participants_total",
    "category",
    "context",
    "session_number",
    "status",
    "instructor",
]


def _text(value) -> str:
    return "" if value is None else str(value)


def write_baseline(records: list[SessionRecord], path: Path) -> str:
    """Write the records in a fixed order; returns the SHA-256 of the file."""
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS, lineterminator="\n")
        writer.writeheader()
        for record in sorted(records, key=lambda r: (r.date.isoformat() if r.date else "", r.source_key)):
            writer.writerow(
                {
                    "source_key": record.source_key,
                    "date": record.date.isoformat() if record.date else "",
                    "course_name": _text(record.course_name),
                    "duration_hours": _text(record.duration_hours),
                    "location": _text(record.location),
                    "school_type": _text(record.school_type),
                    "class_age_text": _text(record.class_age_text),
                    "class_level": _text(record.class_level),
                    "age": _text(record.age),
                    "participants_female": _text(record.participants_female),
                    "participants_male": _text(record.participants_male),
                    "participants_total": _text(record.participants_total),
                    "category": _text(record.category),
                    "context": _text(record.context),
                    "session_number": _text(record.session_number),
                    "status": _text(record.status),
                    "instructor": _text(record.instructor),
                }
            )
    return checksum(path)


def checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_baseline(path: Path, instance: Instance) -> list[SessionRecord]:
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames != COLUMNS:
            raise ValueError(f"{path.name}: not a baseline file (columns {reader.fieldnames})")
        rows = list(reader)
    records = []
    for row in rows:
        text = to_text(row["class_age_text"])
        class_age = (
            parse_class_age(text)
            if text
            else ClassAge(class_level=to_int(row["class_level"]), age=to_int(row["age"]))
        )
        records.append(
            build_record(
                instance,
                source=SOURCE_BASELINE,
                source_key=f"{SOURCE_BASELINE}:{row['source_key']}",
                session_date=_date(row["date"]),
                course_name=to_text(row["course_name"]),
                duration_hours=Decimal(row["duration_hours"]) if row["duration_hours"] else None,
                location=to_text(row["location"]),
                school_type=to_text(row["school_type"]),
                class_age_text=text,
                class_age=class_age,
                participants_female=to_int(row["participants_female"]),
                participants_male=to_int(row["participants_male"]),
                participants_total=to_int(row["participants_total"]),
                category=to_text(row["category"]),
                context=to_text(row["context"]),
                session_number=to_int(row["session_number"]),
                status=to_text(row["status"]),
                instructor=to_text(row["instructor"]),
            )
        )
    return records
