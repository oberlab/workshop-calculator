"""Health check for monitoring."""

import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from metrics.db import ImportRun, get_sessionmaker
from metrics.scheduler import job_status, utc_now

router = APIRouter()


@router.get("/healthz")
def healthz() -> JSONResponse:
    """For monitoring: the time of the last finished import and the state of the scheduler jobs.

    503 without a database. A failed or overdue job (rules in metrics.scheduler) turns `status`
    into `degraded` but keeps 200: the app itself works, and the container health check and
    ops/update.sh must not fail because a backup did. Monitors check for `"status": "ok"`.
    """
    try:
        with get_sessionmaker()() as session:
            last_import = session.execute(select(func.max(ImportRun.finished_at))).scalar()
            jobs = job_status(session, utc_now())
    except SQLAlchemyError:
        # No error details: the endpoint is reachable without a login.
        return JSONResponse({"status": "error", "database": "unreachable"}, status_code=503)
    healthy = all(job["state"] == "ok" for job in jobs.values())
    return JSONResponse(
        {
            "status": "ok" if healthy else "degraded",
            "database": "ok",
            "last_import": last_import.isoformat() if last_import else None,
            "version": os.environ.get("APP_VERSION", "dev"),
            # Job names and states only; the messages stay in job_runs (no login here).
            "jobs": jobs,
        }
    )
