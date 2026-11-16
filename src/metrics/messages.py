"""Slack messages of the bot after the switch (WM-130, PROPOSAL.md 4.7 and 4.10).

Three scheduler jobs, each with its own switch in the instance config (`[messages]`, all off by
default) and all of them off while SLACK_BOT_TOKEN is not set:

- After the course (`after_course`, daily at 09:00): a DM to the Kursleitung of a Termin from
  Bestätigt on whose participants or Herkunft are still missing (`planning.after.gaps`), with the
  link to the phone form. Sent 1, 2, 3 and 7 days after the Termin (local dates), at most one DM per
  Termin and day, and no more once the form is filled in or the Termin is Abgeschlossen. Afterwards
  Mein Bereich and the hints keep showing the task.
- The day before (`reminder`, daily at 15:00): a DM to each person of the team (lead and support) of
  every Bestätigt Termin of tomorrow: time, place, team. Moving the Termin to another day gives a new
  reminder for the new day.
- Weekly status (`weekly_status`, Monday at 09:00, caught up on a later day of the same week if
  Monday's run was missed): to the team channel (`channel`): the held Termine and participants of
  this calendar year, counted exactly like the key figures on the front page (`analytics.totals`,
  period "this_year"), and the Termine of this week from today to Sunday (Bestätigt, Geplant
  marked). One message per week (key `weekly_status:{monday}`).

At most once: every message gets a key (occasion, Termin, date, person) and a row in
`slack_messages` is written before the request goes out. A row that is `sent`, still `sending` or
`unclear` (the request broke off and may have arrived) is never sent again; only a `failed` row
(Slack answered with an error, so nothing was posted) is tried again while its occasion lasts. A
job with failed messages is recorded as failed, so the scheduler retries it an hour later.

This module is the messaging part of the Slack integration and stays after WM-161; the one-time
transfer (`ingest/slack.py`, `ingest/transfer.py`) is separate. Logs name counts only, never people,
Slack IDs or message texts.
"""

import datetime as dt
import json
import logging
import urllib.request
from dataclasses import dataclass, field
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session, selectinload

from metrics import analytics, hints
from metrics.config import get_settings
from metrics.db import (
    BESTAETIGT,
    DURCHGEFUEHRT,
    GEPLANT,
    Appointment,
    Course,
    Person,
    SlackMessage,
    get_sessionmaker,
)
from metrics.instance import get_instance
from metrics.planning import after
from metrics.planning.workload import monday_of
from metrics.scheduler import LOCAL

log = logging.getLogger(__name__)

API_URL = "https://slack.com/api/chat.postMessage"

AFTER_COURSE, REMINDER, WEEKLY_STATUS = "after_course", "reminder", "weekly_status"
# Days after the Termin on which the lead gets the DM while something is missing.
AFTER_DAYS = (1, 2, 3, 7)
# Stages that get messages about a Termin: from Bestätigt on, never Geplant or Abgesagt.
REMINDED = (BESTAETIGT, DURCHGEFUEHRT)
# The weekly status goes out on Monday; a later day of the same week catches up if Monday's run was
# missed (one message per week, so it never goes out twice).
WEEKDAYS = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")

SENDING, SENT, FAILED, UNCLEAR = "sending", "sent", "failed", "unclear"


# --- The Slack client ---------------------------------------------------------------------------


class SlackError(RuntimeError):
    """Slack answered `ok: false`: the message was not posted."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class Client(Protocol):
    def post(self, channel: str, text: str) -> str:
        """Post `text` (Slack mrkdwn) to a channel or, with a user ID, as a DM; returns the message ts."""
        ...


@dataclass
class SlackClient:
    """`chat.postMessage` with the bot token (scope `chat:write`)."""

    token: str
    timeout: float = 30

    def post(self, channel: str, text: str) -> str:
        body = json.dumps({"channel": channel, "text": text, "unfurl_links": False}).encode()
        request = urllib.request.Request(
            API_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
            },
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            result = json.load(response)
        if not result.get("ok"):
            raise SlackError(str(result.get("error") or "unknown_error"))
        return str(result.get("ts") or "")


def get_client() -> Client | None:
    """The bot, or None while SLACK_BOT_TOKEN is not set (then no message is sent)."""
    token = get_settings().slack_bot_token
    return SlackClient(token) if token else None


# --- Sending at most once -----------------------------------------------------------------------


@dataclass
class Outcome:
    sent: int = 0
    skipped: int = 0  # already sent (or unclear) before
    failed: list[str] = field(default_factory=list)  # Slack error codes

    def summary(self, what: str) -> str:
        text = f"{self.sent} {what} sent, {self.skipped} already sent"
        if self.failed:
            text += f", {len(self.failed)} failed ({', '.join(sorted(set(self.failed)))})"
        return text


class MessagesFailed(RuntimeError):
    """Some messages could not be sent; the scheduler records the run as failed and retries."""


def _finish(
    row_id: int, status: str, now: dt.datetime, ts: str | None = None, error: str | None = None
) -> None:
    with get_sessionmaker()() as session, session.begin():
        row = session.get_one(SlackMessage, row_id)
        row.status, row.ts, row.error = status, ts, error
        if status == SENT:
            row.sent_at = now


def deliver(
    client: Client,
    outcome: Outcome,
    now: dt.datetime,
    *,
    key: str,
    kind: str,
    channel: str,
    text: str,
    appointment_id: int | None = None,
    person_id: int | None = None,
) -> None:
    row_id = claim(key, kind, channel, appointment_id, person_id)
    if row_id is None:
        outcome.skipped += 1
        return
    try:
        ts = client.post(channel, text)
    except SlackError as error:
        _finish(row_id, FAILED, now, error=error.code[:200])
        outcome.failed.append(error.code)
        return
    except Exception as error:  # timeout, connection reset: it may have been posted
        _finish(row_id, UNCLEAR, now, error=type(error).__name__)
        outcome.failed.append(type(error).__name__)
        return
    _finish(row_id, SENT, now, ts=ts)
    outcome.sent += 1


def claim(key: str, kind: str, channel: str, appointment_id: int | None, person_id: int | None) -> int | None:
    """Record the message as `sending` before it goes out; returns its row, or None when it must not
    be sent: a row with this key exists and is not `failed`."""
    with get_sessionmaker()() as session, session.begin():
        inserted = session.scalar(
            insert(SlackMessage)
            .values(
                kind=kind,
                key=key,
                channel=channel,
                status=SENDING,
                appointment_id=appointment_id,
                person_id=person_id,
            )
            .on_conflict_do_nothing(index_elements=["key"])
            .returning(SlackMessage.id)
        )
        if inserted is not None:
            return inserted
        row = session.scalars(select(SlackMessage).where(SlackMessage.key == key).with_for_update()).one()
        if row.status != FAILED:
            return None
        row.status, row.attempts, row.error, row.channel = SENDING, row.attempts + 1, None, channel
        return row.id


# --- Texts ----------------------------------------------------------------------------------------


def escape(text: str) -> str:
    """Slack mrkdwn treats &, < and > as markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def link(url: str, label: str) -> str:
    return f"<{url}|{escape(label)}>"


def day_label(day: dt.date) -> str:
    return f"{WEEKDAYS[day.weekday()]} {day:%d.%m.}"


def time_label(t: Appointment) -> str:
    if t.start_time and t.end_time:
        return f"{t.start_time:%H:%M}–{t.end_time:%H:%M} Uhr"
    if t.start_time:
        return f"ab {t.start_time:%H:%M} Uhr"
    return "Uhrzeit noch offen"


def title(t: Appointment) -> str:
    return escape(hints.title(t))


def place_label(t: Appointment, with_address: bool = False) -> str:
    place = t.course.place
    if place is None:
        return "Ort noch offen"
    if with_address and place.address:
        return escape(f"{place.name}, {place.address}")
    return escape(place.name)


def team_label(t: Appointment) -> str:
    names = [f"{t.lead.name} (Kursleitung)"] if t.lead else []
    names += [p.name for p in t.support]
    return escape(", ".join(names)) if names else "noch niemand"


def _base() -> str:
    return get_settings().public_url or ""


def after_course_text(t: Appointment, days: int) -> str:
    missing = after.gaps(t)
    what = " und ".join(
        x for x, gap in (("die Teilnehmenden", missing.counts), ("die Herkunft", missing.origins)) if gap
    )
    when = "gestern" if days == 1 else f"am {day_label(t.date)}"
    if days == AFTER_DAYS[0]:
        lead = f"Hallo! Wie lief *{title(t)}* {when} ({place_label(t)})? Bitte trag noch {what} ein."
    elif days == AFTER_DAYS[-1]:
        lead = f"Letzte Erinnerung: Für *{title(t)}* {when} ({place_label(t)}) fehlen noch {what}."
    else:
        lead = f"Erinnerung: Für *{title(t)}* {when} ({place_label(t)}) fehlen noch {what}."
    base = _base()
    path = f"/mein-bereich/nach-dem-kurs/{t.id}"
    action = link(base + path, "Jetzt eintragen") if base else "Eintragen unter „Mein Bereich“ in Zahlen."
    return f"{lead}\n{action}"


def reminder_text(t: Appointment) -> str:
    course = t.course
    lines = [
        f"Morgen, {day_label(t.date)}: *{title(t)}*"
        + (f" ({escape(course.context.name)})" if course.context else ""),
        f"• Zeit: {time_label(t)}",
        f"• Ort: {place_label(t, with_address=True)}",
        f"• Team: {team_label(t)}",
    ]
    if course.class_age_text:
        lines.append(f"• Klasse/Alter: {escape(course.class_age_text)}")
    # Equipment joins here once Termine have it (WM-140).
    if base := _base():
        lines.append(link(f"{base}/planung/{course.id}?termin={t.id}", "Termin ansehen"))
    return "\n".join(lines)


def number(value: int) -> str:
    """1.234 like the front page (German thousands separator)."""
    return f"{value:,}".replace(",", ".")


@dataclass(frozen=True)
class WeeklyFigures:
    year: int
    totals: analytics.Totals  # held Termine of the calendar year, as on the front page
    termine: list[Appointment]  # the rest of this week, Geplant and Bestätigt


def weekly_text(monday: dt.date, figures: WeeklyFigures) -> str:
    sunday = monday + dt.timedelta(days=6)
    totals = figures.totals
    lines = [
        f"*Wochenstatus für {monday:%d.%m.}–{sunday:%d.%m.%Y}*",
        "",
        f"*{figures.year} bisher:* {number(totals.sessions)} Einsätze, "
        f"{number(totals.participants)} Teilnehmende",
        "",
    ]
    if figures.termine:
        lines.append(f"*Termine diese Woche ({len(figures.termine)})*")
        for t in figures.termine:
            when = f"{day_label(t.date)} {t.start_time:%H:%M}" if t.start_time else day_label(t.date)
            parts = [when, f"*{title(t)}*", place_label(t), escape(t.lead.name) if t.lead else "ohne Leitung"]
            stage = " _(geplant)_" if t.stage == GEPLANT else ""
            lines.append("• " + " · ".join(parts) + stage)
    else:
        lines.append("Diese Woche stehen keine Termine mehr an.")
    if base := _base():
        lines += ["", link(base + "/kalender", "Kalender")]
    return "\n".join(lines)


# --- What is due --------------------------------------------------------------------------------


def _load(stmt):
    return stmt.options(
        selectinload(Appointment.course).selectinload(Course.format),
        selectinload(Appointment.course).selectinload(Course.context),
        selectinload(Appointment.course).selectinload(Course.place),
        selectinload(Appointment.lead),
        selectinload(Appointment.support),
        selectinload(Appointment.origins),
    )


def _reachable(person: Person | None) -> bool:
    return person is not None and person.active and bool(person.slack_id)


def after_course_due(session: Session, today: dt.date) -> list[tuple[Appointment, int]]:
    """Termine whose lead gets the DM today, with the days since the Termin."""
    earliest = today - dt.timedelta(days=max(AFTER_DAYS))
    found = session.scalars(
        _load(select(Appointment))
        .where(
            Appointment.stage.in_(REMINDED),
            Appointment.date >= earliest,
            Appointment.date < today,
            Appointment.lead_id.is_not(None),
        )
        .order_by(Appointment.date, Appointment.id)
    )
    due = []
    for t in found:
        days = (today - t.date).days
        if days in AFTER_DAYS and _reachable(t.lead) and after.open_for_entry(t) and after.gaps(t).any:
            due.append((t, days))
    return due


def reminders_due(session: Session, today: dt.date) -> list[tuple[Appointment, Person]]:
    """(Termin, person) for every person of the team of tomorrow's Bestätigt Termine."""
    tomorrow = today + dt.timedelta(days=1)
    found = session.scalars(
        _load(select(Appointment))
        .where(Appointment.stage.in_(REMINDED), Appointment.date == tomorrow)
        .order_by(Appointment.start_time.nulls_last(), Appointment.id)
    )
    due = []
    for t in found:
        team = ([t.lead] if t.lead else []) + [p for p in t.support if p is not t.lead]
        due += [(t, p) for p in dict.fromkeys(team) if _reachable(p)]
    return due


def weekly_figures(session: Session, today: dt.date) -> WeeklyFigures:
    """The year's totals as the front page shows them on `today`, and this week's Termine from
    `today` to Sunday."""
    instance = get_instance()
    year_total = analytics.totals(
        session, analytics.Filters(preset="this_year"), today, instance.school_year_start_month
    )
    sunday = monday_of(today) + dt.timedelta(days=6)
    termine = list(
        session.scalars(
            _load(select(Appointment))
            .where(
                Appointment.stage.in_((GEPLANT, BESTAETIGT)),
                Appointment.date >= today,
                Appointment.date <= sunday,
            )
            .order_by(Appointment.date, Appointment.start_time.nulls_last(), Appointment.id)
        )
    )
    return WeeklyFigures(year=today.year, totals=year_total, termine=termine)


# --- The jobs -----------------------------------------------------------------------------------


def _ready(switch: str) -> tuple[Client | None, str | None]:
    """The client, or why this message type is off right now (for job_runs)."""
    client = get_client()
    if client is None:
        log.info("Slack messaging is off: SLACK_BOT_TOKEN is not set")
        return None, "messaging off: SLACK_BOT_TOKEN not set"
    if not getattr(get_instance().messages, switch):
        return None, f"switched off ([messages] {switch})"
    return client, None


def _done(outcome: Outcome, what: str) -> str:
    summary = outcome.summary(what)
    log.info("Slack %s", summary)
    if outcome.failed:
        raise MessagesFailed(summary)
    return summary


def send_after_course(now: dt.datetime) -> str:
    client, off = _ready(AFTER_COURSE)
    if client is None:
        return off or ""
    today = now.astimezone(LOCAL).date()
    with get_sessionmaker()() as session:
        due = [
            (t, days, t.lead.slack_id, t.lead_id, after_course_text(t, days))
            for t, days in after_course_due(session, today)
        ]
    outcome = Outcome()
    for t, days, slack_id, person_id, text in due:
        deliver(
            client,
            outcome,
            now,
            key=f"{AFTER_COURSE}:{t.id}:{t.date.isoformat()}:{days}",
            kind=AFTER_COURSE,
            channel=slack_id,
            text=text,
            appointment_id=t.id,
            person_id=person_id,
        )
    return _done(outcome, "after-course DMs")


def send_reminders(now: dt.datetime) -> str:
    client, off = _ready(REMINDER)
    if client is None:
        return off or ""
    today = now.astimezone(LOCAL).date()
    with get_sessionmaker()() as session:
        due = [(t.id, t.date, p.id, p.slack_id, reminder_text(t)) for t, p in reminders_due(session, today)]
    outcome = Outcome()
    for t_id, day, person_id, slack_id, text in due:
        deliver(
            client,
            outcome,
            now,
            key=f"{REMINDER}:{t_id}:{day.isoformat()}:{person_id}",
            kind=REMINDER,
            channel=slack_id,
            text=text,
            appointment_id=t_id,
            person_id=person_id,
        )
    return _done(outcome, "reminders")


def send_weekly_status(now: dt.datetime) -> str:
    client, off = _ready(WEEKLY_STATUS)
    if client is None:
        return off or ""
    channel = get_instance().messages.channel
    if not channel:
        return "no team channel configured ([messages] channel)"
    today = now.astimezone(LOCAL).date()
    monday = monday_of(today)
    # Monday's run, or the first run of the week after it was missed; the key keeps it to one.
    with get_sessionmaker()() as session:
        text = weekly_text(monday, weekly_figures(session, today))
    outcome = Outcome()
    deliver(
        client,
        outcome,
        now,
        key=f"{WEEKLY_STATUS}:{monday.isoformat()}",
        kind=WEEKLY_STATUS,
        channel=channel,
        text=text,
    )
    return _done(outcome, "weekly status")
