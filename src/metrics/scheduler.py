"""`metrics scheduler`: a small loop that runs fixed jobs on time (WM-103, PROPOSAL.md section 8).

It runs as its own Compose service from the app image. Every TICK_SECONDS it asks each job in
JOBS whether it is due and runs the due ones one after the other. Each run is a row in
`job_runs` (started, finished, ok, message); whether a job is due is decided from those rows,
so a restart neither skips nor repeats a run. A job that raises is recorded as failed and
logged; the loop goes on. SIGTERM and SIGINT stop the loop after the job that is running.

Adding a job (calendar check WM-110, Slack messages WM-130, Kimai WM-142): write a function
`(now: datetime) -> str` that does the work in its own database session and returns a short
message for `job_runs`, then add a `Job` with a schedule to JOBS. `/healthz` picks it up.

Schedules and when a job counts as overdue on /healthz:
- `Daily(at)`: once a day at a local time (Europe/Berlin). Due when there was no successful
  run since the latest such time; after a failure it is tried again an hour later. Overdue when
  the last successful run started more than 26 hours ago (one day plus two hours of slack).
- `Every(interval)`: due when the last run started at least `interval` ago. Overdue when the
  last successful run started more than three intervals ago.
A job that never succeeded counts as overdue; a job whose latest finished run failed counts as
failed until a later run succeeds.
"""

import logging
import signal
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from metrics import backup as backups
from metrics.config import get_settings
from metrics.db import BESTAETIGT, DURCHGEFUEHRT, Appointment, HistoryEntry, JobRun, get_sessionmaker

log = logging.getLogger(__name__)

LOCAL = ZoneInfo("Europe/Berlin")
TICK_SECONDS = 30
RETRY_AFTER = timedelta(hours=1)
# `changed_by` in the Verlauf for changes the scheduler makes.
ACTOR = "scheduler"


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class Daily:
    """Once a day at a local wall-clock time (pick one outside 02:00-03:00, the DST change)."""

    at: time
    overdue_after: timedelta = timedelta(hours=26)

    def latest_slot(self, now: datetime) -> datetime:
        local = now.astimezone(LOCAL)
        slot = datetime.combine(local.date(), self.at, tzinfo=LOCAL)
        if slot > local:
            slot = datetime.combine(local.date() - timedelta(days=1), self.at, tzinfo=LOCAL)
        return slot

    def due(self, now: datetime, last_start: datetime | None, last_ok: datetime | None) -> bool:
        slot = self.latest_slot(now)
        if last_ok is not None and last_ok >= slot:
            return False
        return last_start is None or last_start < slot or now - last_start >= RETRY_AFTER


@dataclass(frozen=True)
class Every:
    """Every `interval`, counted from the start of the last run."""

    interval: timedelta

    @property
    def overdue_after(self) -> timedelta:
        return 3 * self.interval

    def due(self, now: datetime, last_start: datetime | None, last_ok: datetime | None) -> bool:
        return last_start is None or now - last_start >= self.interval


@dataclass(frozen=True)
class Job:
    name: str
    schedule: Daily | Every
    # Gets the scheduler's current time, returns a short message for job_runs.
    run: Callable[[datetime], str]


# --- The jobs -------------------------------------------------------------------------------


def move_past_appointments(now: datetime) -> str:
    """Termine in stage Bestätigt whose date has passed move to Durchgeführt.

    Geplant Termine with a past date stay where they are: the hint engine (WM-113) flags them,
    a person decides. Each move is recorded in the Verlauf with the scheduler as actor.
    """
    today = now.astimezone(LOCAL).date()
    with get_sessionmaker()() as session, session.begin():
        appointments = session.scalars(
            select(Appointment)
            .where(Appointment.stage == BESTAETIGT, Appointment.date < today)
            .order_by(Appointment.id)
            .with_for_update()
        ).all()
        for appointment in appointments:
            appointment.stage = DURCHGEFUEHRT
            session.add(
                HistoryEntry(
                    table_name=Appointment.__tablename__,
                    row_id=appointment.id,
                    field="stage",
                    old_value=BESTAETIGT,
                    new_value=DURCHGEFUEHRT,
                    changed_by=ACTOR,
                    changed_at=now,
                    reason="Datum liegt in der Vergangenheit",
                )
            )
    return f"{len(appointments)} Termine moved from {BESTAETIGT} to {DURCHGEFUEHRT}"


def nightly_backup(now: datetime) -> str:
    settings = get_settings()
    day = now.astimezone(LOCAL).date()
    return backups.make_backup(settings.database_url, settings.backup_dir, day).summary()


JOBS: tuple[Job, ...] = (
    Job("stage-moves", Daily(time(0, 15)), move_past_appointments),
    Job("backup", Daily(time(3, 30)), nightly_backup),
    # WM-110: Job("calendar", Every(timedelta(minutes=15)), check_calendar),
)


# --- Runs and the loop ----------------------------------------------------------------------


def last_starts(session: Session, name: str) -> tuple[datetime | None, datetime | None]:
    """Start of the latest run and of the latest successful run of a job."""
    row = session.execute(
        select(func.max(JobRun.started_at), func.max(JobRun.started_at).filter(JobRun.ok.is_(True))).where(
            JobRun.job == name
        )
    ).one()
    return row[0], row[1]


def run_job(job: Job, now: datetime, clock: Callable[[], datetime] = utc_now) -> bool:
    """Run one job and record it in job_runs; a failure is recorded and logged, not raised."""
    with get_sessionmaker()() as session, session.begin():
        run = JobRun(job=job.name, started_at=now)
        session.add(run)
        session.flush()
        run_id = run.id
    log.info("job %s started", job.name)
    try:
        message, ok = job.run(now), True
        log.info("job %s finished: %s", job.name, message)
    except Exception as error:
        message, ok = f"{type(error).__name__}: {error}", False
        log.exception("job %s failed", job.name)
    with get_sessionmaker()() as session, session.begin():
        run = session.get_one(JobRun, run_id)
        run.finished_at, run.ok, run.message = clock(), ok, message[:2000]
    return ok


@dataclass
class Scheduler:
    jobs: Sequence[Job] = JOBS
    clock: Callable[[], datetime] = utc_now
    stop: threading.Event = field(default_factory=threading.Event)

    def tick(self) -> list[str]:
        """Run every job that is due now; returns the names of the jobs that ran."""
        ran = []
        for job in self.jobs:
            if self.stop.is_set():
                break
            now = self.clock()
            try:
                with get_sessionmaker()() as session:
                    last_start, last_ok = last_starts(session, job.name)
                if not job.schedule.due(now, last_start, last_ok):
                    continue
                run_job(job, now, self.clock)
                ran.append(job.name)
            except Exception:
                # The database is away or job_runs is missing (before the migration): try again later.
                log.exception("could not run job %s", job.name)
        return ran

    def run(self, tick_seconds: float = TICK_SECONDS) -> None:
        log.info("scheduler started with jobs %s", ", ".join(job.name for job in self.jobs))
        while not self.stop.is_set():
            self.tick()
            self.stop.wait(tick_seconds)
        log.info("scheduler stopped")


def serve() -> None:
    """The `metrics scheduler` command: run the loop until SIGTERM or SIGINT."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    scheduler = Scheduler()

    def stop(signum, frame) -> None:
        log.info("signal %s: stopping after the current job", signal.Signals(signum).name)
        scheduler.stop.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    scheduler.run()


# --- State for /healthz ---------------------------------------------------------------------


def job_status(session: Session, now: datetime, jobs: Sequence[Job] | None = None) -> dict[str, dict]:
    """Per job: `ok`, `failed` (the latest finished run failed) or `overdue`, and the last success."""
    status = {}
    for job in JOBS if jobs is None else jobs:
        _, last_ok = last_starts(session, job.name)
        latest_ok = session.scalar(
            select(JobRun.ok)
            .where(JobRun.job == job.name, JobRun.finished_at.is_not(None))
            .order_by(JobRun.finished_at.desc())
            .limit(1)
        )
        if latest_ok is False:
            state = "failed"
        elif last_ok is None or now - last_ok > job.schedule.overdue_after:
            state = "overdue"
        else:
            state = "ok"
        status[job.name] = {"state": state, "last_ok": last_ok.isoformat() if last_ok else None}
    return status
