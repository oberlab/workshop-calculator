"""The course calendar over CalDAV, with invitations (WM-110, PROPOSAL.md 4.11).

One event per Termin in the course calendar (a Nextcloud calendar of the app's own account);
`Appointment.calendar_uid` links them, and the event's description carries the Termin ID and a link
to the course page. Ported from the prototype's `calendar.py`.

Invitations: with `CALDAV_ORGANIZER` set (the e-mail address of the app's account), the event gets
that `ORGANIZER` and one `ATTENDEE` per lead and support who has an e-mail address. Nextcloud
schedules on its own (implicit scheduling, RFC 6638): it sends the invitation when the event is
created, an update when it changes (time, place, title, team; `SEQUENCE` goes up), a cancellation
to a person taken out of the team, and a cancellation to everyone when the event is set to
`STATUS:CANCELLED`. The app never deletes an event: a cancelled or deleted Termin cancels it.
Responses (PARTSTAT) are read back by the check and shown on the course page; a decline becomes a
Wichtig hint (rule "einladung-abgelehnt").

Only Termine from Bestätigt on are in the calendar (Alex, 2026-11-13): a Geplant Termin has no
event and invites nobody. A Termin that goes back to Geplant gets its event cancelled with a note
(attendees get the cancellation, as for a cancelled Termin) and the link is dropped
(`withdraw`; `workflow.back_to_planned` marks the event outdated, so a calendar that is down is
retried by the check). A Geplant Termin linked to an event otherwise (created from an event on
"Prüfen", or entered before this rule) keeps its event untouched until it is confirmed. When a
withdrawn Termin is confirmed again, "In Kalender eintragen" creates a new event under a new UID
rather than reviving the cancelled one: attendee calendars (Nextcloud, Thunderbird, Outlook) treat
a re-sent cancelled UID differently (some drop it, some keep it struck through), while a new UID is
a plain new invitation everywhere, and the cancelled event stays as the record.

When the calendar is written:
- "In Kalender eintragen" (`enter`), only by a person, for a Bestätigt Termin.
- After a save or stage change of a Termin that has an event (`push`): only when the event would
  change, so a save without calendar-relevant changes sends nothing.
- "Kalender aktualisieren" (`push` from the course page) after the course page showed what it
  would change (`preview`), e.g. when someone edited the event in the calendar.
- By the check (`check`, scheduler job "calendar" every 15 minutes; Alex, 2026-11-13): an event
  marked outdated (a push that failed because the calendar was down, a Termin back in Geplant, or
  an event changed in the calendar) is brought up to date with the same rules as "Kalender
  aktualisieren" (`push`: one update per Termin, SEQUENCE up, cancelled = STATUS:CANCELLED), with a
  Verlauf row by "Kalenderabgleich (automatisch)". It never creates an event for a Termin that was
  never entered (nor re-creates a missing one: that is a hint for a person) and never touches
  events without a Termin.
Otherwise the check reads: it marks events as missing or outdated, stores the responses and lists
future events without a Termin.

The public calendar (`CALDAV_PUBLIC_URL`, same account; Alex, 2026-11-19, option a): a Termin with
an event whose context is marked "Im öffentlichen Kalender" (e.g. Ferienprogramm) also gets a
second event there, with only the format, time, place and the context's name: no organizer, no
attendees (so nobody is mailed), no internal link, no notes. It follows the internal event
(`sync_public`): created with it, updated when it changes, cancelled (never deleted) when the Termin
is cancelled, goes back to Geplant or is deleted, or when its context no longer asks for it. Its UID
is the internal UID plus `-oeffentlich`, so it needs no column of its own. A public calendar that is
down never stops the internal one: the check (every 15 minutes) catches up.

Standard library only. Times inside the app are naive local times (Europe/Berlin); events are
written in that local time with its zone (`VTIMEZONE`), so calendars and e-mails show it as is.
"""

import base64
import datetime as dt
import secrets
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, replace
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from metrics import hints, lists
from metrics.config import get_settings
from metrics.db import (
    ABGESAGT,
    BESTAETIGT,
    CAL_MISSING,
    CAL_OK,
    CAL_OUTDATED,
    GEPLANT,
    Appointment,
    CalendarOrphan,
    CalendarResponse,
    Course,
    Person,
)

LOCAL = ZoneInfo("Europe/Berlin")
TIMEOUT = 10
LOOKAHEAD_DAYS = 400
IN_CALENDAR = (BESTAETIGT,)  # stages that are entered and kept up to date (not Geplant: 2026-11-13)
ORGANIZER_NAME = "Kursplanung"
PRODID = "-//workshop-calculator//Planung//DE"
LINK_LINE = "Planung: "  # depends on the address used, so it is not compared
DECLINED = "DECLINED"
PARTSTATS = {
    "NEEDS-ACTION": "noch keine Antwort",
    "ACCEPTED": "zugesagt",
    "TENTATIVE": "vielleicht",
    DECLINED: "abgesagt",
    "DELEGATED": "weitergeleitet",
}


class CalendarError(Exception):
    """The calendar could not be reached or refused the request (message in German)."""


@dataclass
class Attendee:
    email: str
    name: str = ""
    partstat: str = "NEEDS-ACTION"
    role: str = "REQ-PARTICIPANT"


@dataclass
class Event:
    uid: str
    summary: str
    start: dt.datetime  # naive, local time
    end: dt.datetime
    location: str | None = None
    description: str | None = None
    status: str = "CONFIRMED"  # TENTATIVE (Geplant), CONFIRMED, CANCELLED
    organizer: str | None = None  # e-mail address
    attendees: list[Attendee] = field(default_factory=list)
    sequence: int = 0


# --- iCalendar ------------------------------------------------------------------------------------


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _unescape(text: str) -> str:
    out, i = [], 0
    while i < len(text):
        if text[i] == "\\" and i + 1 < len(text):
            nxt = text[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _param(value: str) -> str:
    return f'"{value}"' if any(c in value for c in ':;,"') else value


def _fold(line: str) -> list[str]:
    """Lines of at most 75 octets (RFC 5545 3.1), continued with a space."""
    parts, current = [], ""
    for char in line:
        limit = 75 if not parts else 74
        if len((current + char).encode()) > limit:
            parts.append(current)
            current = char
        else:
            current += char
    parts.append(current)
    return [parts[0], *(" " + p for p in parts[1:])]


def _local(moment: dt.datetime) -> str:
    """A local time with its zone, so calendars and Nextcloud's e-mails show 09:00, not 08:00 UTC."""
    return f";TZID={TZID}:{moment:%Y%m%dT%H%M%S}"


# The zone the times are written in (the rules since 1996, enough for any future course).
TZID = "Europe/Berlin"
VTIMEZONE = [
    "BEGIN:VTIMEZONE",
    f"TZID:{TZID}",
    "BEGIN:DAYLIGHT",
    "TZOFFSETFROM:+0100",
    "TZOFFSETTO:+0200",
    "TZNAME:CEST",
    "DTSTART:19700329T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU",
    "END:DAYLIGHT",
    "BEGIN:STANDARD",
    "TZOFFSETFROM:+0200",
    "TZOFFSETTO:+0100",
    "TZNAME:CET",
    "DTSTART:19701025T030000",
    "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU",
    "END:STANDARD",
    "END:VTIMEZONE",
]


def to_ics(event: Event) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        *VTIMEZONE,
        "BEGIN:VEVENT",
        f"UID:{event.uid}",
        f"DTSTAMP:{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}",
        f"DTSTART{_local(event.start)}",
        f"DTEND{_local(event.end)}",
        f"SEQUENCE:{event.sequence}",
        f"STATUS:{event.status}",
        f"SUMMARY:{_escape(event.summary)}",
    ]
    if event.location:
        lines.append(f"LOCATION:{_escape(event.location)}")
    if event.description:
        lines.append(f"DESCRIPTION:{_escape(event.description)}")
    if event.organizer:
        lines.append(f"ORGANIZER;CN={_param(ORGANIZER_NAME)}:mailto:{event.organizer}")
        for a in event.attendees:
            params = f"CN={_param(a.name or a.email)};ROLE={a.role};PARTSTAT={a.partstat};RSVP=TRUE"
            lines.append(f"ATTENDEE;{params}:mailto:{a.email}")
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(folded for line in lines for folded in _fold(line))


def _split_params(name_params: str) -> tuple[str, dict[str, str]]:
    """'ATTENDEE;CN="A, B";PARTSTAT=ACCEPTED' -> ('ATTENDEE', {'CN': 'A, B', 'PARTSTAT': 'ACCEPTED'})."""
    parts, current, quoted = [], "", False
    for char in name_params:
        if char == '"':
            quoted = not quoted
        elif char == ";" and not quoted:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    params = {}
    for p in parts[1:]:
        key, _, value = p.partition("=")
        params[key.upper()] = value.strip('"')
    return parts[0].upper(), params


def _content_line(line: str) -> tuple[str, dict[str, str], str]:
    """Split at the first colon outside quotes."""
    quoted = False
    for i, char in enumerate(line):
        if char == '"':
            quoted = not quoted
        elif char == ":" and not quoted:
            name, params = _split_params(line[:i])
            return name, params, line[i + 1 :]
    return line.upper(), {}, ""


def _parse_time(params: dict[str, str], value: str) -> dt.datetime:
    if params.get("VALUE") == "DATE" or len(value) == 8:  # all-day
        return dt.datetime.strptime(value[:8], "%Y%m%d")
    moment = dt.datetime.strptime(value.rstrip("Z"), "%Y%m%dT%H%M%S")
    if value.endswith("Z"):
        return moment.replace(tzinfo=dt.UTC).astimezone(LOCAL).replace(tzinfo=None)
    if "TZID" in params:
        try:
            return moment.replace(tzinfo=ZoneInfo(params["TZID"])).astimezone(LOCAL).replace(tzinfo=None)
        except (KeyError, ValueError):
            pass
    return moment  # floating time: taken as local


def _mail(value: str) -> str:
    return value[7:] if value.lower().startswith("mailto:") else value


def parse_ics(text: str) -> list[Event]:
    """The VEVENTs of an iCalendar text (recurrence rules are not expanded)."""
    unfolded: list[str] = []
    for line in text.replace("\r\n", "\n").split("\n"):
        if line[:1] in (" ", "\t") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    events, current, depth = [], None, 0
    for line in unfolded:
        if line == "BEGIN:VEVENT":
            current, depth = {"ATTENDEE": []}, 0
        elif line.startswith("BEGIN:") and current is not None:
            depth += 1  # a VALARM inside the event
        elif line.startswith("END:") and current is not None and depth:
            depth -= 1
        elif line == "END:VEVENT" and current is not None:
            if "UID" in current and "DTSTART" in current:
                start = _parse_time(*current["DTSTART"])
                end = _parse_time(*current["DTEND"]) if "DTEND" in current else start + dt.timedelta(hours=1)
                fields = current

                def text_of(key: str, fields=fields) -> str | None:
                    return _unescape(fields[key][1]) if key in fields else None

                events.append(
                    Event(
                        uid=current["UID"][1],
                        summary=text_of("SUMMARY") or "",
                        start=start,
                        end=end,
                        location=text_of("LOCATION"),
                        description=text_of("DESCRIPTION"),
                        status=(text_of("STATUS") or "CONFIRMED").upper(),
                        organizer=_mail(current["ORGANIZER"][1]) if "ORGANIZER" in current else None,
                        attendees=[
                            Attendee(
                                _mail(value),
                                params.get("CN", ""),
                                params.get("PARTSTAT", "NEEDS-ACTION").upper(),
                                params.get("ROLE", "REQ-PARTICIPANT").upper(),
                            )
                            for params, value in current["ATTENDEE"]
                        ],
                        sequence=int(text_of("SEQUENCE") or 0),
                    )
                )
            current = None
        elif current is not None and not depth and ":" in line:
            name, params, value = _content_line(line)
            if name == "ATTENDEE":
                current["ATTENDEE"].append((params, value))
            elif name in (
                "UID",
                "SUMMARY",
                "LOCATION",
                "DESCRIPTION",
                "DTSTART",
                "DTEND",
                "STATUS",
                "ORGANIZER",
                "SEQUENCE",
            ):
                current.setdefault(name, (params, value))
    return events


# --- The client -----------------------------------------------------------------------------------


class CalDavCalendar:
    """A CalDAV collection. Events created by the app are stored as `<uid>.ics`."""

    def __init__(self, url: str, username: str | None, password: str | None, organizer: str | None = None):
        self.url = url.rstrip("/") + "/"
        self.organizer = organizer
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
                return response.status, response.read().decode("utf-8", errors="replace"), response.headers
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return 404, "", error.headers
            if error.code == 412:
                raise CalendarError(
                    "Der Kalendereintrag wurde inzwischen im Kalender geändert. Bitte die Seite neu laden."
                ) from error
            raise CalendarError(f"Der Kalender hat die Anfrage abgelehnt ({error.code}).") from error
        except OSError as error:
            raise CalendarError("Der Kalender ist nicht erreichbar.") from error

    def get(self, uid: str) -> tuple[Event, str | None] | None:
        """The event and its ETag, or None if there is none."""
        status, body, headers = self._request("GET", f"{self.url}{uid}.ics")
        if status == 404:
            return None
        events = parse_ics(body)
        return (events[0], headers.get("ETag")) if events else None

    def put(self, event: Event, etag: str | None = None, create: bool = False) -> None:
        """Create (only if no event has that UID) or replace (only if unchanged since `etag`)."""
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if create:
            headers["If-None-Match"] = "*"
        elif etag:
            headers["If-Match"] = etag
        self._request("PUT", f"{self.url}{event.uid}.ics", to_ics(event), headers)

    def events(self, start: dt.date, end: dt.date) -> list[Event]:
        utc = lambda d: dt.datetime.combine(d, dt.time()).replace(tzinfo=LOCAL).astimezone(dt.UTC)  # noqa: E731
        body = f"""<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-data/></d:prop>
  <c:filter><c:comp-filter name="VCALENDAR"><c:comp-filter name="VEVENT">
    <c:time-range start="{utc(start):%Y%m%dT%H%M%SZ}" end="{utc(end):%Y%m%dT%H%M%SZ}"/>
  </c:comp-filter></c:comp-filter></c:filter>
</c:calendar-query>"""
        _, text, _ = self._request(
            "REPORT", self.url, body, {"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}
        )
        found = []
        try:
            root = ET.fromstring(text)
        except ET.ParseError as error:
            raise CalendarError("Der Kalender hat unlesbar geantwortet.") from error
        for data in root.iter("{urn:ietf:params:xml:ns:caldav}calendar-data"):
            found += parse_ics(data.text or "")
        return found


def get_calendar() -> CalDavCalendar | None:
    """The course calendar, or None while `CALDAV_URL` is not set."""
    s = get_settings()
    if not s.caldav_url:
        return None
    return CalDavCalendar(s.caldav_url, s.caldav_username, s.caldav_password, s.caldav_organizer)


def get_public_calendar() -> CalDavCalendar | None:
    """The public calendar, or None. Same login as the internal one; no organizer: nobody is invited."""
    s = get_settings()
    if not (s.caldav_url and s.caldav_public_url):
        return None
    return CalDavCalendar(s.caldav_public_url, s.caldav_username, s.caldav_password)


def link_base() -> str:
    return get_settings().public_url or ""


# --- The event of a Termin ------------------------------------------------------------------------


def _team(t: Appointment) -> list[tuple[Person, str]]:
    people = ([t.lead] if t.lead else []) + list(t.support)
    return [(p, "Kursleitung" if p is t.lead else "Unterstützung") for p in people]


def uninvited(t: Appointment) -> list[Person]:
    """People of the team without an e-mail address: they get no invitation."""
    return [p for p, _ in _team(t) if not p.email]


def expected_event(
    t: Appointment, base: str, organizer: str | None, previous: Event | None = None, uid: str | None = None
) -> Event | None:
    """The event the Termin should have; None without date, start and end.

    `previous` (the event as it is) keeps the answers of people who stay invited, unless the time
    changed, and gives the sequence: one up when anything that matters changes.
    """
    if not (t.date and t.start_time and t.end_time):
        return None
    status = {GEPLANT: "TENTATIVE", ABGESAGT: "CANCELLED"}.get(t.stage, "CONFIRMED")
    names = lambda role: ", ".join(p.name for p, r in _team(t) if r == role) or "–"  # noqa: E731
    lines = [
        f"Kontext: {t.context.name if t.context else '–'}",
        f"Klasse/Alter: {t.class_age_text or '–'}",
        f"Kursleitung: {names('Kursleitung')}",
        f"Unterstützung: {names('Unterstützung')}",
        f"Termin-ID: {t.id}",
    ]
    if t.stage == ABGESAGT and t.cancel_reason:
        lines.insert(0, f"Abgesagt: {t.cancel_reason}")
    if base:
        lines.append(f"{LINK_LINE}{base}/planung/{t.course_id}?termin={t.id}")
    title = hints.title(t)
    place = t.place.name if t.place else None
    # The address too, so the entry leads there (calendar apps link it to a map).
    where = ", ".join(x for x in (place, t.place.address if t.place else None) if x) or None
    event = Event(
        uid=uid or t.calendar_uid or new_uid(t),
        summary=" · ".join(x for x in (title, place) if x),
        start=dt.datetime.combine(t.date, t.start_time),
        end=dt.datetime.combine(t.date, t.end_time),
        location=where,
        description="\n".join(lines),
        status=status,
    )
    if organizer:
        event.organizer = organizer
        before = {a.email.lower(): a for a in previous.attendees} if previous else {}
        moved = previous is None or (previous.start, previous.end) != (event.start, event.end)
        for person, role in _team(t):
            if not person.email:
                continue
            old = before.get(person.email.lower())
            event.attendees.append(
                Attendee(
                    person.email,
                    person.name,
                    old.partstat if old and not moved else "NEEDS-ACTION",
                    "CHAIR" if role == "Kursleitung" else "REQ-PARTICIPANT",
                )
            )
    if previous is not None:
        event.sequence = previous.sequence + (1 if changes(event, previous) else 0)
    return event


def new_uid(t: Appointment) -> str:
    return f"zahlen-termin-{t.id}-{secrets.token_hex(4)}"


def _description_lines(description: str | None) -> list[str]:
    return [line for line in (description or "").splitlines() if not line.startswith(LINK_LINE)]


STATUS_LABELS = {"TENTATIVE": "vorläufig", "CONFIRMED": "bestätigt", "CANCELLED": "abgesagt"}


def changes(expected: Event, actual: Event) -> list[tuple[str, str, str]]:
    """(what, in the calendar, from the Termin) for everything an update would change."""

    def when(e: Event) -> str:
        return f"{e.start:%d.%m.%Y %H:%M}–{e.end:%H:%M}"

    def team(e: Event) -> str:
        return ", ".join(sorted(f"{a.name or a.email} <{a.email.lower()}>" for a in e.attendees)) or "–"

    found = []
    if (expected.start, expected.end) != (actual.start, actual.end):
        found.append(("Termin", when(actual), when(expected)))
    if (expected.location or "") != (actual.location or ""):
        found.append(("Ort", actual.location or "–", expected.location or "–"))
    if expected.summary != actual.summary:
        found.append(("Titel", actual.summary, expected.summary))
    if expected.status != actual.status:
        found.append(
            (
                "Status",
                STATUS_LABELS.get(actual.status, actual.status),
                STATUS_LABELS.get(expected.status, expected.status),
            )
        )
    if {a.email.lower() for a in expected.attendees} != {a.email.lower() for a in actual.attendees} or (
        (expected.organizer or "").lower() != (actual.organizer or "").lower()
    ):
        found.append(("Eingeladen", team(actual), team(expected)))
    old, new = _description_lines(actual.description), _description_lines(expected.description)
    if old != new:
        found.append(("Beschreibung", " / ".join(old) or "–", " / ".join(new) or "–"))
    return found


# --- The public calendar -------------------------------------------------------------------------

PUBLIC_SUFFIX = "-oeffentlich"


def public_uid(t: Appointment) -> str | None:
    return f"{t.calendar_uid}{PUBLIC_SUFFIX}" if t.calendar_uid else None


def public_event(t: Appointment, uid: str) -> Event | None:
    """What the public sees: format, time, place with address, the context's name. Nothing else."""
    if not (t.date and t.start_time and t.end_time):
        return None
    place = t.place.name if t.place else None
    where = ", ".join(x for x in (place, t.place.address if t.place else None) if x) or None
    return Event(
        uid=uid,
        summary=t.format.name if t.format else hints.title(t),
        start=dt.datetime.combine(t.date, t.start_time),
        end=dt.datetime.combine(t.date, t.end_time),
        location=where,
        description=(t.context.page_title or t.context.name) if t.context else None,
        status="CONFIRMED",
    )


_UNREAD = object()


def sync_public(
    session: Session, t: Appointment, by: str, uid: str | None = None, current=_UNREAD
) -> str | None:
    """Bring the Termin's public event in line: there while the Termin is Bestätigt, has an event
    and its context asks for it; otherwise cancelled (never deleted). Returns what happened, or None.
    `uid`: the public UID when the internal one is about to be dropped (`withdraw`); `current`: the
    event as the check already read it (None: there is none). Raises CalendarError."""
    public = get_public_calendar()
    uid = uid or public_uid(t)
    if public is None or uid is None:
        return None
    if current is _UNREAD:
        current = public.get(uid)
    shown = t.stage in IN_CALENDAR and t.context is not None and t.context.public_calendar
    if shown and (expected := public_event(t, uid)) is not None:
        if current is None:
            public.put(expected, create=True)
            reason, note = "in den öffentlichen Kalender eingetragen", "Im öffentlichen Kalender eingetragen."
        else:
            actual, etag = current
            if not changes(expected, actual):
                return None
            expected.sequence = actual.sequence + 1
            public.put(expected, etag=etag)
            reason, note = "öffentlicher Kalender aktualisiert", "Öffentlicher Kalender aktualisiert."
    elif not shown and current is not None and current[0].status != "CANCELLED":
        actual, etag = current
        public.put(replace(actual, status="CANCELLED", sequence=actual.sequence + 1), etag=etag)
        reason, note = "im öffentlichen Kalender abgesagt", "Im öffentlichen Kalender abgesagt."
    else:
        return None
    _log(session, t, by, reason, uid)
    return note


def _public(session: Session, t: Appointment, by: str, uid: str | None = None) -> str | None:
    """`sync_public` for the pages: a public calendar that is down only gets a note."""
    try:
        return sync_public(session, t, by, uid)
    except CalendarError:
        return "Der öffentliche Kalender ist nicht erreichbar; der Abgleich holt es nach."


# --- Writing --------------------------------------------------------------------------------------


def _log(session: Session, t: Appointment, by: str, reason: str, new: str | None) -> None:
    lists._log(session, "appointments", t.id, "calendar_uid", None, new, by, reason)


def enter(session: Session, calendar: CalDavCalendar, t: Appointment, base: str, by: str) -> str:
    """ "In Kalender eintragen": create the event (or bring a known one up to date)."""
    if t.stage not in IN_CALENDAR:
        raise CalendarError("Nur bestätigte Termine kommen in den Kalender.")
    if not (t.date and t.start_time and t.end_time):
        raise CalendarError("Für den Kalender braucht es Datum, Beginn und Ende.")
    if t.calendar_uid and (current := calendar.get(t.calendar_uid)) is not None:
        return push(session, calendar, t, base, by, current=current) or "Der Kalendereintrag ist aktuell."
    uid = t.calendar_uid or new_uid(t)
    event = expected_event(t, base, calendar.organizer, uid=uid)
    calendar.put(event, create=True)
    t.calendar_uid = uid
    t.calendar_state = CAL_OK
    invited = ", ".join(a.name for a in event.attendees)
    _log(session, t, by, "in den Kalender eingetragen" + (f", eingeladen: {invited}" if invited else ""), uid)
    _store_responses(session, t, event)
    hints.refresh(session, [t.id])
    note = "In den Kalender eingetragen." + (f" Eingeladen: {invited}." if invited else "")
    return " ".join(x for x in (note, _public(session, t, by)) if x)


def push(
    session: Session,
    calendar: CalDavCalendar,
    t: Appointment,
    base: str,
    by: str,
    current: tuple[Event, str | None] | None = None,
) -> str | None:
    """`_push` for the internal event, then the public one (`sync_public`); returns what happened."""
    note = _push(session, calendar, t, base, by, current)
    public = _public(session, t, by) if t.calendar_uid and t.stage != GEPLANT else None
    return " ".join(x for x in (note, public) if x) or None


def _push(
    session: Session,
    calendar: CalDavCalendar,
    t: Appointment,
    base: str,
    by: str,
    current: tuple[Event, str | None] | None = None,
) -> str | None:
    """Bring the Termin's event up to date if it would change; returns what happened (or None).

    A Bestätigt Termin updates its event; an Abgesagt one sets it to `STATUS:CANCELLED`; one that
    went back to Geplant has its event cancelled and unlinked (`withdraw`). Nothing is sent when
    nothing changes. The event is never deleted.
    """
    if t.calendar_uid and t.stage == GEPLANT:
        # Back in Geplant (`workflow.back_to_planned` marks the event outdated): cancel it.
        return withdraw(session, calendar, t, by) if t.calendar_state == CAL_OUTDATED else None
    if not t.calendar_uid or t.stage not in (*IN_CALENDAR, ABGESAGT):
        return None
    current = current or calendar.get(t.calendar_uid)
    if current is None:
        t.calendar_state = CAL_MISSING if t.stage != ABGESAGT else None
        hints.refresh(session, [t.id])
        return (
            "Der Kalendereintrag fehlt; „In Kalender eintragen“ legt ihn neu an."
            if t.stage != ABGESAGT
            else None
        )
    actual, etag = current
    expected = expected_event(t, base, calendar.organizer, previous=actual)
    if expected is None:  # times removed: the event keeps the last known time
        t.calendar_state = CAL_OUTDATED
        hints.refresh(session, [t.id])
        return "Ohne Datum, Beginn und Ende bleibt der Kalendereintrag wie er ist."
    found = changes(expected, actual)
    if not found:
        t.calendar_state = CAL_OK
        hints.refresh(session, [t.id])
        return None
    calendar.put(expected, etag=etag)
    t.calendar_state = CAL_OK
    what = ", ".join(what for what, _, _ in found)
    reason = "im Kalender abgesagt" if expected.status == "CANCELLED" else f"Kalender aktualisiert ({what})"
    _log(session, t, by, reason, t.calendar_uid)
    _store_responses(session, t, expected)
    hints.refresh(session, [t.id])
    return (
        "Kalendereintrag abgesagt." if expected.status == "CANCELLED" else f"Kalender aktualisiert: {what}."
    )


def _cancel(calendar: CalDavCalendar, t: Appointment, note: str) -> bool:
    """Set the Termin's event to `STATUS:CANCELLED` with a note; False if there is none to cancel."""
    if not t.calendar_uid or (current := calendar.get(t.calendar_uid)) is None:
        return False
    actual, etag = current
    if actual.status == "CANCELLED":
        return False
    cancelled = replace(actual, status="CANCELLED", sequence=actual.sequence + 1)
    cancelled.description = "\n".join([note, *_description_lines(actual.description)])
    calendar.put(cancelled, etag=etag)
    return True


def cancel_event(
    session: Session, calendar: CalDavCalendar, t: Appointment, base: str, by: str
) -> str | None:
    """Before a Geplant Termin is deleted: its event is cancelled (attendees are told), not deleted;
    so is its public one."""
    public = _public(session, t, by)
    if not _cancel(calendar, t, "Termin gelöscht."):
        return public
    _log(session, t, by, "im Kalender abgesagt (Termin gelöscht)", t.calendar_uid)
    return " ".join(x for x in ("Kalendereintrag abgesagt.", public) if x)


def withdraw(session: Session, calendar: CalDavCalendar, t: Appointment, by: str) -> str | None:
    """A Termin back in Geplant: its event is cancelled with a note (attendees get the cancellation)
    and unlinked, so a later "In Kalender eintragen" sends a fresh invitation under a new UID."""
    uid = t.calendar_uid
    public = _public(session, t, by, public_uid(t))  # Geplant: the public event is cancelled too
    cancelled = _cancel(calendar, t, "Termin wieder in Planung (nicht bestätigt).")
    lists._log(
        session,
        "appointments",
        t.id,
        "calendar_uid",
        uid,
        None,
        by,
        "im Kalender abgesagt (zurück auf „Geplant“)"
        if cancelled
        else "Kalendereintrag gelöst (zurück auf „Geplant“)",
    )
    t.calendar_uid, t.calendar_state = None, None
    t.calendar_responses = []
    hints.refresh(session, [t.id])
    note = "Kalendereintrag abgesagt (wieder geplant)." if cancelled else None
    return " ".join(x for x in (note, public) if x) or None


def preview(calendar: CalDavCalendar, t: Appointment, base: str) -> list[tuple[str, str, str]] | None:
    """What "Kalender aktualisieren" would change; None if there is no event to compare."""
    if not t.calendar_uid or (current := calendar.get(t.calendar_uid)) is None:
        return None
    expected = expected_event(t, base, calendar.organizer, previous=current[0])
    return changes(expected, current[0]) if expected else None


# --- The check ------------------------------------------------------------------------------------


def _store_responses(session: Session, t: Appointment, event: Event) -> bool:
    """Keep the answers of the attendees as the event has them; True if anything changed."""
    team = {p.email.lower(): p for p, _ in _team(t) if p.email}
    seen = {a.email.lower(): a for a in event.attendees}
    changed = False
    for row in list(t.calendar_responses):
        if row.email not in seen:
            t.calendar_responses.remove(row)  # no longer invited: bookkeeping of the check
            changed = True
    existing = {r.email: r for r in t.calendar_responses}
    for email, a in seen.items():
        person = team.get(email)
        row = existing.get(email)
        if row is None:
            t.calendar_responses.append(
                CalendarResponse(email=email, person_id=person.id if person else None, partstat=a.partstat)
            )
            changed = True
        elif row.partstat != a.partstat or row.person_id != (person.id if person else None):
            row.partstat, row.person_id = a.partstat, person.id if person else None
            row.seen_at = dt.datetime.now(dt.UTC)
            changed = True
    return changed


AUTOMATIC = "Kalenderabgleich (automatisch)"  # who the check's own updates are by, in the Verlauf


@dataclass
class CheckResult:
    checked: int = 0
    missing: int = 0
    outdated: int = 0
    responses: int = 0
    orphans: int = 0
    pushed: list[str] = field(default_factory=list)  # "Termin 12: Kalender aktualisiert: Termin."
    failed: list[str] = field(default_factory=list)
    public: list[str] = field(default_factory=list)  # changes in the public calendar

    def summary(self) -> str:
        text = (
            f"{self.checked} events checked: {self.missing} missing, {self.outdated} outdated, "
            f"{self.responses} with new responses; {self.orphans} future events without a Termin"
        )
        if self.pushed:
            text += f"; pushed {len(self.pushed)}: " + " | ".join(self.pushed)
        if self.failed:
            text += f"; push failed {len(self.failed)}: " + " | ".join(self.failed)
        if self.public:
            text += f"; public calendar {len(self.public)}: " + " | ".join(self.public)
        return text


def check(session: Session, calendar: CalDavCalendar, today: dt.date, base: str = "") -> CheckResult:
    """Compare the events with their Termine, read the responses, list events without a Termin, and
    push the pending changes: every Termin whose event is outdated gets it updated (`push`)."""
    result = CheckResult()
    now = dt.datetime.now(dt.UTC)
    events = {
        e.uid: e
        for e in calendar.events(today - dt.timedelta(days=1), today + dt.timedelta(days=LOOKAHEAD_DAYS))
    }
    linked = set(
        session.scalars(select(Appointment.calendar_uid).where(Appointment.calendar_uid.is_not(None)))
    )
    termine = session.scalars(
        select(Appointment)
        .where(
            Appointment.calendar_uid.is_not(None),
            Appointment.stage.in_((GEPLANT, *IN_CALENDAR, ABGESAGT)),
            (Appointment.date >= today) | Appointment.date.is_(None),
        )
        .options(
            selectinload(Appointment.course).selectinload(Course.place),
            selectinload(Appointment.course).selectinload(Course.context),
            selectinload(Appointment.course).selectinload(Course.format),
            selectinload(Appointment.lead),
            selectinload(Appointment.support),
            selectinload(Appointment.calendar_responses),
        )
    ).all()
    changed_ids = []
    for t in termine:
        actual = events.get(t.calendar_uid)
        if actual is None:
            found = calendar.get(t.calendar_uid)  # moved out of the window, or gone
            actual = found[0] if found else None
        before = (t.calendar_state, [(r.email, r.partstat) for r in t.calendar_responses])
        result.checked += 1
        if t.stage == GEPLANT:
            # Outdated: back in Geplant, the event still to be cancelled (`withdraw`). Otherwise a
            # Geplant Termin linked to an existing event (from "Prüfen", or entered before
            # 2026-11-13) keeps it as it is until it is confirmed.
            result.outdated += t.calendar_state == CAL_OUTDATED
        elif actual is None:
            t.calendar_state = CAL_MISSING if t.stage != ABGESAGT else None
            result.missing += t.stage != ABGESAGT
        else:
            expected = expected_event(t, base, calendar.organizer, previous=actual)
            outdated = expected is None or bool(changes(expected, actual))
            t.calendar_state = CAL_OUTDATED if outdated else CAL_OK
            result.outdated += outdated
            if _store_responses(session, t, actual):
                result.responses += 1
        t.calendar_checked_at = now
        if before != (t.calendar_state, [(r.email, r.partstat) for r in t.calendar_responses]):
            changed_ids.append(t.id)

    pushed_ids = set()
    for t in termine:
        if t.calendar_state != CAL_OUTDATED:
            continue
        pushed_ids.add(t.id)  # `push` also brings its public event in line
        try:
            note = push(session, calendar, t, base, AUTOMATIC)
        except CalendarError as error:
            result.failed.append(f"Termin {t.id}: {error}")
            continue
        if note and t.calendar_state != CAL_OUTDATED:  # outdated: no times, nothing was written
            result.pushed.append(f"Termin {t.id}: {note}")

    _check_public(session, [t for t in termine if t.id not in pushed_ids], today, result)

    seen = {
        uid: e
        for uid, e in events.items()
        if uid not in linked and e.start.date() >= today and e.status != "CANCELLED"
    }
    for orphan in session.scalars(select(CalendarOrphan)):
        if orphan.uid not in seen and orphan.ignored_by is None:
            session.delete(orphan)  # bookkeeping of the check, not a record
    for uid, e in seen.items():
        row = session.get(CalendarOrphan, uid) or CalendarOrphan(uid=uid)
        row.summary, row.starts_at, row.ends_at, row.location, row.seen_at = (
            e.summary,
            e.start,
            e.end,
            e.location,
            now,
        )
        session.add(row)
    result.orphans = len(seen)
    session.flush()
    hints.refresh(session, changed_ids)
    return result


def _check_public(session: Session, termine: list[Appointment], today: dt.date, result: CheckResult) -> None:
    """The public calendar's part of the check: one listing, then `sync_public` per Termin (e.g. a
    context newly marked, or unmarked, or a change the public calendar missed while it was down)."""
    public = get_public_calendar()
    if public is None:
        return
    try:
        listed = public.events(today - dt.timedelta(days=1), today + dt.timedelta(days=LOOKAHEAD_DAYS))
    except CalendarError as error:
        result.failed.append(f"öffentlicher Kalender: {error}")
        return
    found = {e.uid: e for e in listed}
    for t in termine:
        uid = public_uid(t)
        if uid is None:
            continue
        try:
            # Not listed: none, unless the Termin wants one (then ask, it may lie outside the window).
            wanted = t.context is not None and t.context.public_calendar
            current = (found[uid], None) if uid in found else (public.get(uid) if wanted else None)
            note = sync_public(session, t, AUTOMATIC, current=current)
        except CalendarError as error:
            result.failed.append(f"Termin {t.id} (öffentlich): {error}")
            continue
        if note:
            result.public.append(f"Termin {t.id}: {note}")


def responses(t: Appointment) -> list[tuple[str, str, str]]:
    """(name, e-mail, German answer) of the invited people, for the course page."""
    return [
        (r.person.name if r.person else r.email, r.email, PARTSTATS.get(r.partstat, r.partstat))
        for r in t.calendar_responses
    ]
