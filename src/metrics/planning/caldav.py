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

When the calendar is written:
- "In Kalender eintragen" (`enter`), only by a person, for a Geplant or Bestätigt Termin.
- After a save or stage change of a Termin that has an event (`push`): only when the event would
  change, so a save without calendar-relevant changes sends nothing.
- "Kalender aktualisieren" (`push` from the course page) after the course page showed what it
  would change (`preview`), e.g. when someone edited the event in the calendar.
The check (`check`, scheduler job "calendar" every 15 minutes) only reads: it marks events as
missing or outdated, stores the responses and lists future events without a Termin.

Standard library only. Times inside the app are naive local times (Europe/Berlin); events are
written in UTC.
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
IN_CALENDAR = (GEPLANT, BESTAETIGT)  # stages that are entered and kept up to date
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


def _utc(local: dt.datetime) -> str:
    return local.replace(tzinfo=LOCAL).astimezone(dt.UTC).strftime("%Y%m%dT%H%M%SZ")


def to_ics(event: Event) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "BEGIN:VEVENT",
        f"UID:{event.uid}",
        f"DTSTAMP:{dt.datetime.now(dt.UTC):%Y%m%dT%H%M%SZ}",
        f"DTSTART:{_utc(event.start)}",
        f"DTEND:{_utc(event.end)}",
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
    course = t.course
    status = {GEPLANT: "TENTATIVE", ABGESAGT: "CANCELLED"}.get(t.stage, "CONFIRMED")
    names = lambda role: ", ".join(p.name for p, r in _team(t) if r == role) or "–"  # noqa: E731
    lines = [
        f"Kontext: {course.context.name if course.context else '–'}",
        f"Klasse/Alter: {course.class_age_text or '–'}",
        f"Kursleitung: {names('Kursleitung')}",
        f"Unterstützung: {names('Unterstützung')}",
        f"Termin-ID: {t.id}",
    ]
    if t.stage == ABGESAGT and t.cancel_reason:
        lines.insert(0, f"Abgesagt: {t.cancel_reason}")
    if base:
        lines.append(f"{LINK_LINE}{base}/planung/{course.id}?termin={t.id}")
    title = hints.title(t)
    place = course.place.name if course.place else None
    event = Event(
        uid=uid or t.calendar_uid or new_uid(t),
        summary=" · ".join(x for x in (title, place) if x),
        start=dt.datetime.combine(t.date, t.start_time),
        end=dt.datetime.combine(t.date, t.end_time),
        location=place,
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


# --- Writing --------------------------------------------------------------------------------------


def _log(session: Session, t: Appointment, by: str, reason: str, new: str | None) -> None:
    lists._log(session, "appointments", t.id, "calendar_uid", None, new, by, reason)


def enter(session: Session, calendar: CalDavCalendar, t: Appointment, base: str, by: str) -> str:
    """ "In Kalender eintragen": create the event (or bring a known one up to date)."""
    if t.stage not in IN_CALENDAR:
        raise CalendarError("Nur geplante und bestätigte Termine kommen in den Kalender.")
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
    return "In den Kalender eingetragen." + (f" Eingeladen: {invited}." if invited else "")


def push(
    session: Session,
    calendar: CalDavCalendar,
    t: Appointment,
    base: str,
    by: str,
    current: tuple[Event, str | None] | None = None,
) -> str | None:
    """Bring the Termin's event up to date if it would change; returns what happened (or None).

    A Geplant or Bestätigt Termin updates its event; an Abgesagt one sets it to `STATUS:CANCELLED`.
    Nothing is sent when nothing changes. The event is never deleted.
    """
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


def cancel_event(
    session: Session, calendar: CalDavCalendar, t: Appointment, base: str, by: str
) -> str | None:
    """Before a Geplant Termin is deleted: its event is cancelled (attendees are told), not deleted."""
    if not t.calendar_uid or (current := calendar.get(t.calendar_uid)) is None:
        return None
    actual, etag = current
    if actual.status == "CANCELLED":
        return None
    cancelled = replace(actual, status="CANCELLED", sequence=actual.sequence + 1)
    cancelled.description = "\n".join(["Termin gelöscht.", *_description_lines(actual.description)])
    calendar.put(cancelled, etag=etag)
    _log(session, t, by, "im Kalender abgesagt (Termin gelöscht)", t.calendar_uid)
    return "Kalendereintrag abgesagt."


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


@dataclass
class CheckResult:
    checked: int = 0
    missing: int = 0
    outdated: int = 0
    responses: int = 0
    orphans: int = 0

    def summary(self) -> str:
        return (
            f"{self.checked} events checked: {self.missing} missing, {self.outdated} outdated, "
            f"{self.responses} with new responses; {self.orphans} future events without a Termin"
        )


def check(session: Session, calendar: CalDavCalendar, today: dt.date, base: str = "") -> CheckResult:
    """Compare the events with their Termine, read the responses, list events without a Termin.

    Reads only; writes the calendar state, the responses and the orphan list in the database.
    """
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
            Appointment.stage.in_((*IN_CALENDAR, ABGESAGT)),
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
        if actual is None:
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


def responses(t: Appointment) -> list[tuple[str, str, str]]:
    """(name, e-mail, German answer) of the invited people, for the course page."""
    return [
        (r.person.name if r.person else r.email, r.email, PARTSTATS.get(r.partstat, r.partstat))
        for r in t.calendar_responses
    ]
