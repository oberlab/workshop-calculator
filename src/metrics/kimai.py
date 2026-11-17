"""Absences and weekly hours from Kimai Cloud (WM-142, `PROPOSAL.md` 4.4). The app only reads Kimai.

The scheduler job `kimai` (every hour, `sync`) reads Kimai's users and matches them to the app's
people: the Kimai user an admin assigned on the Personen page (`people.kimai_user_id`) first, then
the e-mail (whitespace trimmed, case ignored). A Person's e-mail is never changed. For each matched
person it then

- stores the absences from `PAST` days ago to `AHEAD` days ahead in `absences`, one row per day
  (approved ones only, see `kimai_map.NOT_ABSENT`). Nothing is deleted: an absence of that window
  that is no longer in Kimai gets `withdrawn_at` and stops counting (a cancelled vacation); if it
  shows up again the mark goes. Rows outside the window, and rows of people who are no longer
  matched, stay as they are;
- sets `people.weekly_hours` from the working contract when Kimai has one, with a Verlauf row
  (changed by "kimai"); without a contract in Kimai the value entered by hand stays;
- recomputes the hints of the Termine on changed days and, after changed weekly hours, the
  person's Termine in planning from this week on ("abwesend", "ueber-kapazitaet").

Unmatched Kimai users and unmatched active people are counted in the job message, never named in a
log. If any request fails the job fails as a whole and changes nothing, so a Kimai outage never
withdraws absences. Without KIMAI_URL and KIMAI_TOKEN the job only says "Kimai is off".

Requests per run: the user list (it carries the e-mail), one single user per matched person (only
it has the preferences with the contract) and one absences request per matched person.

`metrics kimai-check` (`check`) prints to the terminal: the matching with names and e-mails, name
suggestions for unmatched people (never applied), the preference names over all users with counts
(never values) against those of the token's own user and its roles, and per Kimai user the status
and count of the absences request, plus one try each without `user`, with date-only `begin`/`end`
and of the calendar endpoint (field names only, no dates or names). It sends GET requests only and
writes nothing. Paths and field names live in `metrics.kimai_map`; the token is never printed or
logged.
"""

import datetime as dt
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from metrics import hints
from metrics import kimai_map as km
from metrics.config import get_settings
from metrics.db import ACTIVE_STATUSES, Absence, Appointment, HistoryEntry, Person

log = logging.getLogger(__name__)

ACTOR = "kimai"  # `changed_by` in the Verlauf
PAST = dt.timedelta(days=31)
AHEAD = dt.timedelta(days=400)
TIMEOUT = 30
OFF = "Kimai is off (KIMAI_URL and KIMAI_TOKEN not set)"


class KimaiError(Exception):
    """A request to Kimai failed (the message never holds the token)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib would follow a redirect with the Authorization header, to any host: refuse it, so the
    token only ever goes to KIMAI_URL (a redirect fails as its HTTP status)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


@dataclass
class Client:
    """GET requests to the Kimai API with the bearer token; nothing else is ever sent."""

    url: str
    token: str = field(repr=False)
    timeout: float = TIMEOUT

    @property
    def base(self) -> str:
        url = self.url.rstrip("/")
        return url if url.endswith(km.API) else url + km.API

    def get(self, path: str, params: dict[str, str] | None = None):
        status, body = self.try_get(path, params)
        if status != 200:
            raise KimaiError(f"GET {path}: HTTP {status}")
        return body

    def try_get(self, path: str, params: dict[str, str] | None = None) -> tuple[int, object]:
        """(HTTP status, JSON) of a GET; an HTTP error is a status here, not an exception (for
        `kimai-check`). A missing connection or an answer without JSON still raises."""
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:
                return response.status, json.loads(response.read().decode() or "null")
        except urllib.error.HTTPError as error:
            return error.code, None
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise KimaiError(f"GET {path}: Kimai not reachable ({type(reason).__name__})") from None
        except ValueError:
            raise KimaiError(f"GET {path}: no JSON in the answer") from None

    def user_list(self) -> list[dict]:
        found = self.get(km.USERS, km.USERS_QUERY)
        if not isinstance(found, list):
            raise KimaiError(f"GET {km.USERS}: expected a list")
        return [u for u in found if isinstance(u, dict) and isinstance(u.get(km.USER_ID), int)]

    def user(self, user_id: int) -> dict:
        found = self.get(km.USER.format(id=user_id))
        if not isinstance(found, dict):
            raise KimaiError(f"GET {km.USER}: expected an object")
        return found

    def users(self) -> list[dict]:
        """Every enabled Kimai user from the list, which carries the e-mail (Kimai 2.72); a user
        listed without one is read singly."""
        listed = [u for u in self.user_list() if u.get(km.USER_ENABLED) is not False]
        return [u if km.USER_EMAIL in u else self.user(u[km.USER_ID]) for u in listed]

    def with_preferences(self, user: dict) -> dict:
        """The user with its preferences (only the single user has them)."""
        return user if km.USER_PREFERENCES in user else self.user(user[km.USER_ID])

    def absences(self, user_id: int, begin: dt.date, end: dt.date) -> list[dict]:
        params = {
            km.ABSENCE_USER: str(user_id),
            km.ABSENCE_BEGIN: km.day_param(begin),
            km.ABSENCE_END: km.day_param(end),
        }
        found = self.get(km.ABSENCES, params)
        if not isinstance(found, list):
            raise KimaiError(f"GET {km.ABSENCES}: expected a list")
        return [a for a in found if isinstance(a, dict)]


def client() -> Client | None:
    """The client from KIMAI_URL and KIMAI_TOKEN, or None when Kimai is off."""
    settings = get_settings()
    if not settings.kimai_url or not settings.kimai_token:
        return None
    return Client(settings.kimai_url, settings.kimai_token)


# --- Matching ------------------------------------------------------------------------------------

MANUAL, EMAIL = "manuell", "E-Mail"


@dataclass
class Matching:
    pairs: list[tuple[Person, dict, str]] = field(default_factory=list)  # person, Kimai user, how
    people_without: list[Person] = field(default_factory=list)  # active people without a Kimai user
    users_without: list[dict] = field(default_factory=list)  # Kimai users without a person
    # Manual assignments naming a Kimai user that does not exist (or is disabled)
    unknown_ids: list[Person] = field(default_factory=list)

    def count(self, how: str) -> int:
        return sum(1 for _, _, h in self.pairs if h == how)


def candidates(session: Session) -> list[Person]:
    """The people Kimai users may match: proposed or confirmed, active or not."""
    stmt = select(Person).where(Person.status.in_(ACTIVE_STATUSES)).order_by(Person.name, Person.id)
    return list(session.scalars(stmt))


def match(people: Iterable[Person], users: Iterable[dict]) -> Matching:
    """The manual assignment first, then the e-mail (trimmed, case ignored); one person per user."""
    people, users = list(people), list(users)
    result = Matching()
    by_id = {u[km.USER_ID]: u for u in users}
    taken_users: set[int] = set()
    matched: set[int] = set()
    for person in people:
        if person.kimai_user_id is None:
            continue
        user = by_id.get(person.kimai_user_id)
        if user is None:
            result.unknown_ids.append(person)
            continue
        result.pairs.append((person, user, MANUAL))
        taken_users.add(user[km.USER_ID])
        matched.add(person.id)
    by_email: dict[str, list[Person]] = {}
    for person in people:
        if person.id not in matched and person.kimai_user_id is None and (key := km.email_key(person.email)):
            by_email.setdefault(key, []).append(person)
    for user in users:
        if user[km.USER_ID] in taken_users:
            continue
        found = by_email.get(km.email_key(user.get(km.USER_EMAIL)) or "", [])
        if len(found) == 1 and found[0].id not in matched:
            result.pairs.append((found[0], user, EMAIL))
            taken_users.add(user[km.USER_ID])
            matched.add(found[0].id)
    result.users_without = [u for u in users if u[km.USER_ID] not in taken_users]
    result.people_without = [p for p in people if p.active and p.id not in matched]
    return result


# --- The job -------------------------------------------------------------------------------------


@dataclass
class Result:
    matching: Matching
    new: int = 0
    changed: int = 0
    withdrawn: int = 0
    back: int = 0
    hours_changed: int = 0
    rechecked: int = 0

    def summary(self) -> str:
        m = self.matching
        return (
            f"{len(m.pairs)} people matched ({m.count(MANUAL)} by hand, {m.count(EMAIL)} by e-mail), "
            f"{len(m.users_without)} Kimai users and {len(m.people_without)} active people without a match"
            + (f", {len(m.unknown_ids)} assigned Kimai IDs unknown" if m.unknown_ids else "")
            + f"; absences: {self.new} new, {self.changed} changed, {self.withdrawn} withdrawn, "
            f"{self.back} back; weekly hours changed for {self.hours_changed}; "
            f"hints of {self.rechecked} Termine recomputed"
        )


def sync(session: Session, kimai: Client, today: dt.date, now: dt.datetime) -> Result:
    """Read Kimai and update absences and weekly hours (see the module docstring)."""
    matching = match(candidates(session), kimai.users())
    result = Result(matching)
    begin, end = today - PAST, today + AHEAD
    fetched: dict[tuple[str, dt.date], tuple[Person, str, bool]] = {}
    for person, user, _ in matching.pairs:
        for absence in kimai.absences(user[km.USER_ID], begin, end):
            day = km.day_of(absence)
            if day is None or not begin <= day <= end or not km.counts(absence):
                continue
            key = km.key_of(absence, user[km.USER_ID], day)
            fetched[(key, day)] = (person, km.kind_of(absence), km.half_day(absence))

    touched: set[dt.date] = set()
    matched_ids = {p.id for p, _, _ in matching.pairs}
    keys = {k for k, _ in fetched}
    stmt = select(Absence).where(Absence.day.between(begin, end))
    existing = {
        (row.kimai_key, row.day): row
        for row in session.scalars(stmt)
        if row.person_id in matched_ids or row.kimai_key in keys
    }
    for (key, day), (person, kind, half) in fetched.items():
        row = existing.get((key, day))
        if row is None:
            session.add(
                Absence(
                    person_id=person.id,
                    day=day,
                    kind=kind,
                    half_day=half,
                    kimai_key=key,
                    first_seen_at=now,
                    last_seen_at=now,
                )
            )
            result.new += 1
            touched.add(day)
            continue
        row.last_seen_at = now
        if row.withdrawn_at is not None:
            row.withdrawn_at = None
            result.back += 1
            touched.add(day)
        if (row.person_id, row.kind, row.half_day) != (person.id, kind, half):
            row.person_id, row.kind, row.half_day = person.id, kind, half
            result.changed += 1
            touched.add(day)
    for (key, day), row in existing.items():
        if (key, day) not in fetched and row.withdrawn_at is None and row.person_id in matched_ids:
            row.withdrawn_at = now
            result.withdrawn += 1
            touched.add(day)

    hours_people = set()
    for person, user, _ in matching.pairs:
        hours = km.weekly_hours(kimai.with_preferences(user))
        if hours is None or (person.weekly_hours is not None and Decimal(person.weekly_hours) == hours):
            continue
        session.add(
            HistoryEntry(
                table_name=Person.__tablename__,
                row_id=person.id,
                field="weekly_hours",
                old_value=None if person.weekly_hours is None else str(person.weekly_hours),
                new_value=str(hours),
                changed_by=ACTOR,
                changed_at=now,
                reason="Wochenstunden aus Kimai",
            )
        )
        person.weekly_hours = hours
        hours_people.add(person.id)
    result.hours_changed = len(hours_people)
    session.flush()
    result.rechecked = _recheck(session, touched, hours_people, today)
    return result


def _recheck(session: Session, days: set[dt.date], people: set[int], today: dt.date) -> int:
    """Recompute the hints of the Termine on these days and the people's Termine from this week on."""
    ids: set[int] = set()
    if days:
        ids |= set(session.scalars(select(Appointment.id).where(Appointment.date.in_(days))))
    if people:
        monday = today - dt.timedelta(days=today.weekday())
        stmt = select(Appointment.id).where(
            Appointment.date >= monday, Appointment.stage.in_(hints.PLANNED), hints._involving(people)
        )
        ids |= set(session.scalars(stmt))
    return hints.refresh(session, ids, today) if ids else 0


# --- The check command ---------------------------------------------------------------------------


def _who(user: dict) -> str:
    name = user.get(km.USER_ALIAS) or user.get(km.USER_NAME) or "?"
    return f"{name} (Kimai-ID {user.get(km.USER_ID)}, {user.get(km.USER_EMAIL) or 'ohne E-Mail'})"


def _norm(text: object) -> str:
    return " ".join(str(text or "").split()).casefold()


def _kimai_name(user: dict) -> str:
    return str(user.get(km.USER_ALIAS) or user.get(km.USER_NAME) or "")


def suggestions(matching: Matching) -> list[tuple[Person, dict, str]]:
    """For active people without a match: a Kimai user without a person whose full name is the
    person's name (case and spaces ignored), or, for a single name such as "Sarah", the only one
    whose first name starts with it ("Sarah Kühn"). Only shown; an admin assigns on the Personen page."""
    found = []
    for person in matching.people_without:
        name = _norm(person.name)
        if not name:
            continue
        same = [u for u in matching.users_without if _norm(_kimai_name(u)) == name]
        if len(same) == 1:
            found.append((person, same[0], "gleicher Name"))
            continue
        if " " in name:
            continue
        starts = [
            u for u in matching.users_without if (_norm(_kimai_name(u)).split() or [""])[0].startswith(name)
        ]
        if len(starts) == 1:
            found.append((person, starts[0], "Vorname"))
    return found


# What a status of the absences endpoint means, in German (`kimai-check`).
STATUS_TEXT = {
    200: "",
    400: "Anfrage abgelehnt: ein Parameter passt nicht",
    401: "Token ungültig oder abgelaufen",
    403: (
        "Zugriff verweigert: dem Token fehlt die Berechtigung "
        "(für Abwesenheiten anderer: contract_other_profile)"
    ),
    404: "nicht gefunden: Endpunkt oder Funktion (Arbeitszeit/Abwesenheiten) fehlt",
}


def _answer(kimai: Client, path: str, params: dict[str, str] | None) -> tuple[str, list[dict]]:
    """Status, count and the field names of the first entry (never values, dates or names), and the
    entries."""
    status, body = kimai.try_get(path, params)
    entries = [a for a in body if isinstance(a, dict)] if status == 200 and isinstance(body, list) else []
    text = f"HTTP {status}"
    if STATUS_TEXT.get(status):
        text += f" ({STATUS_TEXT[status]})"
    if status == 200:
        if isinstance(body, list):
            text += f", {len(body)} Einträge"
            if body and isinstance(body[0], dict):
                text += f"; Felder: {', '.join(sorted(body[0]))}"
        else:
            text += f", Antwort ist {type(body).__name__}, keine Liste"
    return text, entries


def check(session: Session, kimai: Client, today: dt.date) -> list[str]:
    """What `metrics kimai-check` prints: the matching (with names, on screen only), name
    suggestions, the preference names (never values) and what the absence endpoints answer.
    GET requests only; reads the database, writes nothing."""
    lines = [f"Kimai: {kimai.base}"]
    try:
        version = kimai.get(km.VERSION)
        if isinstance(version, dict):
            lines.append(f"Version: {version.get('version') or version.get('semver') or '?'}")
    except KimaiError as error:
        lines.append(f"Version: nicht lesbar ({error})")
    listed = kimai.user_list()
    lines.append(f"GET {km.USERS}: {len(listed)} Nutzer; Felder der Liste: {_fields(listed)}")
    users = [kimai.with_preferences(u) for u in kimai.users()]
    lines.append(f"GET {km.USER}: Felder: {_fields(users)}")
    matching = match(candidates(session), users)
    lines.append("")
    lines.append(
        f"Zugeordnet: {len(matching.pairs)} ({matching.count(MANUAL)} von Hand, "
        f"{matching.count(EMAIL)} per E-Mail)"
    )
    for person, user, how in sorted(matching.pairs, key=lambda x: x[0].name.casefold()):
        hours = km.weekly_hours(user)
        shown = f"{hours} Std./Woche" if hours is not None else "kein Arbeitsvertrag mit Stunden"
        lines.append(f"  {person.name} = {_who(user)} [{how}], {shown}")
    lines.append(f"Aktive Personen ohne Kimai-Nutzer: {len(matching.people_without)}")
    lines += [f"  {p.name} ({p.email or 'ohne E-Mail'})" for p in matching.people_without]
    lines.append(f"Kimai-Nutzer ohne Person: {len(matching.users_without)}")
    lines += [f"  {_who(u)}" for u in matching.users_without]
    if matching.unknown_ids:
        lines.append("Von Hand zugeordnete Kimai-IDs, die es (aktiv) nicht gibt:")
        lines += [f"  {p.name}: {p.kimai_user_id}" for p in matching.unknown_ids]
    proposals = suggestions(matching)
    if proposals:
        lines.append("Vorschläge (nichts wird automatisch zugeordnet; ein Admin trägt die Kimai-Nutzer-ID")
        lines.append("auf der Personen-Seite ein):")
        for person, user, why in proposals:
            lines.append(f"  Vorschlag: {person.name} → {_who(user)} [{why}]")

    lines.append("")
    lines += _preference_lines(kimai, users)
    lines.append("")
    lines += _absence_lines(kimai, users, today)
    return lines


def _preference_lines(kimai: Client, users: list[dict]) -> list[str]:
    """The preference names over all users (how many users have each), never their values, and
    whether the token's own user shows more of them (a permission limit)."""
    names = Counter(name for u in users for name in km.preferences(u))
    contract = [n for n in km.CONTRACT_PREFERENCES if n in names]
    lines = [
        "Einstellungen (Namen, Anzahl Nutzer): "
        + (", ".join(f"{n} ({c})" for n, c in sorted(names.items())) or "keine"),
        "Arbeitsvertrag in den Einstellungen: "
        + (", ".join(f"{n} ({names[n]})" for n in contract) or "keine der erwarteten Einstellungen"),
    ]
    status, me = kimai.try_get(km.ME)
    if status != 200 or not isinstance(me, dict):
        lines.append(f"GET {km.ME}: HTTP {status}")
        return lines
    roles = [str(r) for r in me.get(km.USER_ROLES) or []]
    lines.append(f"Token-Nutzer: Kimai-ID {me.get(km.USER_ID)}, Rollen: {', '.join(roles) or 'keine'}")
    own = set(km.preferences(me))
    others = [set(km.preferences(u)) for u in users if u.get(km.USER_ID) != me.get(km.USER_ID)]
    most = max((len(o) for o in others), default=0)
    extra = sorted(own - set().union(*others)) if others else sorted(own)
    lines.append(f"  eigene Einstellungen: {len(own)}, bei anderen Nutzern höchstens {most}")
    if extra:
        lines.append(f"  nur beim Token-Nutzer sichtbar: {', '.join(extra)}")
        lines.append(
            "  Hinweis: Der Token sieht bei anderen Nutzern weniger Einstellungen; vermutlich fehlt eine "
            "Berechtigung (z. B. contract_other_profile)."
        )
    return lines


def _absence_lines(kimai: Client, users: list[dict], today: dt.date) -> list[str]:
    """What the absence endpoints answer: per Kimai user, then once without `user`, once with
    date-only `begin`/`end` and once the calendar endpoint. Counts and field names only."""
    begin, end = today - PAST, today + AHEAD
    window = {km.ABSENCE_BEGIN: km.day_param(begin), km.ABSENCE_END: km.day_param(end)}
    lines = [f"GET {km.ABSENCES} ({begin:%d.%m.%Y}–{end:%d.%m.%Y}) je Kimai-Nutzer:"]
    found: list[dict] = []
    for user in sorted(users, key=lambda u: u[km.USER_ID]):
        params = {km.ABSENCE_USER: str(user[km.USER_ID]), **window}
        text, entries = _answer(kimai, km.ABSENCES, params)
        lines.append(f"  Kimai-ID {user[km.USER_ID]}: {text}")
        found += entries
    dates = {km.ABSENCE_BEGIN: begin.isoformat(), km.ABSENCE_END: end.isoformat()}
    for label, path, params in (
        ("Ohne user (eigene des Tokens)", km.ABSENCES, window),
        ("Ohne user, Datum ohne Uhrzeit (JJJJ-MM-TT)", km.ABSENCES, dates),
        (f"GET {km.ABSENCES_CALENDAR}", km.ABSENCES_CALENDAR, window),
    ):
        lines.append(f"{label}: {_answer(kimai, path, params)[0]}")
    if found:
        lines.append(f"Alle Abwesenheiten zusammen: {len(found)}")
        lines.append(f"  type: {_values(found, km.ABSENCE_TYPE)}")
        lines.append(f"  status: {_values(found, km.ABSENCE_STATUS)}")
        unreadable = sum(1 for a in found if km.day_of(a) is None)
        lines.append(f"  Datum lesbar: {len(found) - unreadable} von {len(found)}")
        lines.append(f"  würden zählen: {sum(1 for a in found if km.counts(a) and km.day_of(a))}")
    try:
        types = kimai.get(km.ABSENCE_TYPES)
        shown = ", ".join(sorted(types)) if isinstance(types, dict) else type(types).__name__
        lines.append(f"GET {km.ABSENCE_TYPES}: {shown}")
    except KimaiError as error:
        lines.append(f"GET {km.ABSENCE_TYPES}: {error}")
    return lines


def _fields(items: list[dict]) -> str:
    return ", ".join(sorted({k for item in items for k in item})) or "–"


def _values(items: list[dict], name: str) -> str:
    counts = Counter(str(item.get(name)) for item in items)
    return ", ".join(f"{v} ({n}×)" for v, n in sorted(counts.items()))
