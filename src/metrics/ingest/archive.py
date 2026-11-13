"""Quarterly hand-over from the Slack List to the legacy data.

1. `archive_list` saves the complete List (all items, all columns, schema) as raw JSON and
   writes the items up to a cut-off date as a CSV in the Slack export format: only the
   transferred columns, plus the Slack item ID and its last-edit timestamp.
2. The CSV is reviewed, normalised and loaded with `metrics load-legacy --slack-export`.
3. `delete_archived` deletes exactly the archived items from Slack (scope `lists:write`),
   skipping items that were edited after the archive was made.
"""

import csv
import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from metrics.ingest.legacy import SLACK_ID_COLUMN as ID_COLUMN
from metrics.ingest.slack import ListSnapshot, SlackError, _cell, _resolve_columns, fetch_list
from metrics.instance import Instance

UPDATED_COLUMN = "Slack-geändert"
DELETE_URL = "https://slack.com/api/slackLists.items.delete"


def write_raw(path: Path, list_id: str, stamp: str, snapshot: ListSnapshot) -> None:
    """The complete List (schema and all items) as fetched; `read_raw` reads it back."""
    document = {"list_id": list_id, "fetched_at": stamp, "schema": snapshot.schema, "items": snapshot.items}
    path.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")


def read_raw(path: Path) -> tuple[str, ListSnapshot]:
    """The List ID and the List of a raw JSON written by `write_raw`."""
    document = json.loads(path.read_text(encoding="utf-8"))
    return document["list_id"], ListSnapshot(schema=document["schema"], items=document["items"])


def _format_number(value: str | None) -> str:
    return value.replace(".", ",") if value else ""


def archive_list(
    token: str, list_id: str, until: date, out_dir: Path, instance: Instance
) -> tuple[Path, Path, int]:
    """Returns the paths of the raw JSON and the CSV, and the number of archived items."""
    snapshot = fetch_list(token, list_id)
    columns, _ = _resolve_columns(snapshot.schema, instance)
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)

    raw_path = out_dir / f"slack_{list_id}_{stamp}_raw.json"
    write_raw(raw_path, list_id, stamp, snapshot)

    header = [instance.slack_columns[key] for key in columns] + [ID_COLUMN, UPDATED_COLUMN]
    rows = []
    for item in snapshot.items:
        values = {key: _cell(item, column) for key, column in columns.items()}
        if not values["date"] or date.fromisoformat(values["date"][:10]) > until:
            continue
        values["duration_hours"] = _format_number(values["duration_hours"])
        row = [values[key] or "" for key in columns]
        rows.append((values["date"], [*row, item["id"], str(item.get("updated_timestamp", ""))]))

    csv_path = out_dir / f"Einsatztracking_bis_{until.isoformat()}.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(row for _, row in sorted(rows, key=lambda r: r[0]))
    return raw_path, csv_path, len(rows)


@dataclass
class DeleteResult:
    deleted: list[str] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)


def _post(token: str, url: str, params: dict) -> dict:
    request = urllib.request.Request(
        url, data=urllib.parse.urlencode(params).encode(), headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def delete_archived(token: str, list_id: str, csv_path: Path, log_path: Path) -> DeleteResult:
    with csv_path.open(newline="", encoding="utf-8") as f:
        archived = {row[ID_COLUMN]: row[UPDATED_COLUMN] for row in csv.DictReader(f)}
    current = {
        item["id"]: str(item.get("updated_timestamp", "")) for item in fetch_list(token, list_id).items
    }

    result = DeleteResult()
    with log_path.open("a", encoding="utf-8") as log:
        for item_id, updated in archived.items():
            if item_id not in current:
                result.skipped[item_id] = "no longer in the List"
            elif current[item_id] != updated:
                result.skipped[item_id] = "edited in Slack after the archive was made"
            else:
                while True:
                    response = _post(token, DELETE_URL, {"list_id": list_id, "id": item_id})
                    if response.get("error") != "ratelimited":
                        break
                    time.sleep(30)
                if not response.get("ok"):
                    raise SlackError(
                        f"Deleting {item_id} failed: {response.get('error')} {response.get('needed', '')}"
                    )
                result.deleted.append(item_id)
                time.sleep(3)  # rate tier 2: about 20 calls per minute
            status = "deleted" if item_id in result.deleted else f"skipped: {result.skipped[item_id]}"
            log.write(f"{datetime.now(UTC).isoformat()}\t{list_id}\t{item_id}\t{status}\n")
    return result
