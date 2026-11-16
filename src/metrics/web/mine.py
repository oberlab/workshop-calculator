"""Mein Bereich (WM-112): the landing page after login, and the phone form after the course (WM-133).

`GET /mein-bereich` (Team): Zu erledigen, Meine nächsten Termine and Meine Auslastung of the Person
linked to the login (`metrics.planning.mine`). A login without a linked Person sees how to get
linked instead; nobody ever sees another person's tasks here.

`GET /mein-bereich/nach-dem-kurs/{id}` (Team): participants, Herkunft and a note for one Termin, only
what is missing (`metrics.planning.after`); `POST` saves it and goes back to Mein Bereich once
nothing is missing. The address names the Termin by its ID only, so the Slack DM (WM-130) can link
to it without personal data in the URL.
"""

import datetime as dt
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from metrics import auth, hints
from metrics.db import Appointment, get_sessionmaker
from metrics.planning import after, mine, workflow
from metrics.planning.workflow import FormError
from metrics.web.common import current_login, page, today
from metrics.web.planning import _form, _me, _municipalities

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
            "message": request.query_params.get("hinweis"),
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


# --- After the course (WM-133) -------------------------------------------------------------------

AFTER = "/mein-bereich/nach-dem-kurs/{appointment_id}"


def _termin(session, appointment_id: int) -> Appointment:
    t = session.get(Appointment, appointment_id)
    if t is None:
        raise HTTPException(404, "Termin nicht gefunden")
    return t


def _after_page(request: Request, session, t: Appointment, errors=None, gaps=None, note="", status_code=200):
    """The phone form; after a refused save `gaps` keeps the fields shown that were sent."""
    gaps = gaps or after.gaps(t)
    context = {
        "t": t,
        "title": workflow.title(t),
        "day_text": long_date(t.date) if t.date else "ohne Datum",
        "open": after.open_for_entry(t),
        "gaps": gaps,
        "participant_fields": workflow.PARTICIPANT_FIELDS,
        "total": hints.participants(t),
        "origins": {o.municipality: o.participants for o in t.origins},
        "origins_total": after.origins_total(t),
        "suggested": after.suggested_municipalities(session, t) if gaps.origins else [],
        "free_rows": after.FREE_ROWS,
        "municipalities": _municipalities(session) if gaps.origins else [],
        "course_href": f"/planung/{t.course_id}?termin={t.id}",
        "action": AFTER.format(appointment_id=t.id),
        "note": note,
        "errors": errors or [],
        "message": request.query_params.get("hinweis"),
    }
    return page(request, "nach_dem_kurs.html", context, status_code=status_code)


@router.get("/mein-bereich/nach-dem-kurs/{appointment_id:int}", response_class=HTMLResponse)
def after_course(appointment_id: int, request: Request):
    with get_sessionmaker()() as session:
        return _after_page(request, session, _termin(session, appointment_id))


@router.post("/mein-bereich/nach-dem-kurs/{appointment_id:int}", response_class=HTMLResponse)
async def save_after_course(appointment_id: int, request: Request):
    form = await _form(request)
    with get_sessionmaker()() as session:
        t = _termin(session, appointment_id)
        try:
            after.record(session, t, form, current_login(request).user)
        except FormError as error:
            # The form again with the entered values (not saved) and what is wrong.
            session.rollback()
            t = _termin(session, appointment_id)
            with session.no_autoflush:
                gaps = after.gaps(t)
                if after.open_for_entry(t):
                    workflow._apply_appointment(session, t, form, {workflow.HELD}, [])
                response = _after_page(
                    request, session, t, error.messages, gaps, (form.get("note") or [""])[0], status_code=400
                )
            session.rollback()
            return response
        session.commit()
        missing = after.gaps(t)
        if missing.any:
            what = [x for x, gap in (("Teilnehmende", missing.counts), ("Herkunft", missing.origins)) if gap]
            done = f"Gespeichert. Es fehlt noch: {', '.join(what)}."
            return RedirectResponse(
                f"{AFTER.format(appointment_id=t.id)}?hinweis={quote(done)}", status_code=303
            )
        done = f"Danke! {workflow.title(t)} ist eingetragen."
        return RedirectResponse(f"/mein-bereich?hinweis={quote(done)}", status_code=303)
