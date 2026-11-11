"""The course calendar: create events for courses and find calendar/course mismatches.

Two backends with the same methods:

- `DemoCalendar`: a table in the planning database, used while `CALDAV_URL` is not set. The
  review page can delete or add its events to show what the checks find.
- `CalDavCalendar`: a CalDAV collection (e.g. a Nextcloud calendar), standard library only.
  Events created by the app are stored as `<uid>.ics`; times are written in UTC.

All times inside the app are naive local times (Europe/Berlin). The app never deletes events.
"""

import base64
import datetime as dt
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from metrics.config import get_settings
from metrics.planning.models import (
    ABGESAGT,
    BESTAETIGT,
    CAL_MISSING,
    CAL_NONE,
    CAL_OK,
    CAL_OUTDATED,
    GEPLANT,
    CalendarOrphan,
    Course,
    DemoCalendarEvent,
)

LOCAL = ZoneInfo("Europe/Berlin")
TIMEOUT = 10
LOOKAHEAD_DAYS = 400


@dataclass
class Event:
    uid: str
    summary: str
    start: dt.datetime  # naive, local time
    end: dt.datetime
    location: str | None = None
    description: str | None = None


class CalendarError(Exception):
    """The calendar could not be reached or refused the request (message in German)."""


# --- what the event for a course looks like -------------------------------------------------


PLANNING_LINE = "Planung: "  # the link line; it depends on the address used, so it is not compared


def event_for(course: Course, names: dict[str, str], link: str) -> Event | None:
    if not (course.date and course.start_time and course.end_time):
        return None
    people = lambda role: ", ".join(names.get(k, k) for k in course.keys(role)) or "–"  # noqa: E731
    lines = [
        f"Kontext: {course.context or '–'}",
        f"Klasse/Alter: {course.class_age_text or '–'}",
        f"Kursleitung: {people('lead')}",
        f"Unterstützung: {people('support')}",
        f"Planungs-ID: {course.id}",
        f"{PLANNING_LINE}{link}",
    ]
    return Event(
        uid=course.calendar_uid or f"planung-{course.id}",
        summary=" · ".join(x for x in (course.title, course.location) if x),
        start=dt.datetime.combine(course.date, course.start_time),
        end=dt.datetime.combine(course.date, course.end_time),
        location=course.location,
        description="\n".join(lines),
    )


def _team_lines(description: str | None) -> list[str]:
    return [line for line in (description or "").splitlines() if not line.startswith(PLANNING_LINE)]


def changes(expected: Event, actual: Event) -> list[tuple[str, str, str]]:
    """(what, in the calendar, from the course) for everything the update would change."""

    def when(e: Event) -> str:
        return f"{e.start:%d.%m.%Y %H:%M}–{e.end:%H:%M}"

    found = []
    if (expected.start, expected.end) != (actual.start, actual.end):
        found.append(("Termin", when(actual), when(expected)))
    if (expected.location or "") != (actual.location or ""):
        found.append(("Ort", actual.location or "–", expected.location or "–"))
    if expected.summary != actual.summary:
        found.append(("Titel", actual.summary, expected.summary))
    old, new = _team_lines(actual.description), _team_lines(expected.description)
    for line in new:
        label = line.split(":", 1)[0]
        before = next((x for x in old if x.split(":", 1)[0] == label), "")
        if before != line:
            found.append((label, before.split(":", 1)[-1].strip() or "–", line.split(":", 1)[-1].strip()))
    return found


def differs(expected: Event, actual: Event) -> bool:
    return bool(changes(expected, actual))


# --- backends -----------------------------------------------------------------------------


class DemoCalendar:
    name = "Demo-Kalender (in der Prototyp-Datenbank)"
    demo = True

    def __init__(self, session: Session):
        self.session = session

    def _event(self, row: DemoCalendarEvent) -> Event:
        return Event(row.uid, row.summary, row.start, row.end, row.location, row.description)

    def get(self, uid: str) -> Event | None:
        row = self.session.get(DemoCalendarEvent, uid)
        return self._event(row) if row else None

    def put(self, event: Event) -> None:
        self.session.merge(
            DemoCalendarEvent(
                uid=event.uid,
                summary=event.summary,
                start=event.start,
                end=event.end,
                location=event.location,
                description=event.description,
            )
        )

    def events(self, start: dt.date, end: dt.date) -> list[Event]:
        rows = self.session.scalars(
            select(DemoCalendarEvent).where(
                DemoCalendarEvent.start >= dt.datetime.combine(start, dt.time()),
                DemoCalendarEvent.start < dt.datetime.combine(end, dt.time()),
            )
        )
        return [self._event(r) for r in rows]

    def delete(self, uid: str) -> None:
        """Only for the demo: stands for someone deleting the event in the calendar."""
        row = self.session.get(DemoCalendarEvent, uid)
        if row:
            self.session.delete(row)


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _unescape(text: str) -> str:
    return (
        text.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _utc(local: dt.datetime) -> str:
    return local.replace(tzinfo=LOCAL).astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def to_ics(event: Event) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//workshop-calculator//Planung//DE",
        "BEGIN:VEVENT",
        f"UID:{event.uid}",
        f"DTSTAMP:{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}",
        f"DTSTART:{_utc(event.start)}",
        f"DTEND:{_utc(event.end)}",
        f"SUMMARY:{_escape(event.summary)}",
    ]
    if event.location:
        lines.append(f"LOCATION:{_escape(event.location)}")
    if event.description:
        lines.append(f"DESCRIPTION:{_escape(event.description)}")
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(lines)


def _parse_time(name_params: str, value: str) -> dt.datetime:
    params = dict(p.split("=", 1) for p in name_params.split(";")[1:] if "=" in p)
    if "VALUE=DATE" in name_params or len(value) == 8:  # all-day
        return dt.datetime.strptime(value[:8], "%Y%m%d")
    moment = dt.datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return moment.replace(tzinfo=dt.UTC).astimezone(LOCAL).replace(tzinfo=None)
    if "TZID" in params:
        try:
            zone = ZoneInfo(params["TZID"].strip('"'))
            return moment.replace(tzinfo=zone).astimezone(LOCAL).replace(tzinfo=None)
        except (KeyError, ValueError):
            pass
    return moment  # floating time: taken as local


def parse_ics(text: str) -> list[Event]:
    """The VEVENTs of an iCalendar text (recurrence rules are not expanded)."""
    unfolded = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    events, current = [], None
    for line in unfolded:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            if "UID" in current and "DTSTART" in current:
                start = _parse_time(*current["DTSTART"])
                end = _parse_time(*current["DTEND"]) if "DTEND" in current else start + dt.timedelta(hours=1)
                events.append(
                    Event(
                        uid=current["UID"][1],
                        summary=_unescape(current.get("SUMMARY", ("", ""))[1]),
                        start=start,
                        end=end,
                        location=_unescape(current["LOCATION"][1]) if "LOCATION" in current else None,
                        description=_unescape(current["DESCRIPTION"][1])
                        if "DESCRIPTION" in current
                        else None,
                    )
                )
            current = None
        elif current is not None and ":" in line:
            name_params, value = line.split(":", 1)
            name = name_params.split(";", 1)[0].upper()
            if name in ("UID", "SUMMARY", "LOCATION", "DESCRIPTION", "DTSTART", "DTEND"):
                current.setdefault(name, (name_params, value))
    return events


class CalDavCalendar:
    demo = False

    def __init__(self, url: str, username: str | None, password: str | None):
        self.url = url.rstrip("/") + "/"
        self.name = f"CalDAV ({self.url})"
        self.headers = {}
        if username:
            token = base64.b64encode(f"{username}:{password or ''}".encode()).decode()
            self.headers["Authorization"] = f"Basic {token}"

    def _request(self, method: str, url: str, body: str | None = None, headers: dict | None = None):
        request = urllib.request.Request(
            url,
            data=body.encode() if body is not None else None,
            method=method,
            headers={**self.headers, **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.status, response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return 404, ""
            raise CalendarError(f"Der Kalender hat die Anfrage abgelehnt ({error.code}).") from error
        except OSError as error:
            raise CalendarError("Der Kalender ist nicht erreichbar.") from error

    def get(self, uid: str) -> Event | None:
        status, body = self._request("GET", f"{self.url}{uid}.ics")
        if status == 404:
            return None
        events = parse_ics(body)
        return events[0] if events else None

    def put(self, event: Event) -> None:
        self._request(
            "PUT",
            f"{self.url}{event.uid}.ics",
            to_ics(event),
            {"Content-Type": "text/calendar; charset=utf-8"},
        )

    def events(self, start: dt.date, end: dt.date) -> list[Event]:
        utc = lambda d: dt.datetime.combine(d, dt.time()).replace(tzinfo=LOCAL).astimezone(dt.UTC)  # noqa: E731
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
    <c:time-range start="{utc(start):%Y%m%dT%H%M%SZ}" end="{utc(end):%Y%m%dT%H%M%SZ}"/>
  </c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>"""
        _, text = self._request(
            "REPORT", self.url, body, {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}
        )
        events = []
        for data in ET.fromstring(text).iter("{urn:ietf:params:xml:ns:caldav}calendar-data"):
            events += parse_ics(data.text or "")
        return events


def get_calendar(session: Session):
    s = get_settings()
    if s.caldav_url:
        return CalDavCalendar(s.caldav_url, s.caldav_username, s.caldav_password)
    return DemoCalendar(session)


# --- the check ----------------------------------------------------------------------------


def check(
    session: Session, calendar, courses: list[Course], names: dict[str, str], today: dt.date, link
) -> None:
    """Update each course's calendar state and the list of events without a course."""
    now = dt.datetime.now(dt.UTC)
    for course in courses:
        if course.stage not in (GEPLANT, BESTAETIGT, ABGESAGT) or not course.calendar_uid:
            continue
        actual = calendar.get(course.calendar_uid)
        expected = event_for(course, names, link(course))
        if actual is None:
            course.calendar_state = CAL_NONE if course.stage == ABGESAGT else CAL_MISSING
        elif course.stage != ABGESAGT and expected and differs(expected, actual):
            course.calendar_state = CAL_OUTDATED
        else:
            course.calendar_state = CAL_OK
        course.calendar_checked_at = now

    linked = {c.calendar_uid for c in courses if c.calendar_uid and c.stage in (GEPLANT, BESTAETIGT)}
    seen = {}
    for event in calendar.events(today, today + dt.timedelta(days=LOOKAHEAD_DAYS)):
        if event.uid not in linked and event.start.date() >= today:
            seen[event.uid] = event
    for orphan in session.scalars(select(CalendarOrphan)):
        if orphan.uid not in seen:
            session.delete(orphan)  # bookkeeping of the check, not user data
    for uid, event in seen.items():
        row = session.get(CalendarOrphan, uid)
        if row is None:
            session.add(
                CalendarOrphan(
                    uid=uid, summary=event.summary, start=event.start, end=event.end, location=event.location
                )
            )
        else:
            row.summary, row.start, row.end, row.location = (
                event.summary,
                event.start,
                event.end,
                event.location,
            )
