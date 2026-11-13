"""Command line: `metrics load-legacy`, `sync-instructors`, `archive-slack`, `delete-slack`,
`transfer-slack`, `hash-password`, `export-baseline`, `load-baseline`, `scheduler`."""

import argparse
import getpass
import sys
from collections import Counter
from datetime import date
from pathlib import Path

from metrics import scheduler
from metrics.auth import hash_password
from metrics.config import get_settings
from metrics.db import get_sessionmaker
from metrics.ingest import store, transfer
from metrics.ingest.archive import archive_list, delete_archived
from metrics.ingest.baseline import SOURCE_BASELINE, checksum, read_baseline, write_baseline
from metrics.ingest.instructors import sync_instructors
from metrics.ingest.legacy import LEGACY_SOURCES, read_harmonised, read_slack_export
from metrics.ingest.records import SessionRecord
from metrics.ingest.slack import SlackError
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
    records = _read_legacy_files(args)
    try:
        with get_sessionmaker()() as session, session.begin():
            store.replace_legacy(session, records, get_instance())
    except store.BaselineFrozen as error:
        sys.exit(str(error))
    print(f"Replaced all legacy rows with {len(records)} rows.")
    _print_warnings(records)
    _print_summary(LEGACY_SOURCES)


def _read_legacy_files(args: argparse.Namespace) -> list[SessionRecord]:
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
    return records


def export_baseline(args: argparse.Namespace) -> None:
    records = _read_legacy_files(args)
    sha256 = write_baseline(records, Path(args.out))
    dates = [r.date for r in records if r.date]
    print(f"{len(records)} entries ({min(dates)} to {max(dates)}) written to {args.out}")
    print(f"sha256 {sha256}")


def load_baseline_file(args: argparse.Namespace) -> None:
    path = Path(args.csv)
    sha256 = checksum(path)
    if args.sha256 and args.sha256 != sha256:
        sys.exit(f"Checksum does not match: expected {args.sha256}, file has {sha256}.")
    instance = get_instance()
    records = read_baseline(path, instance)
    try:
        with get_sessionmaker()() as session, session.begin():
            store.load_baseline(session, records, instance, args.version, sha256)
    except store.BaselineFrozen as error:
        sys.exit(str(error))
    print(f"Baseline {args.version} loaded and frozen: {len(records)} rows, sha256 {sha256}")
    _print_warnings(records)
    _print_summary((SOURCE_BASELINE,))


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


def transfer_slack(args: argparse.Namespace) -> None:
    instance = get_instance()
    try:
        if args.archive:
            token, list_id = _slack_credentials()
            list_ids = {transfer.MAIN: list_id}
            if instance.slack_project_list:
                if not get_settings().slack_project_list_id:
                    sys.exit("[slack.project_list] is configured: SLACK_PROJECT_LIST_ID must be set.")
                list_ids[transfer.PROJECT] = get_settings().slack_project_list_id
            folder = transfer.write_archive(Path(args.archive), transfer.fetch_lists(token, list_ids))
            print(f"Listen gesichert in {folder}\n")
        else:
            folder = Path(args.from_archive)
        lists = transfer.read_archive(folder, instance)
        with get_sessionmaker()() as session:
            report = transfer.transfer(session, lists, instance, date.today())
            if args.dry_run:
                session.rollback()
            else:
                session.commit()
    except (transfer.TransferError, SlackError) as error:
        sys.exit(str(error))
    print(transfer.format_report(report))
    if args.dry_run:
        print("\nProbelauf (--dry-run): nichts gespeichert.")


def sync_instructor_profiles(args: argparse.Namespace) -> None:
    token, _ = _slack_credentials()
    with get_sessionmaker()() as session, session.begin():
        people, pictures = sync_instructors(session, token)
    print(f"{people} instructors updated, {pictures} with a profile picture.")


def print_password_hash(args: argparse.Namespace) -> None:
    password = getpass.getpass("Team password: ")
    if len(password) < 10:
        sys.exit("Use at least 10 characters.")
    if getpass.getpass("Repeat: ") != password:
        sys.exit("The passwords do not match.")
    print(f"TEAM_PASSWORD_HASH={hash_password(password)}")


def run_scheduler(args: argparse.Namespace) -> None:
    if not args.run:
        scheduler.serve()
        return
    job = next(job for job in scheduler.JOBS if job.name == args.run)
    if not scheduler.run_job(job, scheduler.utc_now()):
        sys.exit(f"Job {job.name} failed (details in the log above and in job_runs).")


def main() -> None:
    parser = argparse.ArgumentParser(prog="metrics")
    commands = parser.add_subparsers(required=True)

    legacy = commands.add_parser("load-legacy", help="Replace all legacy rows with the given CSV files")
    legacy.add_argument(
        "--harmonised", action="append", default=[], help="CSV in the harmonised legacy format"
    )
    legacy.add_argument("--slack-export", action="append", default=[], help="CSV export of the Slack List")
    legacy.set_defaults(func=load_legacy)

    archive = commands.add_parser("archive-slack", help="Save the List and export items up to a date as CSV")
    archive.add_argument("--until", type=date.fromisoformat, required=True, help="last date (inclusive)")
    archive.add_argument("--out", required=True, help="directory for the raw JSON and the CSV")
    archive.set_defaults(func=archive_slack)

    remove = commands.add_parser("delete-slack", help="Delete archived items from the List")
    remove.add_argument("--csv", required=True, help="CSV written by archive-slack")
    remove.set_defaults(func=delete_slack)

    take = commands.add_parser(
        "transfer-slack",
        help="Transfer the Slack Lists into Termine once (items after the baseline); Slack is only read",
    )
    origin = take.add_mutually_exclusive_group(required=True)
    origin.add_argument(
        "--archive", metavar="DIR", help="read the Lists from Slack, save them under DIR, then transfer"
    )
    origin.add_argument("--from-archive", metavar="FOLDER", help="transfer from an archive folder (offline)")
    take.add_argument("--dry-run", action="store_true", help="show the summary, save nothing in the database")
    take.set_defaults(func=transfer_slack)

    people = commands.add_parser("sync-instructors", help="Fetch instructor names and pictures from Slack")
    people.set_defaults(func=sync_instructor_profiles)

    password = commands.add_parser("hash-password", help="Print a TEAM_PASSWORD_HASH line for .env")
    password.set_defaults(func=print_password_hash)

    export = commands.add_parser(
        "export-baseline", help="Write all reviewed legacy data into one baseline CSV"
    )
    export.add_argument(
        "--harmonised", action="append", default=[], help="CSV in the harmonised legacy format"
    )
    export.add_argument("--slack-export", action="append", default=[], help="CSV export of the Slack List")
    export.add_argument("--out", required=True, help="baseline CSV to write")
    export.set_defaults(func=export_baseline)

    baseline = commands.add_parser("load-baseline", help="Load the baseline CSV once and freeze it")
    baseline.add_argument("csv", help="baseline CSV written by export-baseline")
    baseline.add_argument("--version", required=True, help="name of this baseline, e.g. 2026-09-30")
    baseline.add_argument("--sha256", help="expected checksum; the load stops if the file differs")
    baseline.set_defaults(func=load_baseline_file)

    loop = commands.add_parser("scheduler", help="Run the scheduler loop (stage moves, nightly backup)")
    loop.add_argument(
        "--run", choices=[job.name for job in scheduler.JOBS], help="run this one job now, then exit"
    )
    loop.set_defaults(func=run_scheduler)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
