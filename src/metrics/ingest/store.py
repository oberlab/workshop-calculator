"""Writing session records to the database."""

from collections import Counter
from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from metrics.db import ImportRun, SessionRow
from metrics.ingest.legacy import LEGACY_SOURCES
from metrics.ingest.records import SessionRecord
from metrics.ingest.slack import SOURCE_SLACK


def _log(session: Session, kind: str, rows: int, message: str) -> None:
    session.add(ImportRun(kind=kind, finished_at=datetime.now(UTC), rows=rows, message=message))


def replace_legacy(session: Session, records: list[SessionRecord]) -> None:
    """Replace all legacy rows. Allowed until the legacy baseline is frozen."""
    session.execute(delete(SessionRow).where(SessionRow.source.in_(LEGACY_SOURCES)))
    session.execute(insert(SessionRow), [record.as_row() for record in records])
    _log(session, "legacy-load", len(records), f"{len(records)} legacy rows loaded")


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
