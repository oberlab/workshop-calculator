"""Command line: `metrics load-legacy …` and `metrics sync-slack`."""

import argparse
import sys
from collections import Counter
from pathlib import Path

from metrics.config import get_settings
from metrics.db import get_sessionmaker
from metrics.ingest import store
from metrics.ingest.legacy import LEGACY_SOURCES, read_harmonised, read_slack_export
from metrics.ingest.records import SessionRecord
from metrics.ingest.slack import SOURCE_SLACK, fetch_list, map_items
from metrics.instance import get_instance


def _print_warnings(records: list[SessionRecord]) -> None:
    warnings = Counter(w for record in records for w in record.warnings)
    if warnings:
        print("\nWarnings (rows affected):")
        for warning, count in sorted(warnings.items()):
            print(f"  {warning} ({count})")


def _print_summary(sources: tuple[str, ...]) -> None:
    with get_sessionmaker()() as session:
        result = store.summary(session, sources)
    print("\nSessions per year and status:")
    for (year, status), count in sorted(
        result["per_year_and_status"].items(), key=lambda x: (x[0][0] or 0, x[0][1] or "")
    ):
        print(f"  {year}  {status or '-':<14} {count}")
    print("Sessions per target group:")
    for group, count in sorted(result["per_target_group"].items()):
        print(f"  {group:<36} {count}")


def load_legacy(args: argparse.Namespace) -> None:
    instance = get_instance()
    records: list[SessionRecord] = []
    for path in args.harmonised:
        loaded = read_harmonised(Path(path), instance)
        print(f"{path}: {len(loaded)} rows (harmonised format)")
        records += loaded
    for path in args.slack_export:
        loaded, skipped = read_slack_export(Path(path), instance)
        print(f"{path}: {len(loaded)} rows (Slack export format), {skipped} without date skipped")
        records += loaded
    with get_sessionmaker()() as session, session.begin():
        store.replace_legacy(session, records)
    print(f"Replaced all legacy rows with {len(records)} rows.")
    _print_warnings(records)
    _print_summary(LEGACY_SOURCES)


def sync_slack(args: argparse.Namespace) -> None:
    settings = get_settings()
    if not settings.slack_bot_token or not settings.slack_list_id:
        sys.exit("SLACK_BOT_TOKEN and SLACK_LIST_ID must be set.")
    snapshot = fetch_list(settings.slack_bot_token, settings.slack_list_id)
    mapped = map_items(snapshot, get_instance())
    with get_sessionmaker()() as session, session.begin():
        removed = store.upsert_slack(session, mapped.records)
    print(
        f"{len(snapshot.items)} items fetched, {len(mapped.records)} synced, "
        f"{mapped.skipped_without_date} without date skipped, {removed} newly marked as removed from Slack."
    )
    if mapped.unmapped_columns:
        names = ", ".join(c.replace("\n", " ") for c in mapped.unmapped_columns)
        print(f"Columns not transferred (not mapped): {names}")
    _print_warnings(mapped.records)
    _print_summary((SOURCE_SLACK,))


def main() -> None:
    parser = argparse.ArgumentParser(prog="metrics")
    commands = parser.add_subparsers(required=True)

    legacy = commands.add_parser("load-legacy", help="Replace all legacy rows with the given CSV files")
    legacy.add_argument(
        "--harmonised", action="append", default=[], help="CSV in the harmonised legacy format"
    )
    legacy.add_argument("--slack-export", action="append", default=[], help="CSV export of the Slack List")
    legacy.set_defaults(func=load_legacy)

    slack = commands.add_parser("sync-slack", help="Read the Slack List and upsert its items")
    slack.set_defaults(func=sync_slack)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
