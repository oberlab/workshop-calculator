import sys
from datetime import date, timedelta

import pytest

from metrics import backup
from metrics.backup import BackupError, make_backup, pg_dump_command, prune

URL = "postgresql+psycopg://metrics:s3cret@db:5432/metrics"


@pytest.fixture
def fake_pg_dump(tmp_path):
    """A stand-in for pg_dump that writes a custom-format header (CI has no matching pg_dump)."""
    script = tmp_path / "pg_dump"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "if os.environ.get('FAKE_PG_DUMP_FAIL'):\n"
        "    sys.stdout.buffer.write(b'half a dump')\n"
        "    sys.exit('pg_dump: error: connection refused')\n"
        "sys.stdout.buffer.write(b'PGDMP' + ' '.join(sys.argv[1:]).encode())\n"
    )
    script.chmod(0o755)
    return str(script)


@pytest.fixture
def directory(tmp_path):
    path = tmp_path / "backups"
    path.mkdir()
    return path


def test_the_password_goes_into_the_environment_only():
    args, env = pg_dump_command(URL)
    assert args == [
        "pg_dump",
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--no-password",
        "--host",
        "db",
        "--port",
        "5432",
        "--username",
        "metrics",
        "--dbname",
        "metrics",
    ]
    assert not any("s3cret" in arg for arg in args)
    assert env["PGPASSWORD"] == "s3cret"


def test_a_nightly_backup_writes_the_daily_and_the_first_monthly_dump(directory, fake_pg_dump):
    first = make_backup(URL, directory, date(2026, 11, 1), fake_pg_dump)
    assert first.dump == directory / "metrics-daily-2026-11-01.dump"
    assert first.dump.read_bytes().startswith(b"PGDMP--format=custom")
    assert first.dump.stat().st_mode & 0o777 == 0o640
    assert first.monthly == directory / "metrics-monthly-2026-11.dump"
    assert first.monthly.read_bytes() == first.dump.read_bytes()

    second = make_backup(URL, directory, date(2026, 11, 2), fake_pg_dump)
    assert second.monthly is None
    assert sorted(p.name for p in directory.iterdir()) == [
        "metrics-daily-2026-11-01.dump",
        "metrics-daily-2026-11-02.dump",
        "metrics-monthly-2026-11.dump",
    ]
    assert second.summary().startswith("metrics-daily-2026-11-02.dump (")


def test_a_failed_dump_leaves_no_file_and_prunes_nothing(directory, fake_pg_dump, monkeypatch):
    for day in range(20):
        (directory / f"metrics-daily-2026-10-{day + 1:02}.dump").write_bytes(b"PGDMP")
    monkeypatch.setenv("FAKE_PG_DUMP_FAIL", "1")
    with pytest.raises(BackupError, match="pg_dump exited with 1: pg_dump: error: connection refused"):
        make_backup(URL, directory, date(2026, 11, 13), fake_pg_dump)
    assert len(list(directory.iterdir())) == 20


def test_a_missing_directory_is_an_error_not_created(tmp_path, fake_pg_dump):
    with pytest.raises(BackupError, match="does not exist"):
        make_backup(URL, tmp_path / "missing", date(2026, 11, 13), fake_pg_dump)
    assert not (tmp_path / "missing").exists()


def test_pruning_keeps_14_daily_and_12_monthly_and_touches_nothing_else(directory):
    start = date(2026, 1, 1)
    for n in range(40):
        (directory / f"metrics-daily-{start + timedelta(days=n)}.dump").write_bytes(b"PGDMP")
    for month in range(1, 13):
        (directory / f"metrics-monthly-2025-{month:02}.dump").write_bytes(b"PGDMP")
    (directory / "metrics-monthly-2026-01.dump").write_bytes(b"PGDMP")
    others = [
        "before-v1.0.0-20260101-120000.dump",  # written by ops/update.sh
        "metrics-daily-2025-01-01.dump.partial",
        "metrics-daily-2025-01-01.dump.bak",
        "old-metrics-daily-2025-01-01.dump",
        "notes.txt",
    ]
    for name in others:
        (directory / name).write_text("keep me")
    (directory / "metrics-daily-2024-01-01.dump").mkdir()  # not a file

    deleted = prune(directory)

    assert len(deleted) == 26 + 1
    names = sorted(p.name for p in directory.iterdir())
    daily = [n for n in names if backup.DAILY.fullmatch(n) and (directory / n).is_file()]
    monthly = [n for n in names if backup.MONTHLY.fullmatch(n)]
    assert daily == [f"metrics-daily-{start + timedelta(days=n)}.dump" for n in range(26, 40)]
    assert monthly == [f"metrics-monthly-2025-{m:02}.dump" for m in range(2, 13)] + [
        "metrics-monthly-2026-01.dump"
    ]
    assert set(others) | {"metrics-daily-2024-01-01.dump"} <= set(names)
