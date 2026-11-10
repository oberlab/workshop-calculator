"""Writing session records to the database."""

from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from metrics.db import ImportRun, SessionRow
from metrics.ingest.baseline import SOURCE_BASELINE
from metrics.ingest.legacy import LEGACY_SOURCES
from metrics.ingest.records import SessionRecord
from metrics.ingest.slack import SOURCE_SLACK


def _log(session: Session, kind: str, rows: int, message: str) -> None:
    session.add(ImportRun(kind=kind, finished_at=datetime.now(UTC), rows=rows, message=message))


class BaselineFrozen(Exception):
    """A load would change data covered by the frozen baseline."""


def baseline_end(session: Session):
    """Last date of the frozen baseline, or None while no baseline is loaded."""
    return session.execute(
        select(func.max(SessionRow.date)).where(SessionRow.source == SOURCE_BASELINE)
    ).scalar()


def _replace_archived_slack_rows(session: Session, records: list[SessionRecord]) -> None:
    # Rows from archived Slack exports replace the synced Slack rows of the same items, so an
    # item is never counted twice after it has been handed over from the List.
    archived = [r.source_key.split(":slack:", 1)[1] for r in records if ":slack:" in r.source_key]
    if archived:
        slack_keys = [f"{SOURCE_SLACK}:{item_id}" for item_id in archived]
        session.execute(
            delete(SessionRow).where(SessionRow.source == SOURCE_SLACK, SessionRow.source_key.in_(slack_keys))
        )


def replace_legacy(session: Session, records: list[SessionRecord]) -> None:
    """Replace all legacy rows (not the baseline).

    Once a baseline is loaded, only entries dated after its last date may be loaded this way
    (quarterly hand-overs); anything older belongs to the frozen baseline and is refused.
    """
    end = baseline_end(session)
    if end is not None:
        older = sorted({r.date for r in records if r.date is None or r.date <= end}, key=str)
        if older:
            raise BaselineFrozen(
                f"The baseline is frozen up to {end}; {len(older)} date(s) in these files are not after it "
                f"(first: {older[0]}). Load only newer exports."
            )
    session.execute(delete(SessionRow).where(SessionRow.source.in_(LEGACY_SOURCES)))
    _replace_archived_slack_rows(session, records)
    session.execute(insert(SessionRow), [record.as_row() for record in records])
    _log(session, "legacy-load", len(records), f"{len(records)} legacy rows loaded")


def load_baseline(session: Session, records: list[SessionRecord], version: str, sha256: str) -> None:
    """Load the baseline once; it replaces the legacy rows it was made from and is never replaced.

    The legacy rows hold the same entries (the baseline was exported from them), so nothing is
    lost; a second baseline load is refused.
    """
    if baseline_end(session) is not None:
        raise BaselineFrozen("A baseline is already loaded and frozen; it cannot be loaded again.")
    session.execute(delete(SessionRow).where(SessionRow.source.in_(LEGACY_SOURCES)))
    _replace_archived_slack_rows(session, records)
    session.execute(insert(SessionRow), [record.as_row() for record in records])
    _log(
        session,
        "baseline-load",
        len(records),
        f"baseline {version} loaded and frozen: {len(records)} rows, sha256 {sha256}",
    )


def upsert_slack(session: Session, records: list[SessionRecord]) -> int:
    """Insert or update Slack items; items no longer in the List are kept and marked.

    Returns the number of rows newly marked as removed from Slack.
    """
    rows = [{**record.as_row(), "source_state": "active"} for record in records]
    if rows:
        statement = insert(SessionRow).values(rows)
        updatable = {c: statement.excluded[c] for c in rows[0] if c not in ("source_key",)}
        session.execute(statement.on_conflict_do_update(index_elements=["source_key"], set_=updatable))
    seen = [record.source_key for record in records]
    removed = session.execute(
        update(SessionRow)
        .where(SessionRow.source == SOURCE_SLACK)
        .where(SessionRow.source_key.not_in(seen))
        .where(SessionRow.source_state == "active")
        .values(source_state="removed_from_slack")
    ).rowcount
    _log(
        session,
        "slack-sync",
        len(records),
        f"{len(records)} items synced, {removed} newly removed from Slack",
    )
    return removed


def summary(session: Session, sources: tuple[str, ...]) -> dict[str, Counter]:
    """Sessions per year and status, per target group, for the CLI report."""
    rows = session.execute(
        select(SessionRow.date, SessionRow.status, SessionRow.target_group).where(
            SessionRow.source.in_(sources)
        )
    ).all()
    return {
        "per_year_and_status": Counter((d.year if d else None, status) for d, status, _ in rows),
        "per_target_group": Counter(group for _, _, group in rows),
    }
