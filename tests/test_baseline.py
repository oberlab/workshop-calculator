from pathlib import Path

from metrics.ingest.baseline import SOURCE_BASELINE, read_baseline, write_baseline
from metrics.ingest.legacy import read_harmonised, read_slack_export

FIXTURES = Path(__file__).parent / "fixtures"


def _comparable(record):
    row = record.as_row()
    row.pop("source")
    row["source_key"] = row["source_key"].removeprefix(f"{SOURCE_BASELINE}:")
    return row


def test_baseline_round_trip_gives_identical_records(instance, tmp_path):
    original = read_harmonised(FIXTURES / "harmonised.csv", instance)
    original += read_slack_export(FIXTURES / "slack_export.csv", instance)[0]
    path = tmp_path / "baseline.csv"
    write_baseline(original, path)
    loaded = read_baseline(path, instance)
    assert {r.source for r in loaded} == {SOURCE_BASELINE}
    by_key = {r["source_key"]: r for r in map(_comparable, loaded)}
    assert by_key == {r["source_key"]: r for r in map(_comparable, original)}


def test_baseline_file_is_deterministic(instance, tmp_path):
    records = read_harmonised(FIXTURES / "harmonised.csv", instance)
    first = write_baseline(records, tmp_path / "a.csv")
    second = write_baseline(list(reversed(records)), tmp_path / "b.csv")
    assert first == second  # same content, same checksum, whatever the input order


def test_read_baseline_rejects_other_files(instance):
    import pytest

    with pytest.raises(ValueError, match="not a baseline file"):
        read_baseline(FIXTURES / "slack_export.csv", instance)
