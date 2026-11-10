"""Command line: `metrics load-legacy`, `sync-slack`, `sync-instructors`, `archive-slack`, `delete-slack`,
`hash-password`, `check-locations`,
`check-names`."""

import argparse
import getpass
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from metrics.auth import hash_password
from metrics.config import get_settings
from metrics.db import STATUS_DONE, SessionRow, get_sessionmaker
from metrics.ingest import store
from metrics.ingest.archive import archive_list, delete_archived
from metrics.ingest.instructors import sync_instructors
from metrics.ingest.legacy import LEGACY_SOURCES, read_harmonised, read_slack_export
from metrics.ingest.records import SessionRecord
from metrics.ingest.slack import SOURCE_SLACK, fetch_list, map_items
from metrics.instance import get_instance
from metrics.locations_check import check_locations, format_report
from metrics.names_check import FIELDS, check_names
from metrics.names_check import format_report as format_names_report


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


def _slack_credentials() -> tuple[str, str]:
    settings = get_settings()
    if not settings.slack_bot_token or not settings.slack_list_id:
        sys.exit("SLACK_BOT_TOKEN and SLACK_LIST_ID must be set.")
    return settings.slack_bot_token, settings.slack_list_id


def archive_slack(args: argparse.Namespace) -> None:
    token, list_id = _slack_credentials()
    raw, csv_path, count = archive_list(token, list_id, args.until, Path(args.out), get_instance())
    print(f"Complete List saved to {raw}")
    print(f"{count} items dated up to {args.until} written to {csv_path}")


def delete_slack(args: argparse.Namespace) -> None:
    token, list_id = _slack_credentials()
    csv_path = Path(args.csv)
    result = delete_archived(token, list_id, csv_path, csv_path.with_suffix(".deleted.log"))
    print(f"{len(result.deleted)} items deleted from the Slack List, {len(result.skipped)} skipped.")
    for item_id, reason in result.skipped.items():
        print(f"  skipped {item_id}: {reason}")


def sync_instructor_profiles(args: argparse.Namespace) -> None:
    token, _ = _slack_credentials()
    with get_sessionmaker()() as session, session.begin():
        people, pictures = sync_instructors(session, get_instance(), token)
    print(f"{people} instructors updated, {pictures} with a profile picture.")


def print_password_hash(args: argparse.Namespace) -> None:
    password = getpass.getpass("Team password: ")
    if len(password) < 10:
        sys.exit("Use at least 10 characters.")
    if getpass.getpass("Repeat: ") != password:
        sys.exit("The passwords do not match.")
    print(f"TEAM_PASSWORD_HASH={hash_password(password)}")


def check_location_tables(args: argparse.Namespace) -> None:
    from sqlalchemy import func, select

    with get_sessionmaker()() as session:
        rows = session.execute(
            select(SessionRow.location, func.count())
            .where(SessionRow.status == STATUS_DONE, SessionRow.date.is_not(None))
            .group_by(SessionRow.location)
        ).all()
    report = check_locations(get_instance(), dict(rows))
    print(format_report(report))
    if report.problems:
        sys.exit(1)


def check_new_names(args: argparse.Namespace) -> None:
    from collections import Counter

    from sqlalchemy import func, select

    known: dict[str, set[str]] = {}
    new: dict[str, Counter] = {}
    with get_sessionmaker()() as session:
        for name in FIELDS:
            column = getattr(SessionRow, name)
            known[name] = {
                v
                for (v,) in session.execute(
                    select(column).where(SessionRow.source != SOURCE_SLACK).distinct()
                )
                if v
            }
            new[name] = Counter(
                dict(
                    session.execute(
                        select(column, func.count()).where(SessionRow.source == SOURCE_SLACK).group_by(column)
                    ).all()
                )
            )
    report = check_names(get_instance(), known, new)
    print(format_names_report(report))
    if report.problems:
        sys.exit(1)


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

    archive = commands.add_parser("archive-slack", help="Save the List and export items up to a date as CSV")
    archive.add_argument("--until", type=date.fromisoformat, required=True, help="last date (inclusive)")
    archive.add_argument("--out", required=True, help="directory for the raw JSON and the CSV")
    archive.set_defaults(func=archive_slack)

    remove = commands.add_parser("delete-slack", help="Delete archived items from the List")
    remove.add_argument("--csv", required=True, help="CSV written by archive-slack")
    remove.set_defaults(func=delete_slack)

    people = commands.add_parser("sync-instructors", help="Fetch instructor names and pictures from Slack")
    people.set_defaults(func=sync_instructor_profiles)

    password = commands.add_parser("hash-password", help="Print a TEAM_PASSWORD_HASH line for .env")
    password.set_defaults(func=print_password_hash)

    locations = commands.add_parser(
        "check-locations", help="Check the location and school type lookup tables against the data"
    )
    locations.set_defaults(func=check_location_tables)

    names = commands.add_parser(
        "check-names", help="List names in synced Slack entries that differ from the clean data"
    )
    names.set_defaults(func=check_new_names)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
