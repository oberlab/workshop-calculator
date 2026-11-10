"""Readers for the legacy CSV formats.

- Harmonised format (output of the old R workflow): semicolon-delimited, decimal comma,
  `NA` for missing values, unnamed index column, columns Datum, Kursname, Dauer, Ort,
  Schulart, Durchschnittsalter, Klasse, TN_F, TN_M, TN_Gesamt, Kurskategorie, Kontext,
  Terminnummer, Kursleitung.
- Slack List export: comma-delimited, column names as in the List (mapped through the
  instance config). Columns that are not mapped, e.g. billing, are ignored.
"""

import csv
from datetime import date
from pathlib import Path

from metrics.db import STATUS_DONE
from metrics.ingest.records import (
    ClassAge,
    SessionRecord,
    build_record,
    parse_class_age,
    to_decimal,
    to_int,
    to_text,
)
from metrics.instance import Instance

SOURCE_HARMONISED = "legacy-harmonised"
SOURCE_SLACK_EXPORT = "legacy-slack-export"
LEGACY_SOURCES = (SOURCE_HARMONISED, SOURCE_SLACK_EXPORT)
SLACK_ID_COLUMN = "Slack-ID"


def _date(value: str | None) -> date | None:
    value = (value or "").strip()
    return date.fromisoformat(value) if value and value != "NA" else None


def read_harmonised(path: Path, instance: Instance) -> list[SessionRecord]:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f, delimiter=";"))
    records = []
    for row in rows:
        context = to_text(row["Kontext"])
        records.append(
            build_record(
                instance,
                source=SOURCE_HARMONISED,
                source_key=f"{SOURCE_HARMONISED}:{path.name}:{row['']}",
                session_date=_date(row["Datum"]),
                course_name=to_text(row["Kursname"]),
                duration_hours=to_decimal(row["Dauer"]),
                location=to_text(row["Ort"]),
                school_type=to_text(row["Schulart"]),
                class_age_text=None,
                class_age=ClassAge(class_level=to_int(row["Klasse"]), age=to_int(row["Durchschnittsalter"])),
                participants_female=to_int(row["TN_F"]),
                participants_male=to_int(row["TN_M"]),
                participants_total=to_int(row["TN_Gesamt"]),
                category=to_text(row["Kurskategorie"]),
                context=context,
                session_number=to_int(row["Terminnummer"]),
                # The harmonised data only contains held sessions.
                status=STATUS_DONE,
                instructor=to_text(row["Kursleitung"]),
            )
        )
    return records


def read_slack_export(path: Path, instance: Instance) -> tuple[list[SessionRecord], int]:
    """Read a CSV export of the Slack List. Returns the records and the number of rows
    skipped because they have no date (planning placeholders)."""
    columns = instance.slack_columns
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = [name for name in columns.values() if name not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name}: columns not found: {missing}")
        rows = list(reader)

    records, skipped = [], 0
    for number, row in enumerate(rows, start=1):
        value = {key: row[name] for key, name in columns.items()}
        session_date = _date(value["date"])
        if session_date is None:
            skipped += 1
            continue
        # Names are stored as recorded; differences are flagged (metrics check-names), never mapped.
        course = to_text(value["course_name"])
        class_age_text = to_text(value["class_age"])
        records.append(
            build_record(
                instance,
                source=SOURCE_SLACK_EXPORT,
                # Archived exports carry the Slack item ID; older exports only the row number.
                source_key=(
                    f"{SOURCE_SLACK_EXPORT}:slack:{row[SLACK_ID_COLUMN]}"
                    if row.get(SLACK_ID_COLUMN)
                    else f"{SOURCE_SLACK_EXPORT}:{path.name}:{number}"
                ),
                session_date=session_date,
                course_name=course,
                duration_hours=to_decimal(value["duration_hours"]),
                location=to_text(value["location"]),
                school_type=None,
                class_age_text=class_age_text,
                class_age=parse_class_age(class_age_text),
                participants_female=to_int(value["participants_female"]),
                participants_male=to_int(value["participants_male"]),
                participants_total=None,
                category=to_text(value["category"]),
                context=to_text(value["context"]),
                session_number=to_int(value["session_number"]),
                status=to_text(value["status"]),
                instructor=to_text(value["instructor"]),
            )
        )
    return records, skipped
