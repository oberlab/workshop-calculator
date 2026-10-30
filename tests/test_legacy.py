from datetime import date
from decimal import Decimal

import pytest

from metrics.ingest import records as r
from metrics.ingest.legacy import read_harmonised, read_slack_export
from tests.conftest import FIXTURES


def test_read_harmonised(instance):
    rows = read_harmonised(FIXTURES / "harmonised.csv", instance)
    assert len(rows) == 5
    first = rows[0]
    assert first.date == date(2020, 1, 7)
    assert first.course_name is None
    assert first.duration_hours == Decimal("2")
    assert (first.class_level, first.age, first.participants_total) == (3, 8, 18)
    assert first.participants_female is None
    assert first.status == "Durchgeführt"
    assert first.target_group == r.SCHOOL_CLASS
    assert rows[1].duration_hours == Decimal("1.5")
    assert rows[2].target_group == r.CHILDREN_OUTSIDE_SCHOOL
    assert rows[3].target_group == r.ADULTS
    # Teacher-training context wins over the recorded class level.
    assert rows[4].target_group == r.TEACHERS
    assert len({row.source_key for row in rows}) == 5


def test_read_slack_export(instance):
    rows, skipped = read_slack_export(FIXTURES / "slack_export.csv", instance)
    assert skipped == 1
    assert len(rows) == 4
    loeten, repair, robotik, unknown = rows
    assert loeten.course_name == "Löten"  # alias from mappings/kursname.csv
    assert (loeten.class_level, loeten.age) == (3, 8)
    assert loeten.participants_total == 6
    assert loeten.duration_hours == Decimal("3.0")
    assert loeten.target_group == r.CHILDREN_OUTSIDE_SCHOOL
    assert repair.course_name == "Repair Café"
    assert repair.context == "Lehrkräftefortbildung"  # alias from mappings/kontext.csv
    assert repair.target_group == r.TEACHERS
    assert robotik.context is None
    assert robotik.target_group == r.SCHOOL_CLASS
    assert unknown.target_group == r.UNKNOWN
    assert unknown.warnings == ["unknown location: Neue Schule"]
    assert "Abgerechnet" not in loeten.as_row()


def test_read_slack_export_missing_column(instance, tmp_path):
    path = tmp_path / "broken.csv"
    path.write_text("Kursname,Datum\nA,2025-01-01\n", encoding="utf-8")
    with pytest.raises(ValueError, match="columns not found"):
        read_slack_export(path, instance)
