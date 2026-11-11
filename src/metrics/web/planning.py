"""Planning prototype pages: course list, course page, availability, review actions.

Everything here reads and writes the planning database (`PLANNING_DATABASE_URL`); the metrics
database is only read for suggestions and the people list.
"""

import datetime as dt
import secrets
import time
from urllib.parse import parse_qs, quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import select

from metrics.instance import get_instance
from metrics.planning import calendar as cal
from metrics.planning import demo, rules, service, vocabulary
from metrics.planning.conflicts import SEVERITY_LABELS, busy_people
from metrics.planning.models import (
    BESTAETIGT,
    CAL_OK,
    DURCHGEFUEHRT,
    GEPLANT,
    LEAD,
    STAGES,
    SUPPORT,
    CalendarOrphan,
    Course,
    DemoCalendarEvent,
    FinalSession,
    History,
    Person,
    get_planning_sessionmaker,
)
from metrics.web.app import current_login, page, public_base, today

router = APIRouter()

FILTERS = ("person", "from", "to", "context", "location", "category", "flagged", "unbilled")


def _login(request: Request):
    login = current_login(request)
    if login is None:
        raise HTTPException(401, "Anmeldung erforderlich")
    return login


def _admin(request: Request):
    login = _login(request)
    if not login.admin:
        raise HTTPException(403, "Nur für Admins")
    return login


async def _form(request: Request) -> dict[str, list[str]]:
    return parse_qs((await request.body()).decode(errors="replace"), keep_blank_values=True)


def _link(request: Request):
    base = public_base(request)
    return lambda course: f"{base}/planung/{course.id}"


def _names(session) -> dict[str, str]:
    return {p.key: p.name for p in vocabulary.people(session)}


CHECK_SECONDS = 120  # a real calendar is checked at most this often on page views
_last_check = 0.0


def calendar_changed() -> None:
    """Check the calendar again on the next page view."""
    global _last_check
    _last_check = 0.0


def _refresh(session, request: Request) -> None:
    """Move held courses on and check the calendar (the demo calendar on every view)."""
    global _last_check
    service.advance(session, today())
    calendar = cal.get_calendar(session)
    if calendar.demo or time.monotonic() - _last_check > CHECK_SECONDS:
        try:
            courses = list(session.scalars(select(Course)))
            cal.check(session, calendar, courses, _names(session), today(), _link(request))
            _last_check = time.monotonic()
        except cal.CalendarError:
            pass  # shown on the review page when checking by hand
    session.commit()


def _flags_by_course(session, courses: list[Course]) -> dict[int, list[service.Flag]]:
    instance = get_instance()
    known = vocabulary.known(instance)
    names = _names(session)
    return {c.id: service.flags(c, courses, names, instance, known, today(), True) for c in courses}


def _me(session, login) -> str | None:
    for person in vocabulary.people(session):
        if person.login == login.user or person.name == login.user:
            return person.key
    return None


def _matches(course: Course, params, flags: list, me: str | None) -> bool:
    person = params.get("person")
    if person == "me":
        person = me or "-"
    if person and person not in course.all_keys:
        return False
    if params.get("from") and (not course.date or course.date.isoformat() < params["from"]):
        return False
    if params.get("to") and (not course.date or course.date.isoformat() > params["to"]):
        return False
    for name in ("context", "location", "category"):
        if params.get(name) and getattr(course, name) != params[name]:
            return False
    if params.get("flagged") and not flags:
        return False
    return not (params.get("unbilled") and course.billed)


@router.get("/planung", response_class=HTMLResponse)
def course_list(request: Request):
    login = current_login(request)
    if login is None:
        return RedirectResponse("/login", status_code=303)
    params = request.query_params
    stage = params.get("stage") or GEPLANT
    if stage not in STAGES:
        raise HTTPException(400, "Unbekannte Phase")
    with get_planning_sessionmaker()() as session:
        vocabulary.ensure_people(session)
        _refresh(session, request)
        courses = list(session.scalars(select(Course)))
        flags = _flags_by_course(session, courses)
        me = _me(session, login)
        visible = [c for c in courses if _matches(c, params, flags[c.id], me)]
        counts = {s: sum(1 for c in visible if c.stage == s) for s in STAGES}
        high = {
            s: sum(1 for c in visible if c.stage == s and service.top_severity(flags[c.id]) == "high")
            for s in STAGES
        }
        rows = [c for c in visible if c.stage == stage]
        upcoming = stage in (GEPLANT, BESTAETIGT)
        rows.sort(key=lambda c: (c.date or dt.date.max, c.start_time or dt.time()), reverse=not upcoming)
        return page(
            request,
            "planung_list.html",
            {
                "stage": stage,
                "stages": STAGES,
                "counts": counts,
                "high": high,
                "rows": rows,
                "flags": flags,
                "names": _names(session),
                "people": vocabulary.people(session),
                "me": me,
                "params": {k: params.get(k, "") for k in FILTERS},
                "filter_query": urlencode({k: params[k] for k in FILTERS if params.get(k)}),
                "suggest": vocabulary.suggestions(session, get_instance()),
                "severity_labels": SEVERITY_LABELS,
                "calendar_name": cal.get_calendar(session).name,
                "message": params.get("hinweis"),
            },
        )


def _course_page(request, session, course: Course, errors: list[str] | None = None, status_code: int = 200):
    instance = get_instance()
    courses = list(session.scalars(select(Course)))
    if course not in courses:
        courses.append(course)
    known = vocabulary.known(instance)
    names = _names(session)
    problems = rules.final_problems(course, instance, known) if course.stage == DURCHGEFUEHRT else []
    history = (
        list(
            session.scalars(
                select(History)
                .where(History.course_id == course.id)
                .order_by(History.at.desc(), History.id.desc())
            )
        )
        if course.id
        else []
    )
    final = (
        session.scalar(select(FinalSession).where(FinalSession.course_id == course.id)) if course.id else None
    )
    return page(
        request,
        "planung_course.html",
        {
            "course": course,
            "stages": STAGES,
            "editable": service.EDITABLE[course.stage],
            "flags": service.flags(course, courses, names, instance, known, today(), True)
            if course.id
            else [],
            "problems": problems,
            "blocking": [p for p in problems if p.blocking],
            "warnings": [p for p in problems if not p.blocking],
            "errors": errors or [],
            "people": vocabulary.people(session, active_only=True),
            "names": names,
            "leads": set(course.keys(LEAD)),
            "supports": set(course.keys(SUPPORT)),
            "suggest": vocabulary.suggestions(session, instance),
            "municipality_options": vocabulary.municipalities(session, instance),
            "wants_municipalities": rules.wants_municipalities(course, instance),
            "age_bands": rules.AGE_BANDS,
            "history": history,
            "final": final,
            "today": today(),
            "calendar_name": cal.get_calendar(session).name,
            "severity_labels": SEVERITY_LABELS,
            "message": request.query_params.get("hinweis"),
            "CAL_OK": CAL_OK,
        },
        status_code=status_code,
    )


@router.get("/planung/neu", response_class=HTMLResponse)
def new_course(request: Request):
    if current_login(request) is None:
        return RedirectResponse("/login", status_code=303)
    with get_planning_sessionmaker()() as session:
        vocabulary.ensure_people(session)
        course = Course(stage=GEPLANT, calendar_state="none", billed=False)
        course.alternatives, course.people, course.municipalities = [], [], []
        if day := request.query_params.get("date"):
            course.date = dt.date.fromisoformat(day)
        response = _course_page(request, session, course)
        session.rollback()
        return response


@router.post("/planung/neu", response_class=HTMLResponse)
async def create_course(request: Request):
    login = _login(request)
    form = await _form(request)
    with get_planning_sessionmaker()() as session:
        course = Course(stage=GEPLANT, created_by=login.user, calendar_state="none", billed=False)
        course.alternatives, course.people, course.municipalities = [], [], []
        try:
            service.apply_form(course, form, GEPLANT)
        except service.FormError as error:
            response = _course_page(request, session, course, error.messages, 400)
            session.rollback()
            return response
        session.add(course)
        session.flush()
        service.log(session, course.id, login.user, "angelegt")
        session.commit()
        return RedirectResponse(f"/planung/{course.id}", status_code=303)


def _get(session, course_id: int) -> Course:
    course = session.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "Kurs nicht gefunden")
    return course


@router.get("/planung/{course_id}", response_class=HTMLResponse)
def show_course(course_id: int, request: Request):
    if current_login(request) is None:
        return RedirectResponse("/login", status_code=303)
    with get_planning_sessionmaker()() as session:
        vocabulary.ensure_people(session)
        course = _get(session, course_id)
        _refresh(session, request)
        return _course_page(request, session, course)


@router.post("/planung/{course_id}", response_class=HTMLResponse)
async def save_course(course_id: int, request: Request):
    login = _login(request)
    form = await _form(request)
    with get_planning_sessionmaker()() as session:
        course = _get(session, course_id)
        before = service.snapshot(course)
        try:
            service.apply_form(course, form, course.stage)
        except service.FormError as error:
            response = _course_page(request, session, course, error.messages, 400)
            session.rollback()
            return response
        service.record_changes(session, course, before, login.user)
        session.commit()
        return RedirectResponse(f"/planung/{course.id}?hinweis={quote('Gespeichert.')}", status_code=303)


def _calendar_entry(session, request, course: Course, user: str) -> str:
    if course.stage not in (GEPLANT, BESTAETIGT):
        raise service.FormError(["Nur geplante und bestätigte Kurse kommen in den Kalender."])
    course.calendar_uid = course.calendar_uid or f"planung-{course.id}-{secrets.token_hex(4)}"
    event = cal.event_for(course, _names(session), _link(request)(course))
    if event is None:
        raise service.FormError(["Für den Kalender braucht es Datum, Beginn und Ende."])
    calendar = cal.get_calendar(session)
    existing = calendar.get(course.calendar_uid)
    calendar.put(event)
    course.calendar_state = CAL_OK
    service.log(
        session,
        course.id,
        user,
        "Kalender aktualisiert" if existing else "in Kalender eingetragen",
        None,
        event.summary,
    )
    return "Kalendereintrag aktualisiert." if existing else "In den Kalender eingetragen."


@router.post("/planung/{course_id}/aktion", response_class=HTMLResponse)
async def course_action(course_id: int, request: Request):
    login = _login(request)
    form = await _form(request)
    action = service._one(form, "action")
    with get_planning_sessionmaker()() as session:
        course = _get(session, course_id)
        message = "Erledigt."
        try:
            if action == "confirm":
                chosen = service._date(service._one(form, "chosen"), "Datum", [])
                service.confirm(session, course, chosen or course.date, login.user)
                message = "Bestätigt."
            elif action == "back":
                service.back_to_planned(session, course, login.user)
            elif action == "done":
                service.mark_done(session, course, login.user)
                message = "Als durchgeführt markiert."
            elif action == "undone":
                service.back_to_confirmed(session, course, login.user, today())
            elif action == "cancel":
                service.cancel(session, course, (form.get("reason") or [""])[0], login.user)
                message = "Abgesagt."
            elif action == "delete":
                if "really" not in form:
                    raise service.FormError(["Bitte das Löschen bestätigen."])
                service.delete(session, course, login.user)
                session.commit()
                return RedirectResponse(f"/planung?hinweis={quote('Kurs gelöscht.')}", status_code=303)
            elif action == "final":
                service.finalize(
                    session,
                    course,
                    get_instance(),
                    vocabulary.known(get_instance()),
                    login.user,
                    "checked" in form,
                )
                message = "Final bestätigt und (im Prototyp) in die Datenbank übernommen."
            elif action == "calendar":
                message = _calendar_entry(session, request, course, login.user)
                calendar_changed()
            elif action == "billed":
                before = service.snapshot(course)
                course.billed = "billed" in form
                service.record_changes(session, course, before, login.user)
                message = "Gespeichert."
            else:
                raise HTTPException(400, "Unbekannte Aktion")
        except (service.FormError, cal.CalendarError) as error:
            session.rollback()
            course = _get(session, course_id)
            messages = error.messages if isinstance(error, service.FormError) else [str(error)]
            return _course_page(request, session, course, messages, 400)
        session.commit()
        return RedirectResponse(f"/planung/{course.id}?hinweis={quote(message)}", status_code=303)


@router.get("/api/planung/belegt")
def busy(request: Request) -> JSONResponse:
    """Who is already booked at that time: {person key: course}."""
    _login(request)
    params = request.query_params
    try:
        day = dt.date.fromisoformat(params.get("date", ""))
    except ValueError:
        return JSONResponse({})

    def time(name):
        try:
            return dt.time.fromisoformat(params.get(name, ""))
        except ValueError:
            return None

    exclude = int(params["exclude"]) if params.get("exclude", "").isdigit() else None
    with get_planning_sessionmaker()() as session:
        courses = [
            c
            for c in session.scalars(select(Course).where(Course.date == day))
            if c.stage in (GEPLANT, BESTAETIGT, DURCHGEFUEHRT)
        ]
        return JSONResponse(busy_people(day, time("start"), time("end"), courses, exclude))


# --- review page --------------------------------------------------------------------------


def review_context(request: Request) -> dict:
    """The planning sections of the review page (admins)."""
    with get_planning_sessionmaker()() as session:
        vocabulary.ensure_people(session)
        _refresh(session, request)
        courses = list(session.scalars(select(Course)))
        flags = _flags_by_course(session, courses)
        calendar = cal.get_calendar(session)
        return {
            "high": [
                (c, flags[c.id])
                for c in sorted(courses, key=lambda c: c.date or dt.date.max)
                if service.top_severity(flags[c.id]) == "high"
            ],
            "waiting": sorted(
                (c for c in courses if c.stage == DURCHGEFUEHRT), key=lambda c: c.date or dt.date.max
            ),
            "orphans": list(
                session.scalars(
                    select(CalendarOrphan)
                    .where(CalendarOrphan.ignored_by.is_(None))
                    .order_by(CalendarOrphan.start)
                )
            ),
            "ignored": list(
                session.scalars(
                    select(CalendarOrphan)
                    .where(CalendarOrphan.ignored_by.is_not(None))
                    .order_by(CalendarOrphan.start)
                )
            ),
            "demo_events": list(session.scalars(select(DemoCalendarEvent).order_by(DemoCalendarEvent.start)))
            if calendar.demo
            else None,
            "calendar_name": calendar.name,
            "people": vocabulary.people(session),
            "finals": list(session.scalars(select(FinalSession).order_by(FinalSession.confirmed_at.desc()))),
            "names": _names(session),
            "stages": STAGES,
            "severity_labels": SEVERITY_LABELS,
            "message": request.query_params.get("hinweis"),
        }


def _back(message: str) -> RedirectResponse:
    return RedirectResponse(f"/review?hinweis={quote(message)}", status_code=303)


@router.post("/review/kalender/pruefen")
def check_calendar(request: Request):
    _admin(request)
    with get_planning_sessionmaker()() as session:
        try:
            courses = list(session.scalars(select(Course)))
            cal.check(session, cal.get_calendar(session), courses, _names(session), today(), _link(request))
            calendar_changed()
        except cal.CalendarError as error:
            return _back(str(error))
        session.commit()
    return _back("Kalender geprüft.")


@router.post("/review/kalender/ignorieren")
async def ignore_event(request: Request):
    login = _admin(request)
    uid = service._one(await _form(request), "uid")
    with get_planning_sessionmaker()() as session:
        orphan = session.get(CalendarOrphan, uid)
        if orphan:
            orphan.ignored_by = login.user
            session.commit()
    return _back("Termin wird nicht mehr gemeldet.")


@router.post("/review/kalender/kurs")
async def course_from_event(request: Request):
    login = _admin(request)
    uid = service._one(await _form(request), "uid")
    with get_planning_sessionmaker()() as session:
        orphan = session.get(CalendarOrphan, uid)
        if orphan is None:
            raise HTTPException(404)
        title, _, place = orphan.summary.partition(" · ")
        course = Course(
            stage=GEPLANT,
            date=orphan.start.date(),
            start_time=orphan.start.time(),
            end_time=orphan.end.time(),
            course_name=title or None,
            location=orphan.location or place or None,
            calendar_uid=uid,
            calendar_state=CAL_OK,
            created_by=login.user,
            billed=False,
        )
        session.add(course)
        session.flush()
        service.log(session, course.id, login.user, "aus Kalendertermin angelegt", None, orphan.summary)
        session.delete(orphan)
        session.commit()
        return RedirectResponse(
            f"/planung/{course.id}?hinweis={quote('Aus dem Kalendertermin angelegt; bitte ergänzen.')}",
            status_code=303,
        )


@router.post("/review/demo/kalender/loeschen")
async def demo_delete_event(request: Request):
    _admin(request)
    uid = service._one(await _form(request), "uid")
    with get_planning_sessionmaker()() as session:
        calendar = cal.get_calendar(session)
        if not calendar.demo:
            raise HTTPException(404)
        calendar.delete(uid)
        session.commit()
    return _back("Termin im Demo-Kalender gelöscht.")


@router.post("/review/demo/zuruecksetzen")
def demo_reset(request: Request):
    _admin(request)
    with get_planning_sessionmaker()() as session:
        vocabulary.ensure_people(session)
        demo.reset(session, get_instance(), today(), _link(request))
    calendar_changed()
    return _back("Demo-Daten neu angelegt.")


@router.post("/review/personen")
async def save_people(request: Request):
    _admin(request)
    form = await _form(request)
    with get_planning_sessionmaker()() as session:
        active = set(form.get("active", []))
        for person in vocabulary.people(session):
            person.active = person.key in active
            login = clean(form.get(f"login_{person.key}", [""])[0])
            person.login = login or None
        new_name = clean(form.get("new_name", [""])[0])
        if new_name:
            key = "p-" + secrets.token_hex(4)
            session.add(Person(key=key, name=new_name, active=True))
        session.commit()
    return _back("Personen gespeichert.")


def clean(value: str) -> str:
    return " ".join(value.split())
