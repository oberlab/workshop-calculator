"""Read-only sync of a Slack List (scope `lists:read`).

Each sync fetches all items (full refresh). Columns are matched by name through the
instance config; columns that are neither mapped nor listed as ignored are reported.
"""

import json
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date

from metrics.ingest.records import SessionRecord, build_record, parse_class_age, to_decimal, to_int, to_text
from metrics.instance import Instance, clean_name

API_URL = "https://slack.com/api/slackLists.items.list"
SOURCE_SLACK = "slack"


class SlackError(RuntimeError):
    pass


@dataclass
class ListSnapshot:
    schema: list[dict]
    items: list[dict]


@dataclass
class MappedItems:
    records: list[SessionRecord]
    unmapped_columns: list[str] = field(default_factory=list)
    skipped_without_date: int = 0


def _call(token: str, params: dict) -> dict:
    request = urllib.request.Request(
        API_URL,
        data=urllib.parse.urlencode(params).encode(),
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def fetch_list(token: str, list_id: str) -> ListSnapshot:
    items, schema, cursor = [], None, None
    while True:
        params = {"list_id": list_id, "limit": 100}
        if cursor:
            params["cursor"] = cursor
        if schema is None:
            params["include_list"] = "true"
        result = _call(token, params)
        if not result.get("ok"):
            if result.get("error") == "ratelimited":
                time.sleep(30)
                continue
            raise SlackError(f"Slack API error: {result.get('error')}")
        if schema is None:
            schema = result["list"]["list_metadata"]["schema"]
        items.extend(result.get("items", []))
        cursor = result.get("response_metadata", {}).get("next_cursor")
        if not cursor:
            return ListSnapshot(schema=schema, items=items)


def _resolve_columns(schema: list[dict], instance: Instance) -> tuple[dict[str, dict], list[str]]:
    """Map our field names to schema columns; return also the unmapped column names."""
    by_name = {column["name"]: column for column in schema}
    by_clean_name = {clean_name(column["name"]): column for column in schema}
    resolved = {}
    for key, name in instance.slack_columns.items():
        column = by_name.get(name) or by_clean_name.get(clean_name(name))
        if column is None:
            raise SlackError(f"Column {name!r} (for {key}) not found in the Slack List")
        resolved[key] = column
    used = {column["id"] for column in resolved.values()}
    ignored = {clean_name(name) for name in instance.slack_ignored_columns}
    unmapped = sorted(
        {
            column["name"]
            for column in schema
            if column["id"] not in used and clean_name(column["name"]) not in ignored
        }
    )
    return resolved, unmapped


def _cell(item: dict, column: dict) -> str | None:
    """The cell value of an item as text (select choices as their labels)."""
    for f in item.get("fields", []):
        if f.get("column_id") != column["id"]:
            continue
        kind = column["type"]
        if kind == "select":
            labels = {c["value"]: c["label"] for c in (column.get("options") or {}).get("choices", [])}
            values = [labels.get(v, v) for v in f.get("select") or []]
            return ", ".join(values) or None
        if kind in ("number", "date", "user"):
            values = f.get(kind) or []
            return str(values[0]) if values else None
        if kind == "text":
            return f.get("text") or None
        value = f.get("value")
        return str(value) if value not in (None, "") else None
    return None


def map_items(snapshot: ListSnapshot, instance: Instance) -> MappedItems:
    columns, unmapped = _resolve_columns(snapshot.schema, instance)
    mapped = MappedItems(records=[], unmapped_columns=unmapped)
    for item in snapshot.items:
        value = {key: _cell(item, column) for key, column in columns.items()}
        if not value["date"]:
            mapped.skipped_without_date += 1
            continue
        # Names are stored as recorded; differences are flagged (metrics check-names), never mapped.
        course = to_text(value["course_name"])
        class_age_text = to_text(value["class_age"])
        mapped.records.append(
            build_record(
                instance,
                source=SOURCE_SLACK,
                source_key=f"{SOURCE_SLACK}:{item['id']}",
                session_date=date.fromisoformat(value["date"][:10]),
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
    return mapped
