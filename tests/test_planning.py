"""Planning prototype: conflicts, rules, stages, final confirmation, calendar, pages."""

import datetime as dt
import json

import pytest

from metrics.planning import calendar as cal
from metrics.planning import conflicts, rules, service
from metrics.planning.models import (
    ABGESAGT,
    ABGESCHLOSSEN,
    BESTAETIGT,
    CAL_MISSING,
    CAL_OK,
    CAL_OUTDATED,
    DURCHGEFUEHRT,
    GEPLANT,
    LEAD,
    SUPPORT,
    AlternativeDate,
    CalendarOrphan,
    Course,
    CoursePerson,
    FinalSession,
    get_planning_sessionmaker,
)

TODAY = dt.date(2026, 11, 11)


def make(id_, day, start=None, end=None, lead=(), support=(), location="Makerspace", **fields) -> Course:
    course = Course(
        id=id_,
        stage=fields.pop("stage", GEPLANT),
        date=day,
        start_time=dt.time.fromisoformat(start) if start else None,
        end_time=dt.time.fromisoformat(end) if end else None,
        location=location,
        calendar_state="none",
        billed=False,
        **fields,
    )
    course.people = [CoursePerson(person_key=k, role=LEAD) for k in lead] + [
        CoursePerson(person_key=k, role=SUPPORT) for k in support
    ]
    course.alternatives, course.municipalities = [], []
    return course


DAY = dt.date(2026, 11, 20)


@pytest.mark.parametrize(
    ("other", "severity"),
    [
        (
            make(2, DAY, "10:00", "12:00", lead=["kim"], location="Schule"),
            conflicts.HIGH,
        ),  # same person, overlap
        (
            make(2, DAY, None, None, support=["kim"], location="Schule"),
            conflicts.MEDIUM,
        ),  # same person, no time
        (make(2, DAY, "10:00", "12:00", lead=["sam"]), conflicts.MEDIUM),  # same place, overlap
        (make(2, DAY, "10:00", "12:00", lead=["sam"], location="Schule"), conflicts.MEDIUM),  # overlap only
        (
            make(2, DAY, "13:00", "15:00", lead=["kim"], location="Schule"),
            conflicts.LOW,
        ),  # same day, no overlap
    ],
)
def test_conflict_severity(other, severity):
    course = make(1, DAY, "09:00", "11:00", lead=["kim"])
    found = conflicts.conflicts_for(course, [course, other], {"kim": "Kim"})
    assert [c.severity for c in found] == [severity]


def test_conflict_on_alternative_date_is_low():
    course = make(1, DAY + dt.timedelta(days=7), "09:00", "11:00", lead=["kim"])
    course.alternatives = [AlternativeDate(date=DAY)]
    other = make(2, DAY, "09:00", "11:00", lead=["kim"])
    (found,) = conflicts.conflicts_for(course, [other], {})
    assert (found.severity, found.alternative, found.date) == (conflicts.LOW, True, DAY)


def test_busy_people_respects_time():
    booked = make(2, DAY, "09:00", "11:00", lead=["kim"], support=["sam"])
    later = conflicts.busy_people(DAY, dt.time(12), dt.time(13), [booked], exclude=None)
    overlapping = conflicts.busy_people(DAY, dt.time(10), dt.time(12), [booked], exclude=None)
    assert later == {} and set(overlapping) == {"kim", "sam"}


def _form(**values) -> dict[str, list[str]]:
    return {k: v if isinstance(v, list) else [v] for k, v in values.items()}


BASIC = _form(
    date="2026-11-20", start_time="09:00", end_time="11:30", context="Projekt A",
    location="Grundschule Musterstadt", class_kind="klasse", class_level="3", lead="kim",
)  # fmt: skip


def test_planned_accepts_anything_but_confirm_needs_the_basics(instance):
    course = make(1, None, location=None)
    service.apply_form(course, _form(context="Projekt A"), GEPLANT)  # nothing enforced
    session = get_planning_sessionmaker()()
    session.add(course)
    session.flush()
    with pytest.raises(service.FormError) as error:
        service.confirm(session, course, None, "Alex")
    assert "Datum" in error.value.messages[0] and "Ort" in error.value.messages[0]
    service.apply_form(course, BASIC, GEPLANT)
    service.confirm(session, course, None, "Alex")
    assert course.stage == BESTAETIGT
    with pytest.raises(service.FormError):  # required fields stay required once confirmed
        service.apply_form(course, {**BASIC, "location": [""]}, BESTAETIGT)


def test_confirm_picks_one_of_the_alternative_dates(instance):
    course = make(1, None)
    service.apply_form(course, {**BASIC, "alt_date": ["2026-11-27", "2026-12-04"]}, GEPLANT)
    session = get_planning_sessionmaker()()
    session.add(course)
    session.flush()
    with pytest.raises(service.FormError):
        service.confirm(session, course, dt.date(2026, 12, 24), "Alex")
    service.confirm(session, course, dt.date(2026, 11, 27), "Alex")
    assert (course.date, course.alternatives) == (dt.date(2026, 11, 27), [])


def test_cancel_only_confirmed_and_delete_only_planned(instance):
    session = get_planning_sessionmaker()()
    planned = make(None, DAY)
    session.add(planned)
    session.flush()
    with pytest.raises(service.FormError):
        service.cancel(session, planned, "krank", "Alex")
    planned.stage = BESTAETIGT
    with pytest.raises(service.FormError):
        service.delete(session, planned, "Alex")
    with pytest.raises(service.FormError):
        service.cancel(session, planned, " ", "Alex")  # needs a reason
    service.cancel(session, planned, "Schule abgesagt", "Alex")
    assert planned.stage == ABGESAGT


def test_confirmed_courses_move_on_the_day_after(instance):
    session = get_planning_sessionmaker()()
    past = make(None, TODAY - dt.timedelta(days=1), stage=BESTAETIGT)
    planned_past = make(None, TODAY - dt.timedelta(days=1))
    today = make(None, TODAY, stage=BESTAETIGT)
    session.add_all([past, planned_past, today])
    session.flush()
    assert service.advance(session, TODAY) == 1
    assert (past.stage, planned_past.stage, today.stage) == (DURCHGEFUEHRT, GEPLANT, BESTAETIGT)
    flags = service.flags(planned_past, [], {}, instance, {}, TODAY, False)
    assert flags[0].severity == conflicts.HIGH and "nie bestätigt" in flags[0].text


def _held(**extra) -> Course:
    course = make(None, DAY, stage=DURCHGEFUEHRT)
    full = {
        **BASIC,
        **_form(course_name="Löten", category="Engineering", session_number="1"),
        **_form(participants_female="10", participants_male="12"),
    }
    service.apply_form(course, {**full, **extra}, DURCHGEFUEHRT)
    return course


def test_final_confirmation_needs_everything(instance):
    course = _held(participants_male=[""])
    problems = [p.text for p in rules.final_problems(course, instance, {}) if p.blocking]
    assert problems == ["Fehlt: TN männlich"]
    session = get_planning_sessionmaker()()
    session.add(course)
    session.flush()
    with pytest.raises(service.FormError):
        service.finalize(session, course, instance, {}, "Alex", checked=False)


def test_final_confirmation_builds_the_session_record(instance):
    course = _held()
    assert course.duration_hours == 2.5  # from Beginn and Ende
    session = get_planning_sessionmaker()()
    session.add(course)
    session.flush()
    final = service.finalize(session, course, instance, {}, "Alex", checked=False)
    record = json.loads(final.record_json)
    assert course.stage == ABGESCHLOSSEN
    assert (final.target_group, final.participants_total) == ("Schulklasse", 22)
    assert record["class_age_text"] == "Kl. 3" and record["instructor"] == "kim"
    assert record["source_key"] == f"planung:{course.id}" and "billed" not in record


def test_warnings_must_be_checked(instance):
    course = _held(course_name=["Loeten"])
    vocabulary = {"course_name": {"Löten"}, "context": {"Projekt A"}, "location": set()}
    session = get_planning_sessionmaker()()
    session.add(course)
    session.flush()
    with pytest.raises(service.FormError) as error:
        service.finalize(session, course, instance, vocabulary, "Alex", checked=False)
    assert "Hinweise" in error.value.messages[0]
    service.finalize(session, course, instance, vocabulary, "Alex", checked=True)


def test_project_municipalities_must_add_up(instance):
    from dataclasses import replace

    from metrics.instance import Project

    instance = replace(instance, projects=[Project("a", "Projekt A", ("Projekt A",), True)])
    course = _held(mun_name=["Musterstadt"], mun_female=["10"], mun_male=["11"])
    texts = [p.text for p in rules.final_problems(course, instance, {})]
    assert any("Herkunft ergibt 10 w / 11 m" in t for t in texts)


def test_ics_round_trip():
    event = cal.Event(
        "planung-1",
        "Löten · Schule, Raum 3",
        dt.datetime(2026, 11, 20, 9),
        dt.datetime(2026, 11, 20, 11, 30),
        "Schule; Raum 3",
        "Zeile 1\nZeile 2",
    )
    (parsed,) = cal.parse_ics(cal.to_ics(event))
    assert parsed == event


def test_ics_reads_tzid_and_folded_lines():
    text = (
        "BEGIN:VEVENT\r\nUID:x\r\nSUMMARY:Lang\r\n er Titel\r\n"
        "DTSTART;TZID=Europe/Berlin:20260701T090000\r\nDTEND;TZID=Europe/Berlin:20260701T100000\r\n"
        "END:VEVENT\r\n"
    )
    (event,) = cal.parse_ics(text)
    assert (event.summary, event.start, event.end) == (
        "Langer Titel",
        dt.datetime(2026, 7, 1, 9),
        dt.datetime(2026, 7, 1, 10),
    )


def test_calendar_check_finds_missing_outdated_and_orphans():
    session = get_planning_sessionmaker()()
    calendar = cal.DemoCalendar(session)
    link = lambda c: f"http://test/planung/{c.id}"  # noqa: E731
    courses = [make(None, DAY, "09:00", "11:00", stage=BESTAETIGT) for _ in range(3)]
    session.add_all(courses)
    session.flush()
    ok, missing, moved = courses
    for course in courses:
        course.calendar_uid = f"planung-{course.id}"
        calendar.put(cal.event_for(course, {}, link(course)))
    calendar.delete(missing.calendar_uid)
    moved.start_time = dt.time(10)
    calendar.put(
        cal.Event("extern", "Fremder Termin", dt.datetime(2026, 11, 25, 17), dt.datetime(2026, 11, 25, 19))
    )
    session.flush()
    cal.check(session, calendar, courses, {}, TODAY, link)
    assert (ok.calendar_state, missing.calendar_state, moved.calendar_state) == (
        CAL_OK,
        CAL_MISSING,
        CAL_OUTDATED,
    )
    session.flush()
    assert [o.uid for o in session.query(CalendarOrphan)] == ["extern"]


# --- pages ------------------------------------------------------------------------------------


@pytest.fixture
def team(monkeypatch):
    from tests.test_web import _client

    return _client(monkeypatch, auth_mode="off")  # "off" counts as admin


def test_planning_pages_need_login(monkeypatch):
    from tests.test_web import _client

    client = _client(monkeypatch)
    assert client.get("/planung", follow_redirects=False).headers["location"] == "/login"
    assert client.post("/planung/neu", data={"date": "2026-11-20"}).status_code == 401


def test_create_confirm_and_list(team):
    created = team.post(
        "/planung/neu",
        data={
            "date": "2030-03-04",
            "start_time": "09:00",
            "end_time": "11:00",
            "course_name": "Löten",
            "lead": "demo-a",
        },
        follow_redirects=False,
    )
    assert created.status_code == 303
    url = created.headers["location"]
    page = team.get(url).text
    assert "Löten" in page and "Bestätigen" in page and "In Kalender eintragen" in page

    refused = team.post(f"{url}/aktion", data={"action": "confirm"})
    assert refused.status_code == 400 and "Zum Bestätigen fehlt" in refused.text

    listing = team.get("/planung?person=demo-a").text
    assert "Löten" in listing and ">Alex<" in listing
    assert "Löten" not in team.get("/planung?person=demo-b").text

    assert team.post(f"{url}/aktion", data={"action": "calendar"}, follow_redirects=False).status_code == 303
    assert "Im Kalender eingetragen" in team.get(url).text

    assert team.post(f"{url}/aktion", data={"action": "delete"}).status_code == 400  # needs the confirmation
    assert (
        team.post(
            f"{url}/aktion", data={"action": "delete", "really": "1"}, follow_redirects=False
        ).status_code
        == 303
    )
    assert team.get(url).status_code == 404


def test_availability_endpoint(team):
    team.post(
        "/planung/neu",
        data={
            "date": "2030-03-04",
            "start_time": "09:00",
            "end_time": "11:00",
            "course_name": "A",
            "lead": "demo-a",
        },
    )
    busy = team.get("/api/planung/belegt?date=2030-03-04&start=10:00&end=12:00").json()
    assert list(busy) == ["demo-a"] and busy["demo-a"].startswith("A (09:00")


def test_demo_data_and_review_page(team):
    assert team.post("/review/demo/zuruecksetzen", follow_redirects=False).status_code == 303
    review = team.get("/review").text
    for text in ("Kritische Hinweise", "Kalender ohne Kurs", "Lasercutter-Workshop"):
        assert text in review
    with get_planning_sessionmaker()() as session:
        stages = {c.stage for c in session.query(Course)}
        assert {GEPLANT, BESTAETIGT, DURCHGEFUEHRT, ABGESCHLOSSEN, ABGESAGT} <= stages
        assert session.query(FinalSession).count() == 1
    for stage in ("geplant", "bestaetigt", "durchgefuehrt", "abgeschlossen", "abgesagt"):
        assert team.get(f"/planung?stage={stage}").status_code == 200
    with get_planning_sessionmaker()() as session:
        ids = [c.id for c in session.query(Course)]
    for course_id in ids:
        assert team.get(f"/planung/{course_id}").status_code == 200


def test_planned_takes_details_but_not_participants_and_dauer_follows_the_times(instance):
    course = make(1, None)
    course.billed = True
    form = {**BASIC, **_form(participants_female="3", category="Code", duration_hours="7")}
    service.apply_form(course, form, GEPLANT)
    assert (course.participants_female, course.category) == (None, "Code")  # Teilnehmende: from Durchgeführt
    assert course.billed is True  # Abrechnung is not part of the form before Durchgeführt
    assert course.duration_hours == 2.5  # from 09:00 to 11:30; a typed value is ignored


def test_new_place_needs_an_address_until_entered(instance):
    from metrics.planning import places
    from metrics.planning.models import Place

    session = get_planning_sessionmaker()()
    course = make(None, DAY, "09:00", "11:00", location="Jugendzentrum Neustadt", stage=BESTAETIGT)
    session.add(course)
    session.flush()
    merged = places.merged(instance, session)
    assert any("Adresse fehlt" in f.text for f in service.flags(course, [], {}, merged, {}, TODAY, False))
    assert places.missing(session, merged) == [("Jugendzentrum Neustadt", 1)]
    assert not places.needs_address(instance, "Grundschule Musterstadt")  # in the location list

    session.add(
        Place(
            name="Jugendzentrum Neustadt",
            street="Hauptstr. 1",
            zip_code="83700",
            city="Neustadt",
            school=False,
        )
    )
    session.flush()
    merged = places.merged(instance, session)
    assert not any("Adresse" in f.text for f in service.flags(course, [], {}, merged, {}, TODAY, False))
    assert merged.location_is_school("Jugendzentrum Neustadt") is False  # feeds the target group


def test_calendar_changes_say_what_the_update_does():
    course = make(7, DAY, "09:00", "11:00", lead=["kim"], course_name="Löten")
    expected = cal.event_for(course, {"kim": "Kim"}, "http://a/planung/7")
    assert "Planungs-ID: 7" in expected.description
    actual = cal.Event(
        expected.uid,
        expected.summary,
        expected.start,
        expected.end,
        "Schule",
        expected.description.replace("Kim", "Sam").replace("http://a", "http://b"),
    )
    assert cal.changes(expected, actual) == [("Ort", "Schule", "Makerspace"), ("Kursleitung", "Sam", "Kim")]


def test_orte_page_and_sidebar_counters(team):
    team.post(
        "/planung/neu",
        data={
            "date": "2030-03-04",
            "start_time": "09:00",
            "end_time": "11:00",
            "location": "Jugendzentrum Neustadt",
            "lead": "demo-a",
        },
    )
    page = team.get("/planung/orte").text
    assert "Jugendzentrum Neustadt" in page and "Adresse fehlt" in page
    sidebar = team.get("/").text
    assert "nav-count nav-count-medium" in sidebar and 'title="1 Orte ohne Adresse"' in sidebar
    saved = team.post(
        "/planung/orte",
        data={
            "name": "Jugendzentrum Neustadt",
            "street": "Hauptstr. 1",
            "zip_code": "83700",
            "city": "Neustadt",
            "school": "nein",
        },
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert 'title="1 Orte ohne Adresse"' not in team.get("/").text
    assert (
        team.post("/planung/orte", data={"name": "Grundschule Musterstadt"}).status_code == 400
    )  # already listed


def test_team_is_slack_users_by_first_name(monkeypatch):
    from metrics.planning import vocabulary

    rows = [
        ("U0AAA1111", "Kim Muster"),
        ("Christine", "Christine"),
        ("U0BBB2222", "Sam Probe"),
        ("U0CCC3333", "Sam Beispiel"),
    ]

    class Result:
        def all(self):
            return rows

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement):
            return Result()

    monkeypatch.setattr(vocabulary, "get_sessionmaker", lambda: Session)
    assert vocabulary.team_from_slack() == [
        ("U0AAA1111", "Kim"),
        ("U0CCC3333", "Sam B."),
        ("U0BBB2222", "Sam P."),
    ]


def test_one_lead_and_children_by_average_age(instance):
    course = make(1, None)
    form = {
        **BASIC,
        **_form(lead=["kim", "sam"], support=["kim", "robin"], class_kind="alter", child_age="12"),
    }
    service.apply_form(course, form, GEPLANT)
    assert course.keys(LEAD) == ["kim"] and course.keys(SUPPORT) == ["robin"]
    assert (course.class_age_text, course.class_level) == ("12 Jahre", None)


def test_school_place_wants_class_or_teachers(instance):
    course = _held(class_kind=["band"], age_band=["19 - 24 Jahre"])  # at "Grundschule Musterstadt"
    texts = [p.text for p in rules.warnings(course, instance, {})]
    assert any("ist eine Schule" in t for t in texts)


def test_excluded_people_are_left_out(monkeypatch):
    from dataclasses import replace

    from metrics.planning import vocabulary

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement):
            return type(
                "R", (), {"all": lambda self: [("U0AAA1111", "Kim Muster"), ("U0BBB2222", "Sam Probe")]}
            )()

    instance = vocabulary.get_instance()
    monkeypatch.setattr(vocabulary, "get_sessionmaker", lambda: Session)
    monkeypatch.setattr(
        vocabulary,
        "get_instance",
        lambda: replace(instance, planning_exclude_people=frozenset({"U0BBB2222"})),
    )
    assert vocabulary.team_from_slack() == [("U0AAA1111", "Kim")]
