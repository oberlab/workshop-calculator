"""Health check for monitoring."""

import os

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError

from metrics.db import ImportRun, get_sessionmaker

router = APIRouter()


@router.get("/healthz")
def healthz() -> JSONResponse:
    """For monitoring: 200 with the time of the last finished import, 503 without a database."""
    try:
        with get_sessionmaker()() as session:
            last_import = session.execute(select(func.max(ImportRun.finished_at))).scalar()
    except SQLAlchemyError:
        # No error details: the endpoint is reachable without a login.
        return JSONResponse({"status": "error", "database": "unreachable"}, status_code=503)
    return JSONResponse(
        {
            "status": "ok",
            "database": "ok",
            "last_import": last_import.isoformat() if last_import else None,
            "version": os.environ.get("APP_VERSION", "dev"),
        }
    )
