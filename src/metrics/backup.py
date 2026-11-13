"""Nightly database dump into the backup directory, with pruning of old dumps (WM-103).

Each run writes `metrics-daily-<YYYY-MM-DD>.dump` (pg_dump custom format, restore with
pg_restore, see ops/DEPLOY.md). The first dump of a month is also kept as
`metrics-monthly-<YYYY-MM>.dump`. Afterwards the newest 14 daily and the newest 12 monthly dumps
are kept and older ones are deleted.

This pruning is the one automatic deletion in the application, and it is narrow on purpose:
it only looks at files directly in the backup directory whose whole name matches one of the
two patterns above, and it only runs after a new dump was written and checked. Anything else in
the directory (the dumps `ops/update.sh` writes before an update, files copied there by hand,
an unfinished `.partial` file) is never touched. Retention counts files, not days, so a backup
job that fails for weeks never deletes the last good dumps.

The dump is written to a `.partial` file and renamed when pg_dump succeeded, so a complete
file name always means a complete dump. The database password goes to pg_dump in the
environment (PGPASSWORD), never on the command line.
"""

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from sqlalchemy import make_url

log = logging.getLogger(__name__)

DAILY = re.compile(r"metrics-daily-\d{4}-\d{2}-\d{2}\.dump")
MONTHLY = re.compile(r"metrics-monthly-\d{4}-\d{2}\.dump")
KEEP_DAILY = 14
KEEP_MONTHLY = 12
DUMP_TIMEOUT_SECONDS = 30 * 60
# Every pg_dump custom-format file starts with these bytes.
CUSTOM_FORMAT_MAGIC = b"PGDMP"


class BackupError(Exception):
    pass


@dataclass
class BackupResult:
    dump: Path
    size: int
    monthly: Path | None = None
    deleted: list[Path] = field(default_factory=list)

    def summary(self) -> str:
        text = f"{self.dump.name} ({self.size} bytes)"
        if self.monthly:
            text += f", kept as {self.monthly.name}"
        if self.deleted:
            text += f", pruned {', '.join(p.name for p in self.deleted)}"
        return text


def pg_dump_command(database_url: str, pg_dump: str = "pg_dump") -> tuple[list[str], dict[str, str]]:
    """Command line and environment for pg_dump; the password only goes into the environment."""
    url = make_url(database_url)
    if not url.database:
        raise BackupError("DATABASE_URL names no database")
    args = [pg_dump, "--format=custom", "--no-owner", "--no-privileges", "--no-password"]
    if url.host:
        args += ["--host", url.host]
    if url.port:
        args += ["--port", str(url.port)]
    if url.username:
        args += ["--username", url.username]
    args += ["--dbname", url.database]
    env = {**os.environ, "PGCONNECT_TIMEOUT": "10"}
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    return args, env


def dump_database(database_url: str, directory: Path, day: date, pg_dump: str = "pg_dump") -> Path:
    """Write the daily dump for `day` into `directory` and return its path."""
    if not directory.is_dir():
        raise BackupError(f"backup directory {directory} does not exist")
    target = directory / f"metrics-daily-{day.isoformat()}.dump"
    partial = target.with_name(target.name + ".partial")
    args, env = pg_dump_command(database_url, pg_dump)
    try:
        # Readable by the owner and the directory's group only.
        descriptor = os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
        with os.fdopen(descriptor, "wb") as out:
            result = subprocess.run(
                args, stdout=out, stderr=subprocess.PIPE, env=env, timeout=DUMP_TIMEOUT_SECONDS, check=False
            )
        if result.returncode != 0:
            lines = result.stderr.decode(errors="replace").strip().splitlines()
            raise BackupError(f"pg_dump exited with {result.returncode}: {lines[-1] if lines else ''}")
        with partial.open("rb") as dump:
            if dump.read(len(CUSTOM_FORMAT_MAGIC)) != CUSTOM_FORMAT_MAGIC:
                raise BackupError("pg_dump wrote no custom-format dump")
        partial.replace(target)
    except BaseException:
        # Only the unfinished file of this very run.
        partial.unlink(missing_ok=True)
        raise
    return target


def keep_monthly(daily: Path, day: date) -> Path | None:
    """Keep the first dump of a month as the monthly copy (a hard link, else a copy)."""
    monthly = daily.with_name(f"metrics-monthly-{day:%Y-%m}.dump")
    if monthly.exists():
        return None
    try:
        os.link(daily, monthly)
    except OSError:
        shutil.copy2(daily, monthly)
    return monthly


def prune(directory: Path, keep_daily: int = KEEP_DAILY, keep_monthly: int = KEEP_MONTHLY) -> list[Path]:
    """Delete all but the newest daily and monthly dumps; touches no other file."""
    deleted: list[Path] = []
    for pattern, keep in ((DAILY, keep_daily), (MONTHLY, keep_monthly)):
        # The names carry ISO dates, so sorting by name sorts by date.
        dumps = sorted(p for p in directory.iterdir() if pattern.fullmatch(p.name) and p.is_file())
        for old in dumps[: max(len(dumps) - keep, 0)]:
            old.unlink()
            log.info("pruned old dump %s", old.name)
            deleted.append(old)
    return deleted


def make_backup(database_url: str, directory: Path, day: date, pg_dump: str = "pg_dump") -> BackupResult:
    """The nightly job: dump, keep the monthly copy, prune."""
    dump = dump_database(database_url, directory, day, pg_dump)
    result = BackupResult(dump=dump, size=dump.stat().st_size, monthly=keep_monthly(dump, day))
    result.deleted = prune(directory)
    return result
