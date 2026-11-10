import csv
from datetime import date

from metrics.ingest import archive
from metrics.ingest.legacy import read_slack_export
from metrics.ingest.slack import ListSnapshot
from tests.test_slack import SCHEMA, item


def snapshot():
    items = [
        item(
            "R1",
            C1=("text", "Loeten"),
            C2=("date", "2026-02-03"),
            C3=("number", 1.5),
            C4=("select", "o1"),
            C5=("select", "k1"),
            C6=("number", 10),
            C7=("number", 12),
            C11=("select", "s1"),
            C13=("checkbox", True),
        ),
        item("R2", C1=("text", "Später"), C2=("date", "2026-10-05"), C11=("select", "s2")),
        item("R3", C1=("text", "ohne Datum")),
    ]
    for i, it in enumerate(items):
        it["updated_timestamp"] = f"170000000{i}"
    return ListSnapshot(SCHEMA, items)


def test_archive_list_writes_raw_json_and_csv(instance, tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "fetch_list", lambda token, list_id: snapshot())
    raw, csv_path, count = archive.archive_list("t", "F1", date(2026, 9, 30), tmp_path, instance)
    assert count == 1
    assert '"R3"' in raw.read_text(encoding="utf-8")  # the raw backup keeps every item
    with csv_path.open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [(r["Slack-ID"], r["Slack-geändert"], r["Dauer (h)"]) for r in rows] == [
        ("R1", "1700000000", "1,5")
    ]
    assert "Abgerechnet" not in rows[0]  # only transferred columns
    records, skipped = read_slack_export(csv_path, instance)
    assert skipped == 0
    assert records[0].source_key == "legacy-slack-export:slack:R1"
    assert records[0].course_name == "Loeten"  # as recorded


def test_delete_archived_skips_edited_and_missing_items(tmp_path, monkeypatch):
    csv_path = tmp_path / "a.csv"
    csv_path.write_text("Slack-ID,Slack-geändert\nR1,1\nR2,2\nR3,3\n", encoding="utf-8")
    current = ListSnapshot(
        [],
        [
            {"id": "R1", "updated_timestamp": "1"},
            {"id": "R2", "updated_timestamp": "9"},
            {"id": "R4", "updated_timestamp": "4"},
        ],
    )
    calls = []
    monkeypatch.setattr(archive, "fetch_list", lambda token, list_id: current)
    monkeypatch.setattr(
        archive, "_post", lambda token, url, params: calls.append(params["id"]) or {"ok": True}
    )
    monkeypatch.setattr(archive.time, "sleep", lambda seconds: None)
    result = archive.delete_archived("t", "F1", csv_path, tmp_path / "log")
    assert calls == ["R1"]  # R4 is not in the archive and is never touched
    assert result.deleted == ["R1"]
    assert result.skipped == {
        "R2": "edited in Slack after the archive was made",
        "R3": "no longer in the List",
    }
    assert len((tmp_path / "log").read_text().splitlines()) == 3
