"""Planning pages (WM-110): the course list, the course page and a new course.

- `GET /planung`: Termine grouped by week; stage tabs plus "Alle offenen" and "Alle" (every stage
  but Abgesagt, as the calendar's default) (`phase`), filters
  `person` (an ID or `ich` for the person linked to the login), `von`, `bis`, `kontext`, `ort`,
  `kategorie`, `hinweise` (`kritisch`, `wichtig`, `alle`) and `abrechnung` (`offen`, `ja`).
  A table on wide screens, cards on phones.
- `GET /planung/neu` and `POST /planung/neu`: a new course in Geplant, with one Termin or as a
  weekly series in one step (`reihe_rhythmus`, `reihe_anzahl`; WM-111).
- `GET /planung/{course_id}` (`?termin=` picks the Termin of a series; default the first open one):
  stage bar with the next action, the course form, the Termine, "Was fehlt noch", hints, calendar
  and Verlauf. `POST` saves the form (`umfang=folgende`: date, times, team and equipment for the
  following Termine too) and then, if `dann` names one, takes the next step (confirm, done, final);
  `POST .../aktion` the other actions (back, cancel, delete, billed, calendar).
- `POST /planung/{course_id}/termine`: "Termine hinzufügen" (weekly, every two weeks, or dates).
- `POST /planung/{course_id}/alle`: bulk actions over the course's Termine (`action`: `confirm`,
  `calendar`, `cancel`); the calendar gets one update per changed Termin.
- `POST /planung/{course_id}/kontakt`: the Kontaktperson of the course (WM-141, `metrics.contacts`):
  `action` `waehlen` (one of the place's contacts, `kontakt`), `neu` (add one to the place and choose
  it) or `entfernen`. Contact data never goes into an address or the page message.
- `POST /planung/{course_id}/korrektur`: propose a correction of an Abgeschlossen Termin with a
  reason (WM-132, `metrics.planning.corrections`); it changes nothing until an admin confirms it.
- `GET /planung/termin/{id}`: the course page of a Termin (links from hints and the calendar).
- `GET /api/planung/belegt?date=&start=&end=&exclude=`: who is booked at that time, for the team
  pickers (`{person ID: Termin}`).

All are Team routes (`access.py`); what a stage allows is checked in `metrics.planning.workflow`.
"""

import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field
from urllib.parse import parse_qs, quote, urlencode

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from metrics import contacts, courses, equipment, hints, lists
from metrics.db import (
    ABGESAGT,
    ABGESCHLOSSEN,
    ACTIVE_STATUSES,
    BESTAETIGT,
    CAL_OUTDATED,
    DURCHGEFUEHRT,
    GEPLANT,
    STAGES,
    Appointment,
    AppointmentOrigin,
    Contact,
    Context,
    Course,
    Equipment,
    Format,
    FormatEquipment,
    Person,
    Place,
    StoredHint,
    appointment_support,
    get_sessionmaker,
)
from metrics.instance import get_instance
from metrics.planning import caldav, corrections, series, workflow, workload
from metrics.planning.lanes import LEAD, SUPPORT
from metrics.planning.workflow import FormError
from metrics.web.common import TIMEZONE, current_login, page, public_base, today
from metrics.web.lists import picker_context

router = APIRouter()

OPEN = "offen"
ALL = "alle"  # every stage but Abgesagt, like the calendar's default (WM-114)
NOT_CANCELLED = tuple(s for s in STAGES if s != ABGESAGT)
TABS = [(OPEN, "Alle offenen"), (ALL, "Alle"), *((s, s) for s in STAGES)]
FILTER_KEYS = ("person", "von", "bis", "kontext", "ort", "kategorie", "hinweise", "abrechnung")
HINT_FILTERS = {
    "kritisch": (hints.KRITISCH,),
    "wichtig": (hints.KRITISCH, hints.WICHTIG),
    "alle": hints.LEVELS,
}
LIMIT = 300
LEVEL_ORDER = {level: i for i, level in enumerate(hints.LEVELS)}


async def _form(request: Request) -> workflow.Form:
    return parse_qs((await request.body()).decode(errors="replace"), keep_blank_values=True)


def _int(value: str | None) -> int | None:
    return int(value) if value and lists.is_whole(value) else None


def _date(value: str | None) -> dt.date | None:
    try:
        return dt.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _who(request: Request) -> str:
    return current_login(request).user


def _me(session: Session, request: Request) -> Person | None:
    login = current_login(request)
    return lists.person_for_login(session, login.subject) if login and login.subject else None


def _load_appointments(stmt):
    return stmt.options(
        selectinload(Appointment.course).selectinload(Course.format),
        selectinload(Appointment.course).selectinload(Course.context),
        selectinload(Appointment.course).selectinload(Course.place),
        selectinload(Appointment.frozen_format),
        selectinload(Appointment.frozen_context),
        selectinload(Appointment.frozen_place),
        selectinload(Appointment.lead),
        selectinload(Appointment.support),
        selectinload(Appointment.alternatives),
        selectinload(Appointment.equipment),
    )


@dataclass
class Row:
    """A Termin in the list with its open hints, most severe first."""

    t: Appointment
    hints: list[StoredHint] = field(default_factory=list)

    @property
    def top(self) -> StoredHint | None:
        return self.hints[0] if self.hints else None


def open_hints(session: Session, ids: list[int]) -> dict[int, list[StoredHint]]:
    """Stored hints of these Termine that still count (blocking, or not accepted), most severe first."""
    if not ids:
        return {}
    found: dict[int, list[StoredHint]] = defaultdict(list)
    stmt = (
        select(StoredHint)
        .where(StoredHint.appointment_id.in_(ids), StoredHint.blocking | ~hints._is_accepted())
        .order_by(StoredHint.id)
    )
    for h in session.scalars(stmt):
        found[h.appointment_id].append(h)
    for items in found.values():
        items.sort(key=lambda h: LEVEL_ORDER.get(h.level, 9))
    return found


# --- Course list ---------------------------------------------------------------------------------


def _filtered(session: Session, params, me: Person | None):
    """The list's filters as a select of Termine (without the stage); context, place and category are
    the Termin's own (WM-135: what it kept, else its course's)."""
    stmt = select(Appointment).join(Course, Course.id == Appointment.course_id)
    person = params.get("person") or ""
    person_id = (me.id if me else -1) if person == "ich" else _int(person)
    if person_id is not None:
        support = select(appointment_support.c.appointment_id).where(
            appointment_support.c.person_id == person_id
        )
        stmt = stmt.where((Appointment.lead_id == person_id) | Appointment.id.in_(support))
    if day := _date(params.get("von")):
        stmt = stmt.where(Appointment.date >= day)
    if day := _date(params.get("bis")):
        stmt = stmt.where(Appointment.date <= day)
    if context_id := _int(params.get("kontext")):
        stmt = stmt.where(Appointment.context_id == context_id)
    if place_id := _int(params.get("ort")):
        stmt = stmt.where(Appointment.place_id == place_id)
    if category := params.get("kategorie"):
        stmt = stmt.where(Appointment.format_id.in_(select(Format.id).where(Format.category == category)))
    if levels := HINT_FILTERS.get(params.get("hinweise") or ""):
        with_hint = select(StoredHint.appointment_id).where(
            StoredHint.level.in_(levels), StoredHint.blocking | ~hints._is_accepted()
        )
        stmt = stmt.where(Appointment.id.in_(with_hint))
    billing = params.get("abrechnung")
    if billing == "offen":
        stmt = stmt.where(
            Appointment.billed.is_(False), Appointment.stage.in_((DURCHGEFUEHRT, ABGESCHLOSSEN))
        )
    elif billing == "ja":
        stmt = stmt.where(Appointment.billed.is_(True))
    return stmt


def _stages(tab: str) -> tuple[str, ...]:
    return {OPEN: workflow.OPEN_STAGES, ALL: NOT_CANCELLED}.get(tab, (tab,))


def week_label(day: dt.date | None) -> str:
    if day is None:
        return "Ohne Datum"
    monday = day - dt.timedelta(days=day.weekday())
    sunday = monday + dt.timedelta(days=6)
    return f"KW {day.isocalendar().week} · {monday:%d.%m.}–{sunday:%d.%m.%Y}"


def by_week(rows: list[Row]) -> list[tuple[str, list[Row]]]:
    groups: list[tuple[str, list[Row]]] = []
    for row in rows:
        label = week_label(row.t.date)
        if groups and groups[-1][0] == label:
            groups[-1][1].append(row)
        else:
            groups.append((label, [row]))
    return groups


def _choices(session: Session):
    return {
        "contexts": list(
            session.scalars(select(Context).where(Context.status.in_(ACTIVE_STATUSES)).order_by(Context.name))
        ),
        "places": list(
            session.scalars(select(Place).where(Place.status.in_(ACTIVE_STATUSES)).order_by(Place.name))
        ),
        "categories": lists.categories(session),
        "people": lists.choices(session, lists.PEOPLE),
    }


@router.get("/planung", response_class=HTMLResponse)
def course_list(request: Request):
    params = request.query_params
    tab = params.get("phase") or OPEN
    if tab not in dict(TABS):
        raise HTTPException(400, "Unbekannte Phase")
    with get_sessionmaker()() as session:
        me = _me(session, request)
        base = _filtered(session, params, me)
        filtered = base.subquery()
        counted = session.execute(select(filtered.c.stage, func.count()).group_by(filtered.c.stage)).all()
        counts = dict(counted)
        for key in (OPEN, ALL):
            counts[key] = sum(counts.get(s, 0) for s in _stages(key))
        stages = _stages(tab)
        upcoming = tab in (OPEN, GEPLANT, BESTAETIGT, DURCHGEFUEHRT)
        order = Appointment.date.asc().nulls_last() if upcoming else Appointment.date.desc().nulls_last()
        stmt = _load_appointments(base.where(Appointment.stage.in_(stages))).order_by(
            order, Appointment.start_time.nulls_last(), Appointment.id
        )
        appointments = list(session.scalars(stmt.limit(LIMIT)))
        found = open_hints(session, [t.id for t in appointments])
        rows = [Row(t, found.get(t.id, [])) for t in appointments]
        query = urlencode([(k, params[k]) for k in FILTER_KEYS if params.get(k)])
        tabs = [
            (key, label, f"/planung?phase={quote(key)}" + (f"&{query}" if query else ""), counts.get(key, 0))
            for key, label in TABS
        ]
        return page(
            request,
            "planung_list.html",
            {
                "tab": tab,
                "tab_label": dict(TABS)[tab],
                "tabs": tabs,
                "groups": by_week(rows),
                "shown": len(rows),
                "total": counts.get(tab, 0),
                "limit": LIMIT,
                "params": {k: params.get(k, "") for k in FILTER_KEYS},
                "me": me,
                "badge": hints.BADGE,
                "titles": {t.id: workflow.title(t) for t in appointments},
                "message": params.get("hinweis"),
                **_choices(session),
            },
        )


# --- Course page ---------------------------------------------------------------------------------


def _get_course(session: Session, course_id: int) -> Course:
    course = session.scalars(
        select(Course)
        .where(Course.id == course_id)
        .options(
            selectinload(Course.appointments).selectinload(Appointment.support),
            selectinload(Course.appointments).selectinload(Appointment.origins),
            selectinload(Course.appointments).selectinload(Appointment.alternatives),
            selectinload(Course.appointments).selectinload(Appointment.calendar_responses),
            selectinload(Course.appointments).selectinload(Appointment.equipment),
        )
    ).first()
    if course is None:
        raise HTTPException(404, "Kurs nicht gefunden")
    return course


def _termine(course: Course) -> list[Appointment]:
    return courses.in_order(course.appointments)


def _selected(course: Course, wanted: int | None) -> Appointment:
    termine = _termine(course)
    if not termine:
        raise HTTPException(404, "Kurs ohne Termin")
    if wanted is not None:
        for t in termine:
            if t.id == wanted:
                return t
        raise HTTPException(404, "Termin nicht gefunden")
    return next((t for t in termine if t.stage in workflow.OPEN_STAGES), termine[0])


def _municipalities(session: Session) -> list[str]:
    instance = get_instance()
    names = {m.name for m in instance.map.municipalities} if instance.map else set()
    names |= set(session.scalars(select(AppointmentOrigin.municipality).distinct()))
    return sorted(names, key=str.casefold)


def _pickers(session: Session, course: Course) -> dict:
    types = lists.school_types(session)
    made = {}
    for kind, name, entry in (
        (lists.FORMATS, "format_id", course.format),
        (lists.CONTEXTS, "context_id", course.context),
        (lists.PLACES, "place_id", course.place),
    ):
        selected = entry.id if entry is not None else None
        made[name] = picker_context(kind, name, lists.choices(session, kind, selected), selected, types)
    return made


def _pickable(session: Session, t: Appointment) -> list[Person]:
    """Active team members, plus anyone already on the Termin who is no longer active."""
    people = {p.id: p for p in lists.choices(session, lists.PEOPLE)}
    for p in ([t.lead] if t.lead else []) + list(t.support):
        people.setdefault(p.id, p)
    return sorted(people.values(), key=lambda p: p.name.casefold())


@dataclass(frozen=True)
class Need:
    """One row of the Material section: an item, what this Termin needs, what the day needs."""

    item: Equipment
    quantity: int | None
    others: int  # what the other Termine of the day need

    @property
    def short(self) -> bool:
        total = (self.quantity or 0) + self.others
        return bool(self.quantity) and self.item.quantity is not None and total > self.item.quantity


def _equipment(session: Session, t: Appointment) -> list[Need]:
    """The Material section (WM-140): every active item, plus what the Termin needs already."""
    needs = equipment.needs_of(t.equipment)
    items = equipment.choices(session, needs)
    others = equipment.others_on(session, t.date, t.id) if t.date and items else {}
    return [Need(item, needs.get(item.id), others.get(item.id, 0)) for item in items]


def _format_needs(session: Session) -> dict[int, dict[int, int]]:
    """format ID -> equipment ID -> quantity, for the formats that need anything."""
    found: dict[int, dict[int, int]] = {}
    for row in session.scalars(select(FormatEquipment)):
        found.setdefault(row.format_id, {})[row.equipment_id] = row.quantity
    return found


STEPS = (GEPLANT, BESTAETIGT, DURCHGEFUEHRT, ABGESCHLOSSEN)
NEXT_ACTION = {
    GEPLANT: ("confirm", "Bestätigen"),
    BESTAETIGT: ("done", "Als durchgeführt markieren"),
    DURCHGEFUEHRT: ("final", "Final bestätigen"),
}


def course_context(
    request: Request,
    session: Session,
    course: Course,
    t: Appointment,
    *,
    new: bool = False,
    errors: list[str] | None = None,
) -> dict:
    termine = _termine(course) if not new else [t]
    ids = [a.id for a in termine if a.id]
    stored = open_hints(session, ids)
    all_stored = (
        list(session.scalars(select(StoredHint).where(StoredHint.appointment_id == t.id))) if t.id else []
    )
    decisions = {h.id: hints._accepted(session, t.id, h.rule, h.key) for h in all_stored if not h.blocking}
    accepted = {i for i, d in decisions.items() if d is not None}
    hint_rows = sorted(
        ((h, decisions.get(h.id)) for h in all_stored),
        key=lambda row: (row[1] is not None, LEVEL_ORDER[row[0].level]),
    )
    blocking, warnings = [], []
    if t.id and t.stage == DURCHGEFUEHRT:
        blocking = [p.text for p in lists.pending_entries(t)] + [
            f.text for f in hints.open_blocking(session, t)
        ]
        warnings = workflow.open_warnings(session, t)
    history = workflow.history(session, course) if not new else []
    editable = workflow.editable(t) if not new else workflow.EDITABLE[GEPLANT]
    later = series.following(t) if t.id and t.stage in series.PLANNING else []
    last = next((a for a in reversed(termine) if a.date), None)
    return {
        "course": course,
        "t": t,
        "new": new,
        "termine": termine,
        "numbered": sum(1 for a in termine if a.number is not None and a.stage != ABGESAGT),
        "later": later,
        "series": {
            "rhythms": [(key, text) for key, (text, _) in series.RHYTHMS.items()],
            "max": series.MAX_TERMINE,
            "next": (last.date + dt.timedelta(days=7)) if last else None,
            "to_confirm": [a for a in termine if a.stage == GEPLANT],
            "to_enter": [
                a
                for a in termine
                if a.stage in caldav.IN_CALENDAR
                and a.date
                and a.start_time
                and a.end_time
                and (not a.calendar_uid or a.calendar_state == "missing")
            ],
            "remaining": series.remaining(course, today()) if not new else [],
            "form": {},
        },
        "title": workflow.title_of_course(course) if not new else "Neuer Kurs",
        "steps": STEPS,
        "next_action": NEXT_ACTION.get(t.stage),
        "editable": editable,
        # Whether the form has anything to save (Abgeschlossen: only "Abgerechnet", saved apart).
        "form_on": new or t.stage in workflow.OPEN_STAGES,
        # Kept course values (WM-135): a change of the course leaves Abgeschlossen Termine as they are.
        "closed_termine": courses.has_closed(course) if not new else False,
        "course_open": courses.has_open(course) if not new else False,
        "kept": workflow.kept_differences(t) if not new else [],
        "kept_by_termin": {a.id: workflow.kept_differences(a) for a in termine if a.id},
        "pickers": _pickers(session, course),
        "pickable": _pickable(session, t),
        "equipment": _equipment(session, t),
        # A new course: what each format needs, so choosing one ticks its material (planning.js).
        "format_needs": _format_needs(session) if new else {},
        "class_kind": workflow.class_kind(course if workflow.COURSE in editable else t),
        "class_kinds": workflow.CLASS_KINDS,
        "at_school": workflow.AT_SCHOOL,
        "age_bands": workflow.AGE_BANDS,
        "school_places": {
            p.id: p.is_school for p in session.scalars(select(Place).where(Place.is_school.is_not(None)))
        },
        "participant_fields": workflow.PARTICIPANT_FIELDS,
        "workload": workload_context(course, t, new),
        "asks_municipalities": workflow.asks_municipalities(t),
        "municipalities": _municipalities(session),
        "hints_by_termin": stored,
        "hint_rows": hint_rows,
        "rules": hints.RULES,
        "checks": workflow.what_is_missing(t, all_stored, accepted) if t.id else [],
        "blocking": blocking,
        "warnings": warnings,
        "history": history,
        "contact": contact_context(session, course) if not new else None,
        "labels": workflow.LABELS,
        "badge": hints.BADGE,
        "today": today(),
        "timezone": TIMEZONE,
        "errors": errors or [],
        "message": request.query_params.get("hinweis"),
    }


def contact_context(session: Session, course: Course, values=None, errors=None) -> dict:
    """The Kontakt section: the chosen contact, the place's contacts and the form for a new one."""
    place = contacts.place_of(session, course)
    return {
        "place": place,
        "chosen": course.contact,
        "choices": contacts.for_place(session, place),
        "fields": contacts.FIELDS,
        "entered": values or {},
        "errors": errors or {},
    }


@dataclass
class WorkloadField:
    """One of the course's own workload values on the course page."""

    name: str
    label: str
    own: str  # the course's value as entered, "" if inherited
    inherited: str  # what applies when empty, e.g. "1 Std. (Format „HackDays“)"
    placeholder: str  # the inherited number alone


def workload_context(course: Course, t: Appointment, new: bool) -> dict:
    """The "Arbeitszeit" panel: the course's own values, what it inherits, and this Termin's estimate."""
    inherited = workload.values(course, own_values=False)
    fields = []
    for name, label, key in workflow.WORKLOAD_FIELDS:
        value, own = getattr(inherited, key), getattr(course, name)
        fields.append(
            WorkloadField(
                name,
                label,
                workload.exact(own) if own is not None else "",
                f"{workload.show(key, value)} ({value.origin})",
                workload.exact(value.amount),
            )
        )
    estimate = {}
    if not new and t.stage != ABGESAGT:
        estimate = {
            "Kursleitung": workload.number(workload.estimate(t, LEAD).total),
            "Unterstützung": workload.number(workload.estimate(t, SUPPORT).total),
        }
    return {"fields": fields, "own": any(f.own for f in fields), "estimate": estimate}


def _course_page(request, session, course, t, *, new=False, errors=None, status_code=200, extra=None):
    context = course_context(request, session, course, t, new=new, errors=errors) | (extra or {})
    context["calendar"] = calendar_context(request, t)
    context["correction"] = correction_context(session, t) if not new else {"on": False}
    return page(request, "planung_course.html", context, status_code=status_code)


def _new_objects(day: dt.date | None = None) -> tuple[Course, Appointment]:
    course = Course(target_group="Unbekannt")
    t = Appointment(course=course, stage=GEPLANT, number=1, date=day, billed=False, source="app")
    return course, t


@router.get("/planung/neu", response_class=HTMLResponse)
def new_course(request: Request):
    with get_sessionmaker()() as session:
        course, t = _new_objects(_date(request.query_params.get("date")))
        return _course_page(request, session, course, t, new=True)


@router.post("/planung/neu", response_class=HTMLResponse)
async def create_course(request: Request):
    form = await _form(request)
    with get_sessionmaker()() as session:
        try:
            t = series.create(session, form, _who(request))
        except FormError as error:
            session.rollback()
            course, t = _new_objects()
            with session.no_autoflush:
                workflow._apply_course(session, course, form, [])
                workflow._apply_workload(course, form, [])
                workflow._apply_appointment(session, t, form, workflow.EDITABLE[GEPLANT], [])
                response = _course_page(
                    request,
                    session,
                    course,
                    t,
                    new=True,
                    errors=error.messages,
                    status_code=400,
                    extra={"repeat": {k: workflow.one(form, k) for k in ("reihe_rhythmus", "reihe_anzahl")}},
                )
            session.rollback()
            return response
        session.commit()
        n = len(t.course.appointments)
        done = "Kurs angelegt." if n == 1 else f"Kurs mit {n} Terminen angelegt."
        return RedirectResponse(f"/planung/{t.course_id}?hinweis={quote(done)}", status_code=303)


@router.get("/planung/termin/{appointment_id:int}")
def show_appointment(appointment_id: int):
    with get_sessionmaker()() as session:
        t = session.get(Appointment, appointment_id)
        if t is None:
            raise HTTPException(404, "Termin nicht gefunden")
        return RedirectResponse(f"/planung/{t.course_id}?termin={t.id}", status_code=303)


@router.get("/planung/{course_id:int}", response_class=HTMLResponse)
def show_course(course_id: int, request: Request):
    with get_sessionmaker()() as session:
        course = _get_course(session, course_id)
        t = _selected(course, _int(request.query_params.get("termin")))
        return _course_page(request, session, course, t)


def _back(t: Appointment, message: str) -> RedirectResponse:
    return RedirectResponse(f"/planung/{t.course_id}?termin={t.id}&hinweis={quote(message)}", status_code=303)


def _refused(request, session, course_id: int, termin_id: int | None, error: FormError, form=None):
    """The course page again with the error; with `form`, the entered values stay in the fields."""
    session.rollback()
    course = _get_course(session, course_id)
    t = _selected(course, termin_id)
    with session.no_autoflush:
        if form is not None and (allowed := workflow.editable(t)):
            if workflow.COURSE in allowed:
                workflow._apply_course(session, course, form, [])
            if workflow.WORKLOAD in allowed:
                workflow._apply_workload(course, form, [])
            workflow._apply_appointment(session, t, form, allowed, [])
        response = _course_page(request, session, course, t, errors=error.messages, status_code=400)
    session.rollback()
    return response


THEN = {
    "confirm": "Bestätigt.",
    "done": "Als durchgeführt markiert.",
    "final": "Final bestätigt: zählt jetzt in den Kennzahlen.",
}


@router.post("/planung/{course_id:int}", response_class=HTMLResponse)
async def save_course(course_id: int, request: Request):
    """Save the form; then the next step if `dann` asks for one (the stage bar's main button)."""
    form = await _form(request)
    termin_id = _int(workflow.one(form, "termin"))
    then = workflow.one(form, "dann")
    by = _who(request)
    with get_sessionmaker()() as session:
        course = _get_course(session, course_id)
        t = _selected(course, termin_id)
        others: list[Appointment] = []
        try:
            if t.stage not in (ABGESCHLOSSEN, ABGESAGT):
                others = series.save(session, t, form, by, workflow.one(form, "umfang"))
            if then == "confirm":
                workflow.confirm(
                    session, t, workflow.parse_date(workflow.one(form, "chosen"), "Datum", []), by
                )
            elif then == "done":
                workflow.mark_done(session, t, by)
            elif then == "final":
                workflow.finalize(session, t, by, "checked" in form, today())
            elif then:
                raise HTTPException(400, "Unbekannte Aktion")
        except FormError as error:
            return _refused(request, session, course_id, termin_id, error, form)
        session.commit()
        note = _calendar_after(request, session, t, by)
        others_note = _calendar_many(request, session, others, by)
        also = f"Auch {len(others)} folgende Termine geändert." if others else ""
        return _back(t, " ".join(x for x in (THEN.get(then, "Gespeichert."), also, note, others_note) if x))


@router.post("/planung/{course_id:int}/aktion", response_class=HTMLResponse)
async def course_action(course_id: int, request: Request):
    form = await _form(request)
    action = workflow.one(form, "action")
    termin_id = _int(workflow.one(form, "termin"))
    by = _who(request)
    with get_sessionmaker()() as session:
        course = _get_course(session, course_id)
        t = _selected(course, termin_id)
        message = "Erledigt."
        try:
            if action == "back":
                workflow.back_to_planned(session, t, by)
                message = "Zurück auf „Geplant“."
            elif action == "undone":
                workflow.back_to_confirmed(session, t, by, today())
                message = "Zurück auf „Bestätigt“."
            elif action == "cancel":
                workflow.cancel(session, t, workflow.one(form, "reason"), by)
                message = "Abgesagt."
            elif action == "delete":
                if "really" in form and t.stage == GEPLANT:
                    message = _cancel_before_delete(request, session, t, by)
                workflow.delete(session, t, by, "really" in form)
                session.commit()
                done = " ".join(
                    x for x in ("Termin gelöscht.", message if message != "Erledigt." else "") if x
                )
                return RedirectResponse(f"/planung?hinweis={quote(done)}", status_code=303)
            elif action == "calendar":
                message = _enter_calendar(request, session, t, by)
            elif action == "billed":
                workflow.set_billed(session, t, "billed" in form, by)
                message = "Gespeichert."
            elif action == "workload":  # the "Arbeitszeit" panel of an Abgeschlossen Termin
                workflow.save(session, t, form, by, only={workflow.WORKLOAD})
                message = "Gespeichert."
            else:
                raise HTTPException(400, "Unbekannte Aktion")
        except FormError as error:
            return _refused(request, session, course_id, termin_id, error)
        session.commit()
        if action in ("back", "undone", "cancel"):
            note = _calendar_after(request, session, t, by)
            message = " ".join(x for x in (message, note) if x)
        return _back(t, message)


# --- Calendar ------------------------------------------------------------------------------------


def _calendar_after(request: Request, session: Session, t: Appointment, by: str) -> str | None:
    """After a committed change: bring the Termin's event up to date (only if it changes). A calendar
    that is down never loses the change: the event is marked outdated and the hint says so."""
    calendar = caldav.get_calendar()
    if calendar is None or not t.calendar_uid:
        return None
    try:
        note = caldav.push(session, calendar, t, public_base(request), by)
    except caldav.CalendarError as error:
        session.rollback()
        t = session.get(Appointment, t.id)
        t.calendar_state = CAL_OUTDATED
        hints.refresh(session, [t.id])
        note = f"Kalender nicht aktualisiert: {error}"
    session.commit()
    return note


def _calendar_many(request: Request, session: Session, termine: list[Appointment], by: str) -> str | None:
    """`_calendar_after` for several Termine: one update per Termin whose event changes."""
    updated, failed = 0, []
    for t in termine:
        note = _calendar_after(request, session, t, by)
        if note and note.startswith("Kalender nicht aktualisiert"):
            failed.append(series.label(t))
        elif note:
            updated += 1
    parts = []
    if updated:
        parts.append(f"Kalender: {updated} Einträge aktualisiert.")
    if failed:
        parts.append(f"Kalender nicht aktualisiert für {', '.join(failed)} (erscheint als Hinweis).")
    return " ".join(parts) or None


def _enter_calendar(request: Request, session: Session, t: Appointment, by: str) -> str:
    calendar = caldav.get_calendar()
    if calendar is None:
        raise FormError("Es ist kein Kalender eingerichtet (CALDAV_URL).")
    try:
        return caldav.enter(session, calendar, t, public_base(request), by)
    except caldav.CalendarError as error:
        raise FormError(str(error)) from error


def _cancel_before_delete(request: Request, session: Session, t: Appointment, by: str) -> str:
    """A deleted Termin's event is cancelled (the attendees are told), never deleted."""
    calendar = caldav.get_calendar()
    if calendar is None or not t.calendar_uid:
        return ""
    try:
        return caldav.cancel_event(session, calendar, t, public_base(request), by) or ""
    except caldav.CalendarError as error:
        return f"Kalendereintrag nicht abgesagt ({error}); er erscheint unter „Prüfen“."


@router.post("/planung/{course_id:int}/termine", response_class=HTMLResponse)
async def add_termine(course_id: int, request: Request):
    """ "Termine hinzufügen": weekly or every two weeks n times, or picked dates. The new Termine take
    the times and team of the Termin open on the page (`termin`)."""
    form = await _form(request)
    termin_id = _int(workflow.one(form, "termin"))
    with get_sessionmaker()() as session:
        course = _get_course(session, course_id)
        template = _selected(course, termin_id)
        errors: list[str] = []
        dates = series.parse_dates(form, errors)
        try:
            if errors:
                raise FormError(errors)
            added = series.add(session, course, dates, template, _who(request))
        except FormError as error:
            session.rollback()
            course = _get_course(session, course_id)
            t = _selected(course, termin_id)
            extra = {"add_form": {k: form.get(k, []) for k in ("rhythmus", "erster", "anzahl", "datum")}}
            return _course_page(
                request, session, course, t, errors=error.messages, status_code=400, extra=extra
            )
        session.commit()
        message = f"{len(added)} Termine hinzugefügt." if len(added) > 1 else "Termin hinzugefügt."
        if len(added) < len(dates):
            message += f" {len(dates) - len(added)} Tage hatten schon einen Termin."
        return _back(added[0], message)


def _bulk_message(done: str, result: series.Bulk) -> str:
    parts = [done] if result.done else []
    if result.deleted:
        parts.append(f"{len(result.deleted)} geplante Termine gelöscht.")
    if result.skipped:
        parts.append(
            "Nicht geändert: " + "; ".join(f"{series.label(t)} {why}" for t, why in result.skipped) + "."
        )
    return " ".join(parts) or "Nichts geändert."


@router.post("/planung/{course_id:int}/alle", response_class=HTMLResponse)
async def bulk_action(course_id: int, request: Request):
    """Bulk actions over the Termine of a course: `confirm` (alle bestätigen), `calendar` (alle in den
    Kalender eintragen), `cancel` (restliche absagen, with `reason`; `delete_planned` deletes the
    remaining Geplant ones). The calendar gets at most one write per Termin."""
    form = await _form(request)
    action = workflow.one(form, "action")
    termin_id = _int(workflow.one(form, "termin"))
    by = _who(request)
    with get_sessionmaker()() as session:
        course = _get_course(session, course_id)
        t = _selected(course, termin_id)
        try:
            if action == "confirm":
                result = series.confirm_all(session, course, by)
                message = _bulk_message(f"{len(result.done)} Termine bestätigt.", result)
                changed = result.done
            elif action == "calendar":
                session.commit()
                message = _enter_all(request, session, course, by)
                return _back(t, message)
            elif action == "cancel":
                planned = [a for a in series.remaining(course, today()) if a.stage == GEPLANT]
                delete_planned = "delete_planned" in form
                if not workflow.one(form, "reason"):
                    raise FormError("Bitte einen Grund für die Absage angeben.")
                notes = (
                    [_cancel_before_delete(request, session, a, by) for a in planned]
                    if delete_planned
                    else []
                )
                result = series.cancel_remaining(
                    session, course, workflow.one(form, "reason"), by, today(), delete_planned
                )
                message = _bulk_message(f"{len(result.done)} Termine abgesagt.", result)
                message = " ".join([message, *(n for n in notes if n)])
                changed = result.done
            else:
                raise HTTPException(400, "Unbekannte Aktion")
        except FormError as error:
            return _refused(request, session, course_id, termin_id, error)
        session.commit()
        note = _calendar_many(request, session, changed, by)
        message = " ".join(x for x in (message, note) if x)
        if session.get(Course, course_id) is None:
            return RedirectResponse(f"/planung?hinweis={quote(message)}", status_code=303)
        keep = t if session.get(Appointment, t.id) is not None else None
        if keep is None:
            return RedirectResponse(f"/planung/{course_id}?hinweis={quote(message)}", status_code=303)
        return _back(keep, message)


def _enter_all(request: Request, session: Session, course: Course, by: str) -> str:
    """ "Alle in den Kalender eintragen": one event per Termin that can have one and has none yet."""
    calendar = caldav.get_calendar()
    if calendar is None:
        raise FormError("Es ist kein Kalender eingerichtet (CALDAV_URL).")
    entered, failed = 0, []
    for a in courses.in_order(course.appointments):
        if a.stage not in caldav.IN_CALENDAR or not (a.date and a.start_time and a.end_time):
            continue
        if a.calendar_uid and a.calendar_state != "missing":
            continue
        try:
            caldav.enter(session, calendar, a, public_base(request), by)
            session.commit()  # each event that exists is linked at once
            entered += 1
        except caldav.CalendarError as error:
            session.rollback()
            failed.append(f"{series.label(a)}: {error}")
    parts = [f"{entered} Termine in den Kalender eingetragen."] if entered else []
    if failed:
        parts.append("Nicht eingetragen: " + "; ".join(failed) + ".")
    return " ".join(parts) or "Kein Termin zum Eintragen."


def calendar_context(request: Request, t: Appointment) -> dict:
    """The calendar box of the course page."""
    calendar = caldav.get_calendar()
    info = {
        "on": calendar is not None,
        "invitations": bool(calendar and calendar.organizer),
        "responses": caldav.responses(t) if t.id else [],
        "uninvited": [p.name for p in caldav.uninvited(t)] if t.id else [],
        "changes": None,
        "error": None,
        "has_times": bool(t.date and t.start_time and t.end_time),
        "stages": caldav.IN_CALENDAR,
        # The public calendar (WM-110): only when it is set up and the context asks for it.
        "public": bool(calendar and caldav.get_public_calendar() and t.context and t.context.public_calendar),
    }
    if calendar is not None and t.calendar_uid and t.calendar_state == CAL_OUTDATED:
        try:
            info["changes"] = caldav.preview(calendar, t, public_base(request))
        except caldav.CalendarError as error:
            info["error"] = str(error)
    return info


@router.post("/planung/{course_id:int}/akzeptieren", response_class=HTMLResponse)
async def accept_hint(course_id: int, request: Request):
    """Accept one warning of a Termin with a reason (team members too; Alex, 2026-11-13)."""
    form = await _form(request)
    termin_id = _int(workflow.one(form, "termin"))
    with get_sessionmaker()() as session:
        course = _get_course(session, course_id)
        t = _selected(course, termin_id)
        try:
            key = (form.get("schluessel") or [""])[0] or None
            reason = workflow.one(form, "grund")
            hints.accept(session, t.id, workflow.one(form, "regel"), reason, _who(request), key=key)
        except hints.HintError as error:
            return _refused(request, session, course_id, termin_id, FormError(str(error)))
        session.commit()
        return _back(t, "Hinweis akzeptiert.")


CONTACT_DONE = {
    "waehlen": "Kontaktperson gewählt.",
    "neu": "Kontaktperson angelegt und gewählt.",
    "entfernen": "Kontaktperson entfernt.",
}


@router.post("/planung/{course_id:int}/kontakt", response_class=HTMLResponse)
async def course_contact(course_id: int, request: Request):
    """Choose, add or remove the Kontaktperson of a course (WM-141). Works in every stage: the
    contact is planning data and never counted."""
    form = await _form(request)
    action = workflow.one(form, "action")
    termin_id = _int(workflow.one(form, "termin"))
    by = _who(request)
    if action not in CONTACT_DONE:
        raise HTTPException(400, "Unbekannte Aktion")
    with get_sessionmaker()() as session:
        course = _get_course(session, course_id)
        t = _selected(course, termin_id)
        values, errors = {}, {}
        try:
            if action == "waehlen":
                contact_id = _int(workflow.one(form, "kontakt"))
                contact = session.get(Contact, contact_id) if contact_id else None
                if contact is None:
                    raise contacts.ContactError("Bitte eine Kontaktperson wählen.")
                contacts.choose(session, course, contact, by)
            elif action == "neu":
                place = contacts.place_of(session, course)
                if place is None:
                    raise contacts.ContactError("Bitte zuerst einen Ort wählen.")
                values, errors = contacts.parse({k: workflow.one(form, k) for k, _ in contacts.FIELDS})
                if errors:
                    raise contacts.ContactError("Bitte die markierten Angaben prüfen.")
                contacts.choose(session, course, contacts.add(session, place, values, by), by)
            else:
                contacts.choose(session, course, None, by)
        except contacts.ContactError as error:
            session.rollback()
            course = _get_course(session, course_id)
            t = _selected(course, termin_id)
            shown = {k: workflow.one(form, k) for k, _ in contacts.FIELDS} if action == "neu" else None
            extra = {"contact": contact_context(session, course, shown, errors), "contact_open": True}
            return _course_page(
                request, session, course, t, errors=[str(error)], status_code=400, extra=extra
            )
        session.commit()
        return _back(t, CONTACT_DONE[action])


@router.post("/planung/{course_id:int}/korrektur", response_class=HTMLResponse)
async def propose_correction(course_id: int, request: Request):
    """Propose a correction of an Abgeschlossen Termin (WM-132); an admin decides in "Prüfen"."""
    form = await _form(request)
    termin_id = _int(workflow.one(form, "termin"))
    with get_sessionmaker()() as session:
        course = _get_course(session, course_id)
        t = _selected(course, termin_id)
        try:
            corrections.propose(session, t, form, workflow.one(form, "grund"), _who(request))
        except FormError as error:
            return _refused(request, session, course_id, termin_id, error)
        session.commit()
        return _back(t, "Korrektur vorgeschlagen: sie gilt, sobald ein Admin sie bestätigt.")


def correction_context(session: Session, t: Appointment) -> dict:
    """The open corrections of a Termin and the choices of the correction form (Abgeschlossen)."""
    if not t.id or t.stage != ABGESCHLOSSEN:
        return {"on": False}
    course = t.course

    def options(kind, selected):
        return [(e.id, lists.label(e)) for e in lists.choices(session, kind, selected)]

    return {
        "on": True,
        "open": [(p, corrections.changes(p)) for p in corrections.open_for(session, [t.id])],
        "entries": [
            ("format", "Format", options(lists.FORMATS, t.format_id), t.format_id),
            ("context", "Kontext", options(lists.CONTEXTS, t.context_id), t.context_id),
            ("place", "Ort", options(lists.PLACES, t.place_id), t.place_id),
        ],
        "people": _pickable(session, t),
        "only_total": corrections.only_total(t),
        "shared": len(course.appointments) > 1,
        # Herkunft: shown when the context asks for it or the Termin has some; two empty rows for a
        # further municipality, so the form works without JS.
        "origins_on": workflow.asks_municipalities(t) or bool(t.origins),
        "origins": sorted(t.origins, key=lambda o: o.municipality),
        "free_rows": 2,
        "municipalities": _municipalities(session),
    }


@router.get("/api/planung/belegt")
def busy(request: Request) -> JSONResponse:
    """Who is already booked at that time: {person ID: Termin}."""
    params = request.query_params
    day = _date(params.get("date"))
    if day is None:
        return JSONResponse({})

    def time(name):
        try:
            return dt.time.fromisoformat(params.get(name) or "")
        except ValueError:
            return None

    with get_sessionmaker()() as session:
        found = workflow.busy_people(session, day, time("start"), time("end"), _int(params.get("exclude")))
    return JSONResponse({str(k): v for k, v in found.items()})
