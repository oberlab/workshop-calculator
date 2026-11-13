"""Mein Bereich (WM-112): the landing page after login.

`GET /mein-bereich` (Team): Zu erledigen, Meine nächsten Termine and Meine Auslastung of the Person
linked to the login (`metrics.planning.mine`). A login without a linked Person sees how to get
linked instead; nobody ever sees another person's tasks here.
"""

import datetime as dt

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from metrics import auth, hints
from metrics.db import get_sessionmaker
from metrics.planning import mine, workflow
from metrics.web.common import current_login, page, today
from metrics.web.planning import _me

router = APIRouter()

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]  # fmt: skip


def long_date(day: dt.date) -> str:
    """A date as "Freitag, 13. November"."""
    return f"{WEEKDAYS[day.weekday()]}, {day.day}. {MONTHS[day.month - 1]}"


def hours(value) -> str:
    """Hours the German way: 2,5 Std."""
    text = f"{float(value):.1f}".replace(".", ",").removesuffix(",0")
    return f"{text} Std."


@router.get("/mein-bereich", response_class=HTMLResponse)
def my_area(request: Request):
    day = today()
    with get_sessionmaker()() as session:
        me = _me(session, request)
        context = {
            "me": me,
            "today_text": long_date(day),
            "shared_login": auth.mode() == "password",
            "login_user": current_login(request).user,
        }
        if me is not None:
            todo = mine.tasks(session, me, day)
            termine = mine.upcoming(session, me, day)
            context |= {
                "tasks": todo,
                "termine": termine,
                "this_week": sum(1 for t in termine if t.date < day + dt.timedelta(days=7)),
                "load": mine.workload(session, me, day),
                "titles": {t.id: workflow.title(t) for t in [*termine, *(x.t for x in todo)]},
                "badge": hints.BADGE,
                "hours": hours,
                "today": day,
            }
        return page(request, "mein_bereich.html", context)
