import json
from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import select

from metrics import scheduler
from metrics.db import (
    ABGESAGT,
    BESTAETIGT,
    DURCHGEFUEHRT,
    GEPLANT,
    Appointment,
    Course,
    HistoryEntry,
    JobRun,
)
from metrics.scheduler import LOCAL, Daily, Every, Job, Scheduler
from metrics.web import health


def local(*args) -> datetime:
    return datetime(*args, tzinfo=LOCAL)


class Clock:
    """A fake clock the tests move by hand; nothing sleeps."""

    def __init__(self, now: datetime):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


# --- Schedules (no database) ----------------------------------------------------------------


def test_daily_is_due_once_after_its_time():
    daily = Daily(time(3, 30))
    assert daily.latest_slot(local(2026, 11, 13, 3, 29)) == local(2026, 11, 12, 3, 30)
    assert daily.latest_slot(local(2026, 11, 13, 3, 30)) == local(2026, 11, 13, 3, 30)
    # Never ran: due at once (a fresh install does not wait until the night).
    assert daily.due(local(2026, 11, 13, 12, 0), None, None)
    # Ran successfully after today's slot: not again until tomorrow's.
    ran = local(2026, 11, 13, 3, 30)
    assert not daily.due(local(2026, 11, 13, 23, 59), ran, ran)
    assert daily.due(local(2026, 11, 14, 3, 30), ran, ran)


def test_daily_retries_a_failed_run_an_hour_later():
    daily = Daily(time(3, 30))
    failed = local(2026, 11, 13, 3, 30)
    last_ok = local(2026, 11, 12, 3, 30)
    assert not daily.due(local(2026, 11, 13, 4, 29), failed, last_ok)
    assert daily.due(local(2026, 11, 13, 4, 30), failed, last_ok)


def test_daily_uses_local_time_across_the_dst_change():
    daily = Daily(time(0, 15))
    # 2026-10-25: clocks go back from 03:00 CEST to 02:00 CET; 00:15 is 22:15 UTC the day before.
    assert daily.latest_slot(datetime(2026, 10, 24, 22, 20, tzinfo=UTC)) == local(2026, 10, 25, 0, 15)
    assert daily.latest_slot(datetime(2026, 10, 25, 23, 20, tzinfo=UTC)) == local(2026, 10, 26, 0, 15)


def test_every_is_due_after_its_interval():
    every = Every(timedelta(minutes=15))
    start = local(2026, 11, 13, 10, 0)
    assert every.due(start, None, None)
    assert not every.due(start + timedelta(minutes=14), start, start)
    assert every.due(start + timedelta(minutes=15), start, None)
    assert every.overdue_after == timedelta(minutes=45)


# --- The loop -------------------------------------------------------------------------------


def runs(db, name):
    db.expire_all()
    return db.scalars(select(JobRun).where(JobRun.job == name).order_by(JobRun.id)).all()


def test_jobs_run_on_time_and_failures_do_not_stop_the_loop(db):
    calls = []

    def works(now):
        calls.append(now)
        return "done"

    def breaks(now):
        raise RuntimeError("boom")

    clock = Clock(local(2026, 11, 13, 0, 0))
    jobs = (Job("breaks", Daily(time(0, 15)), breaks), Job("works", Daily(time(0, 15)), works))
    loop = Scheduler(jobs=jobs, clock=clock)

    # First start: both run at once, the failing one first, the other one still runs.
    assert loop.tick() == ["breaks", "works"]
    assert [(r.ok, r.message) for r in runs(db, "breaks")] == [(False, "RuntimeError: boom")]
    assert [(r.ok, r.message) for r in runs(db, "works")] == [(True, "done")]
    assert runs(db, "works")[0].finished_at is not None

    # Before 00:15 nothing more; the failed job waits an hour before it tries again.
    clock.advance(minutes=14)
    assert loop.tick() == []
    clock.advance(minutes=1)  # 00:15: a new day's slot
    assert loop.tick() == ["breaks", "works"]
    clock.advance(minutes=30)
    assert loop.tick() == []
    clock.advance(minutes=30)
    assert loop.tick() == ["breaks"]
    clock.advance(hours=22, minutes=59)  # 00:14 the next day: still failing, still retried hourly
    assert loop.tick() == ["breaks"]
    assert len(calls) == 2  # the working job ran once per day


def test_a_restart_does_not_repeat_todays_run(db):
    clock = Clock(local(2026, 11, 13, 9, 0))
    jobs = (Job("once", Daily(time(0, 15)), lambda now: "ok"),)
    assert Scheduler(jobs=jobs, clock=clock).tick() == ["once"]
    clock.advance(hours=1)
    assert Scheduler(jobs=jobs, clock=clock).tick() == []


def test_the_loop_stops_when_asked(db):
    loop = Scheduler(jobs=(), clock=Clock(local(2026, 11, 13, 9, 0)))
    loop.stop.set()
    loop.run(tick_seconds=3600)  # returns at once instead of waiting an hour


def test_a_stop_during_a_job_skips_the_remaining_jobs(db):
    loop = Scheduler(clock=Clock(local(2026, 11, 13, 9, 0)))

    def stops(now):
        loop.stop.set()
        return "stopping"

    loop.jobs = (Job("first", Daily(time(0, 15)), stops), Job("second", Daily(time(0, 15)), stops))
    loop.run(tick_seconds=3600)
    assert [r.message for r in runs(db, "first")] == ["stopping"]
    assert runs(db, "second") == []


def test_without_a_database_the_loop_logs_and_goes_on(caplog):
    # conftest points DATABASE_URL at a port nothing listens on.
    jobs = (Job("x", Every(timedelta(minutes=1)), lambda now: "ok"),)
    assert Scheduler(jobs=jobs, clock=Clock(local(2026, 11, 13, 9, 0))).tick() == []
    assert "could not run job x" in caplog.text


# --- Stage moves ----------------------------------------------------------------------------


def test_confirmed_termine_with_a_past_date_become_held(db):
    course = Course(target_group="Schulklasse")
    termine = {
        name: Appointment(course=course, source="test", date=when, stage=stage)
        for name, when, stage in [
            ("yesterday", date(2026, 11, 12), BESTAETIGT),
            ("today", date(2026, 11, 13), BESTAETIGT),
            ("tomorrow", date(2026, 11, 14), BESTAETIGT),
            ("planned", date(2026, 11, 1), GEPLANT),
            ("cancelled", date(2026, 11, 1), ABGESAGT),
            ("no date", None, BESTAETIGT),
        ]
    }
    db.add_all(termine.values())
    db.commit()

    now = local(2026, 11, 13, 0, 15)
    assert scheduler.move_past_appointments(now) == "1 Termine moved from Bestätigt to Durchgeführt"
    db.expire_all()
    assert {name: t.stage for name, t in termine.items()} == {
        "yesterday": DURCHGEFUEHRT,
        "today": BESTAETIGT,
        "tomorrow": BESTAETIGT,
        "planned": GEPLANT,  # flagged later by the hint engine, never moved
        "cancelled": ABGESAGT,
        "no date": BESTAETIGT,
    }
    [entry] = db.scalars(select(HistoryEntry)).all()
    assert (entry.table_name, entry.row_id, entry.field) == ("appointments", termine["yesterday"].id, "stage")
    assert (entry.old_value, entry.new_value, entry.changed_by) == (BESTAETIGT, DURCHGEFUEHRT, "scheduler")
    assert entry.changed_at == now

    # A second run finds nothing to move.
    assert scheduler.move_past_appointments(now).startswith("0 Termine")


# --- /healthz -------------------------------------------------------------------------------


def healthz() -> dict:
    response = health.healthz()
    assert response.status_code == 200
    return json.loads(response.body)


def record(db, job, started, ok):
    db.add(JobRun(job=job, started_at=started, finished_at=started + timedelta(seconds=5), ok=ok))
    db.commit()


def test_healthz_reports_failed_and_overdue_jobs(db):
    now = datetime.now(UTC)
    # Never ran: overdue.
    body = healthz()
    assert body["status"] == "degraded"
    assert body["jobs"] == {
        "stage-moves": {"state": "overdue", "last_ok": None},
        "backup": {"state": "overdue", "last_ok": None},
    }

    record(db, "stage-moves", now - timedelta(hours=2), True)
    record(db, "backup", now - timedelta(hours=3), True)
    body = healthz()
    assert body["status"] == "ok"
    assert {name: job["state"] for name, job in body["jobs"].items()} == {"stage-moves": "ok", "backup": "ok"}

    # The latest backup failed: failed, until a later run succeeds.
    record(db, "backup", now - timedelta(hours=1), False)
    body = healthz()
    assert body["status"] == "degraded"
    assert body["jobs"]["backup"]["state"] == "failed"
    assert body["jobs"]["stage-moves"]["state"] == "ok"
    record(db, "backup", now - timedelta(minutes=1), True)
    assert healthz()["status"] == "ok"


@pytest.mark.parametrize(("age", "state"), [(timedelta(hours=25), "ok"), (timedelta(hours=27), "overdue")])
def test_a_daily_job_is_overdue_after_26_hours(db, age, state):
    now = datetime.now(UTC)
    record(db, "stage-moves", now - age, True)
    assert scheduler.job_status(db, now)["stage-moves"]["state"] == state
