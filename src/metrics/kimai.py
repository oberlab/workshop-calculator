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

`metrics kimai-check` (`check`) prints the matching with names and e-mails to the terminal, and
what the absences and user endpoints answer, field by field; it sends GET requests only and writes
nothing. Paths and field names live in `metrics.kimai_map`; the token is never printed or logged.
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
        url = self.base + path + ("?" + urllib.parse.urlencode(params) if params else "")
        request = urllib.request.Request(
            url,
            headers={"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
            method="GET",
        )
        try:
            with _OPENER.open(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode() or "null")
        except urllib.error.HTTPError as error:
            raise KimaiError(f"GET {path}: HTTP {error.code}") from None
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
        """Every enabled Kimai user, read singly (the single user carries e-mail and preferences)."""
        return [self.user(u[km.USER_ID]) for u in self.user_list() if u.get(km.USER_ENABLED) is not False]

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
        hours = km.weekly_hours(user)
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


def check(session: Session, kimai: Client, today: dt.date) -> list[str]:
    """What `metrics kimai-check` prints: the matching (with names, on screen only) and what the
    endpoints answer. GET requests only; reads the database, writes nothing."""
    lines = [f"Kimai: {kimai.base}"]
    try:
        version = kimai.get(km.VERSION)
        if isinstance(version, dict):
            lines.append(f"Version: {version.get('version') or version.get('semver') or '?'}")
    except KimaiError as error:
        lines.append(f"Version: nicht lesbar ({error})")
    listed = kimai.user_list()
    lines.append(f"GET {km.USERS}: {len(listed)} Nutzer; Felder der Liste: {_fields(listed)}")
    users = kimai.users()
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
    lines.append("")
    prefs = Counter(
        name for _, u, _ in matching.pairs for name in km.preferences(u) if name in km.CONTRACT_PREFERENCES
    )
    lines.append(
        "Arbeitsvertrag in den Einstellungen: "
        + (", ".join(f"{n} ({c}×)" for n, c in sorted(prefs.items())) or "keine der erwarteten Einstellungen")
    )
    begin, end = today - PAST, today + AHEAD
    found: list[dict] = []
    errors = []
    for _, user, _ in matching.pairs:
        try:
            found += kimai.absences(user[km.USER_ID], begin, end)
        except KimaiError as error:
            errors.append(str(error))
    lines.append(
        f"GET {km.ABSENCES} ({begin:%d.%m.%Y}–{end:%d.%m.%Y}): {len(found)} Abwesenheiten"
        + (f", {len(errors)} Fehler ({errors[0]})" if errors else "")
    )
    if found:
        lines.append(f"  Felder: {_fields(found)}")
        lines.append(f"  type: {_values(found, km.ABSENCE_TYPE)}")
        lines.append(f"  status: {_values(found, km.ABSENCE_STATUS)}")
        unreadable = sum(1 for a in found if km.day_of(a) is None)
        example = found[0].get(km.ABSENCE_DATE)
        lines.append(f"  Datum lesbar: {len(found) - unreadable} von {len(found)} (z. B. {example!r})")
        lines.append(f"  würden zählen: {sum(1 for a in found if km.counts(a) and km.day_of(a))}")
    try:
        types = kimai.get(km.ABSENCE_TYPES)
        lines.append(
            f"GET {km.ABSENCE_TYPES}: {', '.join(sorted(types)) if isinstance(types, dict) else types!r}"
        )
    except KimaiError as error:
        lines.append(f"GET {km.ABSENCE_TYPES}: {error}")
    return lines


def _fields(items: list[dict]) -> str:
    return ", ".join(sorted({k for item in items for k in item})) or "–"


def _values(items: list[dict], name: str) -> str:
    counts = Counter(str(item.get(name)) for item in items)
    return ", ".join(f"{v} ({n}×)" for v, n in sorted(counts.items()))
