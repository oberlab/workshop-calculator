#!/usr/bin/env python3
"""Probe read access to a Slack List and summarise its structure.

Usage:
    SLACK_TOKEN=xoxb-... python3 scripts/slack_lists_probe.py <LIST_ID>

The list ID is the `F...` part of the List's link ("Copy link to list").
The token needs the `lists:read` scope, and the app must have access to the List
(share the List with the app, or use a user token of someone who can see it).

Prints only structure (column ids, value types, fill counts), never cell values.
The raw API responses are written to `.probe/` (gitignored) for inspection.
"""

import json
import os
import sys
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

API_URL = "https://slack.com/api/slackLists.items.list"
VALUE_TYPES = (
    "text", "rich_text", "number", "select", "date", "user", "channel",
    "attachment", "checkbox", "email", "phone", "rating", "timestamp",
    "link", "message", "reference",
)
OUT_DIR = Path(".probe")


def call(token, list_id, cursor=None, include_list=False):
    params = {"list_id": list_id, "limit": 100}
    if cursor:
        params["cursor"] = cursor
    if include_list:
        params["include_list"] = "true"
    request = urllib.request.Request(
        API_URL,
        data=urllib.parse.urlencode(params).encode(),
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def find_columns(list_object):
    """Column definitions from the list's schema (`list_metadata.schema`)."""
    schema = (list_object.get("list_metadata") or {}).get("schema") or []
    return {column["id"]: column for column in schema if "id" in column}


def main():
    token = os.environ.get("SLACK_TOKEN")
    if not token or len(sys.argv) != 2:
        sys.exit(__doc__)
    list_id = sys.argv[1]
    OUT_DIR.mkdir(exist_ok=True)

    items, cursor, page, list_object = [], None, 0, None
    while True:
        result = call(token, list_id, cursor, include_list=(page == 0))
        (OUT_DIR / f"page_{page:03d}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False))
        if not result.get("ok"):
            print(f"API error: {result.get('error')}")
            if result.get("needed"):
                print(f"Missing scope: {result['needed']}")
            sys.exit(1)
        if page == 0:
            list_object = result.get("list")
        items.extend(result.get("items", []))
        cursor = result.get("response_metadata", {}).get("next_cursor")
        page += 1
        if not cursor:
            break

    print(f"Access OK: {len(items)} items in {page} page(s)")
    archived = sum(1 for item in items if item.get("archived"))
    if archived:
        print(f"  of which archived: {archived}")

    columns = find_columns(list_object) if list_object else {}
    if list_object is None:
        print("No list object returned (include_list); column names unknown.")

    types_seen = defaultdict(Counter)
    filled = Counter()
    keys = {}
    for item in items:
        for field in item.get("fields", []):
            column_id = field.get("column_id") or field.get("key")
            keys.setdefault(column_id, field.get("key"))
            present = [t for t in VALUE_TYPES if field.get(t)]
            types_seen[column_id].update(present or ["(empty)"])
            if present:
                filled[column_id] += 1

    print(f"\n{'column_id':<16} {'name / key':<36} {'filled':>7}  value types")
    for column_id in sorted(set(types_seen) | set(columns)):
        meta = columns.get(column_id, {})
        label = meta.get("name") or keys.get(column_id) or ""
        if meta.get("type"):
            label = f"{label} [{meta['type']}]"
        types = ", ".join(f"{t}×{n}" for t, n in types_seen[column_id].most_common())
        print(f"{column_id:<16} {label[:36]:<36} {filled[column_id]:>7}  {types}")

    print(f"\nRaw responses written to {OUT_DIR.resolve()}/")


if __name__ == "__main__":
    main()
