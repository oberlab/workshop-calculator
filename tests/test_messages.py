"""Slack reminders and status messages (WM-130). PostgreSQL, demo data and a fake Slack client only.

Done when: a test Termin produces each message once and none twice after a restart. The first test
runs the scheduler hour by hour around a Termin, restarts it and runs everything again.
"""

import dataclasses
import datetime as dt
import logging
from datetime import time

import pytest
import sqlalchemy as sa

from metrics import messages, scheduler
from metrics.db import SlackMessage
from metrics.instance import Messages, load_instance
from metrics.planning import workflow
from metrics.planning.workload import monday_of
from metrics.scheduler import LOCAL, Scheduler
from tests.conftest import EXAMPLE_INSTANCE
from tests.test_mine import _termin, world  # noqa: F401 (fixture)

BASE = "https://zahlen.example.org"
CHANNEL = "C0TEAM0001"
TODAY = dt.date.today()
# A Thursday at least a week ahead: the day after is a Friday, the day of the weekly status.
THURSDAY = TODAY + dt.timedelta(days=(3 - TODAY.weekday()) % 7 + 7)
WEDNESDAY, FRIDAY = THURSDAY - dt.timedelta(days=1), THURSDAY + dt.timedelta(days=1)
NEXT_MONDAY = monday_of(FRIDAY) + dt.timedelta(days=7)
SLACK_JOBS = ("slack-after-course", "slack-weekly-status", "slack-reminders")
JOBS = tuple(j for j in scheduler.JOBS if j.name in ("stage-moves", "hints", *SLACK_JOBS))


def at(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, time(hour, minute), tzinfo=LOCAL)


class FakeSlack:
    """Records what would be posted; `errors` makes the next posts fail like Slack would."""

    def __init__(self):
        self.posts: list[tuple[str, str]] = []
        self.errors: list[Exception] = []

    def post(self, channel: str, text: str) -> str:
        if self.errors:
            raise self.errors.pop(0)
        self.posts.append((channel, text))
        return f"1700000000.{len(self.posts):06d}"

    def to(self, channel: str) -> list[str]:
        return [text for c, text in self.posts if c == channel]


class Clock:
    def __init__(self, now: dt.datetime):
        self.now = now

    def __call__(self) -> dt.datetime:
        return self.now


@pytest.fixture
def slack(monkeypatch):
    fake = FakeSlack()
    monkeypatch.setattr(messages, "get_client", lambda: fake)
    monkeypatch.setenv("PUBLIC_URL", BASE)
    switched_on(monkeypatch)
    return fake


def switched_on(monkeypatch, **switches):
    values = dict(after_course=True, reminder=True, weekly_status=True, channel=CHANNEL) | switches
    instance = dataclasses.replace(load_instance(EXAMPLE_INSTANCE), messages=Messages(**values))
    monkeypatch.setattr(messages, "get_instance", lambda: instance)


@pytest.fixture
def team(world):  # noqa: F811
    w = world
    w.kim.slack_id, w.lea.slack_id, w.sam.slack_id = "U0KIM00001", "U0LEA00001", "U0SAM00001"
    w.db.commit()
    return w


def confirmed(w, day, lead, support=(), **extra):
    t = _termin(w, day, lead, support, **extra)
    workflow.confirm(w.db, t, None, "Test")
    return t


def rows(db) -> list[SlackMessage]:
    db.expire_all()
    return list(db.scalars(sa.select(SlackMessage).order_by(SlackMessage.id)))


def tick_hourly(loop: Scheduler, clock: Clock, until: dt.datetime) -> None:
    while clock.now < until:
        loop.tick()
        clock.now += dt.timedelta(hours=1)


def test_a_termin_gets_each_message_once_and_none_twice_after_a_restart(team, slack):
    w = team
    t = confirmed(w, THURSDAY, w.kim, [w.lea])
    planned = _termin(w, THURSDAY, w.sam)  # Geplant: no reminder, no DM
    cancelled = confirmed(w, THURSDAY, w.sam)
    workflow.cancel(w.db, cancelled, "Schule hat abgesagt", "Test")
    nxt = _termin(w, NEXT_MONDAY + dt.timedelta(days=1), w.lea)  # next week, still Geplant
    w.db.commit()

    clock = Clock(at(WEDNESDAY, 8))
    tick_hourly(Scheduler(jobs=JOBS, clock=clock), clock, at(FRIDAY, 17))

    # The day before: everyone on the team, nobody else.
    [kim_reminder] = [m for m in slack.to("U0KIM00001") if m.startswith("Morgen")]
    [lea_reminder] = slack.to("U0LEA00001")
    assert kim_reminder == lea_reminder
    assert "*Löten*" in kim_reminder and "09:00–11:15 Uhr" in kim_reminder
    assert "Grundschule Musterstadt" in kim_reminder and "Kim (Kursleitung), Lea" in kim_reminder
    assert f"<{BASE}/planung/{t.course_id}?termin={t.id}|Termin ansehen>" in kim_reminder
    assert slack.to("U0SAM00001") == []

    # The day after: the lead, with the phone form.
    [after_dm] = [m for m in slack.to("U0KIM00001") if not m.startswith("Morgen")]
    assert after_dm.startswith("Hallo! Wie lief *Löten* gestern")
    assert "die Teilnehmenden" in after_dm
    assert f"<{BASE}/mein-bereich/nach-dem-kurs/{t.id}|Jetzt eintragen>" in after_dm

    # Friday: the weekly status to the team channel, with next week's Termin.
    [weekly] = slack.to(CHANNEL)
    assert weekly.startswith(f"*Wochenstatus für {NEXT_MONDAY:%d.%m.}")
    assert f"Di {nxt.date:%d.%m.} 09:00 · *Löten* · Grundschule Musterstadt · Lea _(geplant)_" in weekly
    assert "Offene Hinweise:" in weekly and f"<{BASE}/pruefen|Prüfen>" in weekly
    assert len(slack.posts) == 4

    sent = rows(w.db)
    assert [(r.kind, r.status) for r in sent] == [
        ("reminder", "sent"),
        ("reminder", "sent"),
        ("after_course", "sent"),
        ("weekly_status", "sent"),
    ]
    assert {r.appointment_id for r in sent} == {t.id, None}
    assert planned.id not in {r.appointment_id for r in sent}

    # Restart: a new scheduler with fresh caches runs on through the evening and night ...
    from metrics.config import get_settings
    from metrics.db import get_sessionmaker

    get_settings.cache_clear()
    get_sessionmaker.cache_clear()
    loop = Scheduler(jobs=JOBS, clock=clock)
    tick_hourly(loop, clock, at(FRIDAY + dt.timedelta(days=1), 8, 30))
    # ... and every job run again at the moments the messages went out (as if job_runs were lost).
    for when in (at(WEDNESDAY, 16), at(FRIDAY, 9), at(FRIDAY, 12), at(FRIDAY, 16)):
        for job in JOBS:
            if job.name in SLACK_JOBS:
                assert not job.run(when).startswith(("1 ", "2 "))
    assert len(slack.posts) == 4 and len(rows(w.db)) == 4

    # Two days after: the second DM to the lead (a new message, not a repeat), still once.
    tick_hourly(loop, clock, at(FRIDAY + dt.timedelta(days=1), 10))
    assert len(slack.posts) == 5
    assert slack.posts[-1][0] == "U0KIM00001" and slack.posts[-1][1].startswith("Erinnerung: Für *Löten*")

    # Filled in: no more DMs.
    w.db.expire_all()
    t = w.db.get(type(t), t.id)
    t.participants_female, t.participants_male = 10, 12
    t.participants_diverse, t.participants_unspecified, t.participants_total = 0, 0, 22
    w.db.commit()
    tick_hourly(loop, clock, at(FRIDAY + dt.timedelta(days=8), 10))
    assert len(slack.to("U0KIM00001")) == 3
    # The following Friday brings the next weekly status, once.
    assert len(slack.to(CHANNEL)) == 2 and len(slack.posts) == 6


def test_the_after_course_rhythm_is_days_one_two_three_and_seven(team, slack):
    w = team
    t = confirmed(w, THURSDAY, w.kim)
    workflow.mark_done(w.db, t, "Test")
    w.db.commit()
    for days in range(0, 10):
        messages.send_after_course(at(THURSDAY + dt.timedelta(days=days), 9))
    texts = slack.to("U0KIM00001")
    assert len(texts) == 4
    assert texts[0].startswith("Hallo!") and texts[1].startswith("Erinnerung:")
    assert texts[3].startswith(f"Letzte Erinnerung: Für *Löten* am Do {THURSDAY:%d.%m.}")
    assert [r.key.rsplit(":", 1)[1] for r in rows(w.db)] == ["1", "2", "3", "7"]


def test_no_token_means_no_messages(team, monkeypatch, caplog):
    confirmed(team, THURSDAY, team.kim)
    team.db.commit()
    switched_on(monkeypatch)
    caplog.set_level(logging.INFO)
    for run in (messages.send_after_course, messages.send_reminders, messages.send_weekly_status):
        assert run(at(WEDNESDAY, 16)) == "messaging off: SLACK_BOT_TOKEN not set"
    assert "Slack messaging is off" in caplog.text
    assert rows(team.db) == []


def test_each_message_type_has_its_own_switch(team, slack, monkeypatch):
    confirmed(team, THURSDAY, team.kim)
    team.db.commit()
    switched_on(monkeypatch, reminder=False, weekly_status=False)
    assert messages.send_reminders(at(WEDNESDAY, 16)) == "switched off ([messages] reminder)"
    assert messages.send_weekly_status(at(FRIDAY, 12)) == "switched off ([messages] weekly_status)"
    switched_on(monkeypatch, channel=None)
    assert messages.send_weekly_status(at(FRIDAY, 12)) == "no team channel configured ([messages] channel)"
    switched_on(monkeypatch)
    assert messages.send_weekly_status(at(THURSDAY, 12)) == "not the day for the weekly status"
    assert slack.posts == []
    # The example instance ships with everything off.
    assert load_instance(EXAMPLE_INSTANCE).messages == Messages()


def test_people_without_slack_id_or_inactive_get_nothing(team, slack):
    w = team
    w.lea.slack_id = None
    w.sam.active = False
    confirmed(w, THURSDAY, w.kim, [w.lea, w.sam])
    w.db.commit()
    assert messages.send_reminders(at(WEDNESDAY, 16)) == "1 reminders sent, 0 already sent"
    assert [c for c, _ in slack.posts] == ["U0KIM00001"]


def test_a_moved_termin_gets_a_new_reminder(team, slack):
    w = team
    t = confirmed(w, THURSDAY, w.kim)
    w.db.commit()
    messages.send_reminders(at(WEDNESDAY, 16))
    t.date = FRIDAY
    w.db.commit()
    messages.send_reminders(at(THURSDAY, 16))
    messages.send_reminders(at(THURSDAY, 17))
    assert len(slack.posts) == 2


def test_a_slack_error_is_retried_but_an_unclear_send_never(team, slack):
    w = team
    confirmed(w, THURSDAY, w.kim, [w.lea])
    w.db.commit()
    job = next(j for j in scheduler.JOBS if j.name == "slack-reminders")

    slack.errors = [messages.SlackError("ratelimited"), TimeoutError("timed out")]
    assert scheduler.run_job(job, at(WEDNESDAY, 16)) is False  # recorded as failed, retried in an hour
    assert sorted((r.status, r.error) for r in rows(w.db)) == [
        ("failed", "ratelimited"),
        ("unclear", "TimeoutError"),
    ]
    assert slack.posts == []

    assert scheduler.run_job(job, at(WEDNESDAY, 17)) is True
    assert len(slack.posts) == 1  # only the failed one; the unclear one may have arrived
    assert sorted((r.status, r.attempts) for r in rows(w.db)) == [("sent", 2), ("unclear", 1)]


def test_a_message_left_sending_by_a_crash_is_not_sent_again(team, slack):
    w = team
    t = confirmed(w, THURSDAY, w.kim)
    w.db.commit()
    key = f"reminder:{t.id}:{THURSDAY.isoformat()}:{w.kim.id}"
    assert messages.claim(key, "reminder", "U0KIM00001", t.id, w.kim.id) is not None  # then the crash
    assert messages.send_reminders(at(WEDNESDAY, 16)) == "0 reminders sent, 1 already sent"
    assert slack.posts == []


def test_texts_escape_slack_markup_and_work_without_public_url(team, slack, monkeypatch):
    w = team
    t = confirmed(w, THURSDAY, w.kim)
    w.school.name = "Schule <Nord> & Süd"
    workflow.mark_done(w.db, t, "Test")
    w.db.commit()
    monkeypatch.delenv("PUBLIC_URL")
    from metrics.config import get_settings

    get_settings.cache_clear()
    messages.send_after_course(at(FRIDAY, 9))
    [text] = slack.to("U0KIM00001")
    assert "Schule &lt;Nord&gt; &amp; Süd" in text
    assert "Eintragen unter „Mein Bereich“" in text and "http" not in text


def test_logs_name_no_people(team, slack, caplog):
    confirmed(team, THURSDAY, team.kim, [team.lea])
    team.db.commit()
    caplog.set_level(logging.DEBUG, logger="metrics")
    scheduler.run_job(next(j for j in scheduler.JOBS if j.name == "slack-reminders"), at(WEDNESDAY, 16))
    assert "2 reminders sent" in caplog.text
    for secret in ("Kim", "Lea", "U0KIM00001", "U0LEA00001", "Löten"):
        assert secret not in caplog.text
