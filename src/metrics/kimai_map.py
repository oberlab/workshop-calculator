"""Where the Kimai Cloud API keeps what the app reads (WM-142): every path, parameter and field name.

Everything the app assumes about Kimai's JSON is in this one module, so it can be corrected in one
place once someone checks the instance's own API documentation (`<KIMAI_URL>/api/doc`). Built
without access to a real Kimai, from the public sources of 2026-11: kimai.org "Setting user
preferences" (api-example-user), the Kimai source (`src/API/UserController.php`, `src/Entity/User.php`,
`src/Entity/UserPreference.php`, `WorkingTimeCalculatorDay.php`) and a client generated from
Kimai's OpenAPI file (`kimai_client_lib`: `AbsenceApi`, `Absence`). `metrics kimai-check` prints what
the real instance answers, field by field.

Marked per item: [source] read in the sources above; [assumed] a guess to check on `/api/doc`.
"""

import datetime as dt
from decimal import Decimal

# --- Requests -------------------------------------------------------------------------------------

# [source] The API lives under /api; KIMAI_URL may be given with or without it.
API = "/api"
# [source] Authorization: Bearer <API token> (the old X-AUTH-USER / X-AUTH-TOKEN headers are gone).
# [source] GET /api/users lists users; needs the permission `view_user`. `visible=3` lists visible
# and hidden users alike (1 = visible, the default; 2 = hidden).
USERS = "/users"
USERS_QUERY = {"visible": "3"}
# [source] GET /api/users/{id}: one user with the serializer group `User_Entity`, which adds
# `preferences` (the working-contract settings are preferences); needs `view` on that profile.
USER = "/users/{id}"
# [source] GET /api/absences?user=<id>&begin=…&end=… (working-contract feature). `user` needs the
# permission `contract_other_profile` for anyone but the token's own user.
ABSENCES = "/absences"
ABSENCE_USER, ABSENCE_BEGIN, ABSENCE_END = "user", "begin", "end"
# [source] GET /api/version: a harmless request for `metrics kimai-check`.
VERSION = "/version"
# [source] GET /api/absences/types: the instance's absence types (key -> translated name).
ABSENCE_TYPES = "/absences/types"


def day_param(day: dt.date) -> str:
    """[source, format assumed] `begin`/`end` are "HTML5" date-times, as elsewhere in Kimai's API:
    2026-11-17T00:00:00 (local time of the instance)."""
    return f"{day.isoformat()}T00:00:00"


# --- Users ---------------------------------------------------------------------------------------

# [source] Fields of a user (serializer group Default).
USER_ID = "id"
USER_NAME = "username"
USER_ALIAS = "alias"  # the display name, may be empty
USER_ENABLED = "enabled"
# [source] `email` is in the Default group of User.php, so the list and the single user carry it.
# [assumed] The generated client does not list it; if the list lacks it, the single user has it
# (the app reads every matched user singly anyway for the preferences).
USER_EMAIL = "email"
USER_PREFERENCES = "preferences"  # [source] a list of {"name": …, "value": …}, values as strings
PREF_NAME, PREF_VALUE = "name", "value"


def email_key(value: object) -> str | None:
    """How e-mails are compared (Alex, 2026-11-17): whitespace trimmed, case ignored."""
    text = str(value or "").strip().casefold()
    return text or None


def preferences(user: dict) -> dict[str, str]:
    """A user's preferences as name -> value."""
    found = {}
    for item in user.get(USER_PREFERENCES) or []:
        if isinstance(item, dict) and item.get(PREF_NAME):
            found[str(item[PREF_NAME])] = "" if item.get(PREF_VALUE) is None else str(item[PREF_VALUE])
    return found


# [source] The working contract (kimai.org api-example-user): `work_contract_type` is day, week or
# month; times are in seconds.
CONTRACT_TYPE = "work_contract_type"
HOURS_PER_WEEK = "hours_per_week"  # contract type week
HOURS_PER_MONTH = "hours_per_month"  # contract type month
DAY_HOURS = (  # contract type day: expected time per weekday
    "work_monday",
    "work_tuesday",
    "work_wednesday",
    "work_thursday",
    "work_friday",
    "work_saturday",
    "work_sunday",
)
CONTRACT_PREFERENCES = (CONTRACT_TYPE, HOURS_PER_WEEK, HOURS_PER_MONTH, *DAY_HOURS)
# [assumed] A month contract becomes weekly hours as monthly hours × 12 / 52.
WEEKS_PER_MONTH = Decimal(52) / Decimal(12)


def _seconds(prefs: dict[str, str], name: str) -> Decimal | None:
    try:
        value = Decimal(prefs[name].strip())
    except (KeyError, ArithmeticError, ValueError):
        return None
    return value if value.is_finite() and value >= 0 else None


def weekly_hours(user: dict) -> Decimal | None:
    """The weekly working hours of the user's contract, rounded to 0.01 h; None without a contract.

    week: hours_per_week; month: hours_per_month × 12 / 52; day: the sum of work_monday … work_sunday.
    [assumed] Without a contract type (older instances) the day values count if any is set.
    """
    prefs = preferences(user)
    kind = (prefs.get(CONTRACT_TYPE) or "").strip().lower()
    seconds: Decimal | None = None
    if kind == "week":
        seconds = _seconds(prefs, HOURS_PER_WEEK)
    elif kind == "month":
        monthly = _seconds(prefs, HOURS_PER_MONTH)
        seconds = monthly / WEEKS_PER_MONTH if monthly is not None else None
    elif kind == "day" or (not kind and any(name in prefs for name in DAY_HOURS)):
        days = [_seconds(prefs, name) for name in DAY_HOURS]
        seconds = (
            sum((d for d in days if d is not None), Decimal(0)) if any(d is not None for d in days) else None
        )
    if seconds is None:
        return None
    hours = (seconds / Decimal(3600)).quantize(Decimal("0.01"))
    return hours if hours <= Decimal(168) else None


# --- Absences ------------------------------------------------------------------------------------

# [source] Fields of an absence (`Absence` of the OpenAPI file): id, user, date, duration, type,
# status, halfDay. [assumed] One absence is one day (a vacation of a week is five absences);
# `duration` (seconds of that day) is not used.
ABSENCE_ID = "id"
ABSENCE_DATE = "date"
ABSENCE_TYPE = "type"
ABSENCE_STATUS = "status"
ABSENCE_HALF_DAY = ("halfDay", "half_day")  # [assumed] JSON name halfDay; half_day as a fallback

# [source: kimai.org "Working times"; keys assumed] Kimai's types and the German kind the app keeps.
# Anything else is "Abwesend". The kind is shown only to the person themselves.
KINDS = {
    "holiday": "Urlaub",
    "sickness": "Krank",
    "sickness_child": "Krank",
    "time_off": "Freizeitausgleich",
    "parental": "Elternzeit",
    "unpaid_vacation": "Unbezahlter Urlaub",
    "other": "Abwesend",
}
OTHER_KIND = "Abwesend"

# [assumed] Statuses of the approval workflow that are not (yet) an absence: requested or rejected.
# Alex, 2026-11-17: the absences in Kimai are approved. Unknown statuses count, so a different
# spelling of "approved" is not silently dropped; `metrics kimai-check` prints the statuses it saw.
NOT_ABSENT = {"new", "requested", "rejected", "declined"}


def kind_of(absence: dict) -> str:
    return KINDS.get(str(absence.get(ABSENCE_TYPE) or "").strip().lower(), OTHER_KIND)


def counts(absence: dict) -> bool:
    return str(absence.get(ABSENCE_STATUS) or "").strip().lower() not in NOT_ABSENT


def day_of(absence: dict) -> dt.date | None:
    """The day of an absence: "2026-11-20" or a date-time starting with it."""
    try:
        return dt.date.fromisoformat(str(absence.get(ABSENCE_DATE) or "")[:10])
    except ValueError:
        return None


def half_day(absence: dict) -> bool:
    return any(absence.get(name) is True for name in ABSENCE_HALF_DAY)


def key_of(absence: dict, user_id: int, day: dt.date) -> str:
    """Kimai's absence ID; [assumed] without one, the user, day and type stand for it."""
    if absence.get(ABSENCE_ID) is not None:
        return str(absence[ABSENCE_ID])
    return f"{user_id}:{day.isoformat()}:{absence.get(ABSENCE_TYPE) or ''}"
