"""Datenschutz and Impressum (WM-153).

The privacy notice has two parts: a short public one at `/datenschutz` for visitors of the public pages
and the login (who is responsible, what a visit leaves behind, cookies, rights), and the team part at
`/datenschutz/team` behind the login (data about team members, contact persons and participants, who
sees what, where data goes, retention). Both describe what the app does from the code: the retention
periods, cookie names and job intervals below are read from the constants the code uses (`facts`), so
the text cannot drift from the behaviour; `tests/test_privacy.py` fails if a constant changes and the
page does not. Who is responsible, the legal wording and the imprint come from the private instance
config (`[privacy]`, `[imprint]`, `metrics.instance`); a missing value shows a marked placeholder, and
admins see which keys are missing.
"""

import datetime as dt

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from metrics import auth, backup, kimai, logins, oidc, scheduler
from metrics.instance import get_instance
from metrics.web import login, public, security
from metrics.web.common import page

router = APIRouter()


def duration(value: dt.timedelta) -> str:
    """A period in German words: "30 Tage", "12 Stunden", "1 Minute"."""
    seconds = int(value.total_seconds())
    for size, one, many in ((86400, "Tag", "Tage"), (3600, "Stunde", "Stunden"), (60, "Minute", "Minuten")):
        if seconds >= size and seconds % size == 0:
            n = seconds // size
            return f"{n} {one if n == 1 else many}"
    return f"{seconds} {'Sekunde' if seconds == 1 else 'Sekunden'}"


def interval(value: dt.timedelta) -> str:
    """How often something runs: "stündlich", "alle 15 Minuten"."""
    if value == dt.timedelta(hours=1):
        return "stündlich"
    if value == dt.timedelta(days=1):
        return "täglich"
    return f"alle {duration(value)}"


def _job(name: str) -> scheduler.Job:
    return next(job for job in scheduler.JOBS if job.name == name)


def _when(name: str) -> str:
    schedule = _job(name).schedule
    if isinstance(schedule, scheduler.Daily):
        return f"täglich um {schedule.at:%H:%M} Uhr"
    return interval(schedule.interval)


def facts() -> dict[str, str | int]:
    """The numbers and names of the privacy notice, read from the code at every request."""
    return {
        "session_cookie": auth.COOKIE_NAME,
        "csrf_cookie": security.CSRF_COOKIE,
        "state_cookie": oidc.STATE_COOKIE,
        "session_lifetime": duration(logins.LIFETIME),
        "session_idle": duration(logins.IDLE),
        "session_retention": duration(logins.RETENTION),
        "session_prune": _when("login-sessions"),
        "state_lifetime": duration(dt.timedelta(seconds=oidc.STATE_SECONDS)),
        "backup_daily": backup.KEEP_DAILY,
        "backup_monthly": backup.KEEP_MONTHLY,
        "backup_when": _when("backup"),
        "kimai_when": _when("kimai"),
        "kimai_past": duration(kimai.PAST),
        "kimai_ahead": duration(kimai.AHEAD),
        "calendar_when": _when("calendar"),
        "login_window": duration(dt.timedelta(seconds=login.LOGIN_LIMIT.window)),
        "data_window": duration(dt.timedelta(seconds=public.DATA_LIMIT.window)),
    }


def _privacy_context() -> dict:
    settings = get_instance().privacy
    return {"privacy": settings, "facts": facts(), "missing": settings.missing}


@router.get("/datenschutz", response_class=HTMLResponse)
def privacy_notice(request: Request):
    return page(request, "datenschutz.html", _privacy_context())


@router.get("/datenschutz/team", response_class=HTMLResponse)
def privacy_notice_team(request: Request):
    return page(request, "datenschutz_team.html", _privacy_context())


@router.get("/impressum", response_class=HTMLResponse)
def imprint(request: Request):
    settings = get_instance().imprint
    return page(request, "impressum.html", {"imprint": settings, "missing": settings.missing})
