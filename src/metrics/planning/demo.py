"""Demo data for the planning prototype: courses relative to today that show every flag.

Uses the instance's people, places, contexts and course names where available, so the demo
looks familiar; everything is written into the planning database only.
"""

import datetime as dt
import itertools
import secrets

from sqlalchemy.orm import Session

from metrics.instance import Instance
from metrics.planning import service
from metrics.planning.calendar import Event, event_for, get_calendar
from metrics.planning.models import (
    ABGESCHLOSSEN,
    BESTAETIGT,
    CAL_OK,
    DURCHGEFUEHRT,
    GEPLANT,
    LEAD,
    SUPPORT,
    AlternativeDate,
    Course,
    CoursePerson,
    History,
    Municipality,
    PlanningBase,
)
from metrics.planning.vocabulary import people, rebuild_people, suggestions

DEMO_USER = "Demo-Daten"


def _pick(values: list[str], wanted: list[str], fallback: list[str]) -> list[str]:
    chosen = [v for v in wanted if v in values]
    chosen += [v for v in values if v not in chosen]
    return (chosen + fallback)[: max(len(fallback), 1)]


def reset(session: Session, instance: Instance, today: dt.date, link) -> None:
    """Delete all planning data except the entered places and seed the demo courses."""
    engine = session.get_bind()
    session.close()
    for table in reversed(PlanningBase.metadata.sorted_tables):
        if table.name not in ("people", "places"):
            with engine.begin() as connection:
                connection.execute(table.delete())
    rebuild_people(session)
    _seed(session, instance, today, link)


def _seed(session: Session, instance: Instance, today: dt.date, link) -> None:
    team = [p.key for p in people(session, active_only=True)]
    team = (team + ["demo-a", "demo-b", "demo-c", "demo-d"])[:6]
    p1, p2, p3, p4, p5, p6 = (team + team)[:6]
    names = {p.key: p.name for p in people(session)}
    s = suggestions(session, instance)
    schools = [name for name, school in instance.locations.items() if school]
    places = _pick(
        s["location"],
        schools[:3],
        ["Grundschule Musterstadt", "Mittelschule Beispiel", "Makerspace", "Gymnasium Probe"],
    )
    contexts = _pick(s["context"], [], ["Projekt A", "Ferien", "Kurse"])
    project_names = {p.title for p in instance.projects} | {c for p in instance.projects for c in p.contexts}
    contexts = [c for c in contexts if c not in project_names] or contexts
    names_ = [n for n in s["course_name"] if n not in project_names]
    courses_ = _pick(names_, [], ["Löten", "Scratch", "3D-Druck", "Calliope mini"])
    categories = instance.theme.category_order or ["Code", "Engineering", "Science"]
    jugendlab = next((p for p in instance.projects if p.participant_municipalities), None)
    mun_names = (
        [m.name for m in instance.map.municipalities][:3] if instance.map else ["Musterstadt", "Beispieldorf"]
    )

    serial = itertools.count()  # varies context and class from course to course

    def day(offset: int) -> dt.date:
        """The date `offset` working days from today (courses fall on weekdays)."""
        date, step = today, (1 if offset >= 0 else -1)
        for _ in range(abs(offset)):
            date += dt.timedelta(days=step)
            while date.weekday() >= 5:
                date += dt.timedelta(days=step)
        return date

    def t(text: str) -> dt.time:
        return dt.time.fromisoformat(text)

    def course(stage, offset, start, end, lead, support=(), **fields) -> Course:
        c = Course(
            stage=stage,
            date=day(offset) if offset is not None else None,
            start_time=t(start) if start else None,
            end_time=t(end) if end else None,
            context=fields.pop("context", contexts[next(serial) % len(contexts)]),
            location=fields.pop("location", places[0]),
            class_kind=fields.pop("class_kind", "klasse"),
            class_level=fields.pop("class_level", 3 + next(serial) % 6),
            age=fields.pop("age", None),
            created_by=DEMO_USER,
            **fields,
        )
        c.people = [CoursePerson(person_key=k, role=LEAD) for k in lead] + [
            CoursePerson(person_key=k, role=SUPPORT) for k in support
        ]
        if c.start_time and c.end_time:  # Dauer is always end minus start
            start = dt.datetime.combine(c.date or today, c.start_time)
            c.duration_hours = (dt.datetime.combine(c.date or today, c.end_time) - start).seconds / 3600
        session.add(c)
        session.flush()
        session.add(History(course_id=c.id, user=DEMO_USER, action="angelegt"))
        return c

    full = lambda n, cat=0: {  # noqa: E731
        "course_name": courses_[n % len(courses_)],
        "category": categories[cat % len(categories)],
        "session_number": 1,
    }

    # The configured calendar (CalDAV test calendar or the demo calendar). Each reset uses new
    # UIDs, because course numbers start again and the app never deletes calendar events.
    calendar = get_calendar(session)
    run = secrets.token_hex(3)

    def into_calendar(c: Course, shift_hours: int = 0) -> None:
        c.calendar_uid = f"planung-{c.id}-{run}"
        event = event_for(c, names, link(c))
        event.start += dt.timedelta(hours=shift_hours)
        event.end += dt.timedelta(hours=shift_hours)
        calendar.put(event)
        c.calendar_state = CAL_OK

    # In three days: a confirmed course, a double booking (high), same place at the same time
    # (medium) and another course the same day (low).
    a = course(BESTAETIGT, 3, "08:00", "11:30", [p1], [p2], location=places[0], **full(0))
    into_calendar(a)
    course(GEPLANT, 3, "10:00", "12:00", [p1], location=places[1], context=contexts[1 % len(contexts)])
    b = course(BESTAETIGT, 3, "09:00", "10:30", [p4], location=places[0], **full(1, 1))
    into_calendar(b)
    course(GEPLANT, 3, "13:00", "15:00", [p3], location=places[2])

    # A place that is not in the location list yet: its address is still missing.
    course(
        GEPLANT,
        15,
        "14:00",
        "16:00",
        [p4],
        location="Jugendzentrum Neustadt",
        class_kind="band",
        class_level=None,
        age=None,
        age_band="19 - 24 Jahre",
    )

    # Planned with alternatives: no conflict on the main date, a low one on an alternative.
    alt = course(
        GEPLANT,
        10,
        "09:00",
        "12:00",
        [p2],
        [p5],
        location=places[1],
        note="Schule meldet sich, welcher Termin passt.",
    )
    alt.alternatives = [AlternativeDate(date=day(3)), AlternativeDate(date=day(17))]

    # Calendar cases: event deleted in the calendar, event moved, not yet in the calendar.
    gone = course(BESTAETIGT, 8, "14:00", "16:00", [p3], location=places[2], **full(2, 2))
    gone.calendar_uid = f"planung-{gone.id}-{run}"  # "was entered, then deleted": the event is absent
    moved = course(BESTAETIGT, 12, "09:00", "11:00", [p5], [p6], location=places[1], **full(3))
    into_calendar(moved, shift_hours=1)
    course(BESTAETIGT, 20, "08:30", "12:30", [p6], [p1], location=places[3 % len(places)], **full(1, 1))

    # A future calendar event without a course.
    calendar.put(
        Event(
            uid=f"extern-lasercutter-{run}",
            summary="Lasercutter-Workshop · Makerspace",
            start=dt.datetime.combine(day(6), t("17:00")),
            end=dt.datetime.combine(day(6), t("20:00")),
            location="Makerspace",
        )
    )

    # Past: planned but never confirmed (high); confirmed and now moved automatically, with
    # missing numbers (high); held and complete, ready for the final confirmation.
    course(GEPLANT, -1, "09:00", "11:00", [p4], location=places[1])
    course(BESTAETIGT, -2, "08:00", "12:00", [p2], [p3], location=places[0], **full(0))
    course(
        DURCHGEFUEHRT,
        -5,
        "08:00",
        "11:00",
        [p1],
        [p4],
        location=places[2],
        participants_female=11,
        participants_male=12,
        **full(2, 2),
    )

    if jugendlab:
        jl = course(
            DURCHGEFUEHRT,
            -7,
            "17:00",
            "21:00",
            [p5],
            [p6],
            context=jugendlab.contexts[0],
            course_name=jugendlab.title,
            location=places[3 % len(places)],
            class_kind="klasse",
            class_level=7,
            category=categories[1 % len(categories)],
            session_number=1,
            participants_female=4,
            participants_male=6,
        )
        jl.municipalities = [
            Municipality(municipality=mun_names[0], female=2, male=3),
            Municipality(municipality=mun_names[1 % len(mun_names)], female=1, male=2),
        ]

    # Cancelled with the event still in the calendar; finalized but not yet billed.
    gone_course = course(BESTAETIGT, 5, "09:00", "12:00", [p6], location=places[1], **full(3))
    into_calendar(gone_course)
    service.cancel(session, gone_course, "Schule hat wegen Krankheitswelle abgesagt", DEMO_USER)
    done = course(
        DURCHGEFUEHRT,
        -14,
        "08:00",
        "10:00",
        [p3],
        location=places[0],
        participants_female=9,
        participants_male=10,
        **full(1, 1),
    )
    session.flush()
    try:
        service.finalize(session, done, instance, {}, DEMO_USER, checked=True)
    except service.FormError:
        done.stage = ABGESCHLOSSEN  # the demo instance lacks something the rules want; show it anyway
    session.commit()

    service.advance(session, today)  # the confirmed course two days ago moves on its own
    session.commit()
