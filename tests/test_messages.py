"""Slack reminders and status messages (WM-130). PostgreSQL, demo data and a fake Slack client only.

Done when: a test Termin produces each message once and none twice after a restart. The first test
runs the scheduler hour by hour through the week of a Termin (weekly status on Monday 09:00,
reminder the day before at 15:00, DM the day after at 09:00), restarts it and runs everything again.
"""

import dataclasses
import datetime as dt
import logging
import urllib.error
from datetime import time

import pytest
import sqlalchemy as sa

from metrics import messages, metrics, scheduler
from metrics.db import ABGESCHLOSSEN, SlackMessage
from metrics.instance import Messages, load_instance
from metrics.planning import workflow
from metrics.planning.workload import monday_of
from metrics.scheduler import LOCAL, Scheduler
from tests.conftest import EXAMPLE_INSTANCE
from tests.test_mine import _termin, world  # noqa: F401 (fixture)

BASE = "https://zahlen.example.org"
CHANNEL = "C0TEAM0001"
TODAY = dt.date.today()
# A Thursday at least a week ahead; its week's Monday brings the weekly status.
THURSDAY = TODAY + dt.timedelta(days=(3 - TODAY.weekday()) % 7 + 7)
WEDNESDAY, FRIDAY = THURSDAY - dt.timedelta(days=1), THURSDAY + dt.timedelta(days=1)
MONDAY = monday_of(THURSDAY)
NEXT_MONDAY = MONDAY + dt.timedelta(days=7)
SLACK_JOBS = ("slack-after-course", "slack-weekly-status", "slack-reminders")
JOBS = tuple(j for j in scheduler.JOBS if j.name in ("stage-moves", "hints", *SLACK_JOBS))


def at(day: dt.date, hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime.combine(day, time(hour, minute), tzinfo=LOCAL)


class FakeSlack:
    """Records what would be posted; `errors` makes the next posts fail like Slack would."""

    def __init__(self):
        self.posts: list[tuple[str, str]] = []
        self.errors: list[Exception] = []
        self.broken: dict[str, str] = {}  # channel -> the Slack error every post to it gets

    def post(self, channel: str, text: str) -> str:
        if self.errors:
            raise self.errors.pop(0)
        if channel in self.broken:
            raise messages.SlackError(self.broken[channel])
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
    planned = _termin(w, THURSDAY, w.sam, start_time="14:00", end_time="16:00")  # Geplant: no DM
    cancelled = confirmed(w, THURSDAY, w.sam)
    workflow.cancel(w.db, cancelled, "Schule hat abgesagt", "Test")
    nxt = _termin(w, NEXT_MONDAY + dt.timedelta(days=1), w.lea)  # next week, still Geplant
    w.db.commit()

    clock = Clock(at(MONDAY, 9))  # the first run of every job: nothing else is due yet
    tick_hourly(Scheduler(jobs=JOBS, clock=clock), clock, at(FRIDAY, 17))

    # Monday: the weekly status to the team channel, with this year's figures and this week's Termine.
    [weekly] = slack.to(CHANNEL)
    assert weekly.startswith(f"*Wochenstatus für {MONDAY:%d.%m.}–")
    assert f"*{MONDAY.year} bisher:* " in weekly and " Einsätze, " in weekly
    assert f"Do {THURSDAY:%d.%m.} 09:00 · *Löten* · Grundschule Musterstadt · Kim\n" in weekly + "\n"
    assert f"Do {THURSDAY:%d.%m.} 14:00 · *Löten* · Grundschule Musterstadt · Sam _(geplant)_" in weekly
    assert "Termine diese Woche (2)" in weekly  # not the cancelled one, not next week's
    assert "Hinweise" not in weekly and "Vorschläge" not in weekly
    assert f"<{BASE}/kalender|Kalender>" in weekly

    # The day before at 15:00: everyone on the team, nobody else.
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
    assert len(slack.posts) == 4

    sent = rows(w.db)
    assert [(r.kind, r.status, r.sent_at.astimezone(LOCAL).strftime("%a %H")) for r in sent] == [
        ("weekly_status", "sent", "Mon 09"),
        ("reminder", "sent", "Wed 15"),
        ("reminder", "sent", "Wed 15"),
        ("after_course", "sent", "Fri 09"),
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
    for when in (at(MONDAY, 9), at(WEDNESDAY, 15), at(FRIDAY, 9), at(FRIDAY, 15)):
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
    tick_hourly(loop, clock, at(NEXT_MONDAY + dt.timedelta(days=3), 10))
    assert len(slack.to("U0KIM00001")) == 3
    # The following Monday brings the next weekly status, once, with next week's Termin.
    assert len(slack.to(CHANNEL)) == 2 and len(slack.posts) == 6
    nxt_line = f"Di {nxt.date:%d.%m.} 09:00 · *Löten* · Grundschule Musterstadt · Lea _(geplant)_"
    assert nxt_line in slack.to(CHANNEL)[1]


def test_the_weekly_status_year_total_is_the_front_page_total(team, slack, monkeypatch):
    w = team
    held = [
        _termin(w, dt.date(TODAY.year, 1, 1), w.kim),  # this year: counted
        _termin(w, dt.date(TODAY.year, 1, 1), w.lea),
        _termin(w, dt.date(TODAY.year - 1, 12, 31), w.sam),  # last year: not counted
    ]
    for t, total in zip(held, (12, 1234, 30), strict=True):
        t.stage, t.participants_total = ABGESCHLOSSEN, total
    _termin(w, dt.date(TODAY.year, 1, 1), w.sam)  # Geplant: not counted
    w.db.commit()

    instance = messages.get_instance()
    expected = metrics.totals(
        w.db, metrics.Filters(preset="this_year"), TODAY, instance.school_year_start_month
    )
    assert expected.sessions == 2 and expected.participants == 1246
    figures = messages.weekly_figures(w.db, TODAY)
    assert figures.totals == expected and figures.year == TODAY.year

    messages.send_weekly_status(dt.datetime.combine(TODAY, time(9, 0), tzinfo=LOCAL))
    [weekly] = slack.to(CHANNEL)
    assert f"*{TODAY.year} bisher:* 2 Einsätze, 1.246 Teilnehmende" in weekly


def test_a_missed_monday_is_caught_up_once_later_that_week(team, slack):
    w = team
    _termin(w, MONDAY, w.kim)  # already past on Tuesday: not listed any more
    confirmed(w, THURSDAY, w.lea)
    w.db.commit()
    tuesday = MONDAY + dt.timedelta(days=1)
    assert messages.send_weekly_status(at(tuesday, 9)) == "1 weekly status sent, 0 already sent"
    [weekly] = slack.to(CHANNEL)
    assert weekly.startswith(f"*Wochenstatus für {MONDAY:%d.%m.}–")
    assert "Termine diese Woche (1)" in weekly and f"Do {THURSDAY:%d.%m.}" in weekly
    for day in range(2, 7):
        assert messages.send_weekly_status(at(MONDAY + dt.timedelta(days=day), 9)).startswith("0 weekly")
    assert messages.send_weekly_status(at(NEXT_MONDAY, 9)) == "1 weekly status sent, 0 already sent"
    assert [r.key for r in rows(w.db)] == [f"weekly_status:{MONDAY}", f"weekly_status:{NEXT_MONDAY}"]
    assert "keine Termine mehr" in slack.to(CHANNEL)[1]


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
        assert run(at(WEDNESDAY, 15)) == "messaging off: SLACK_BOT_TOKEN not set"
    assert "Slack messaging is off" in caplog.text
    assert rows(team.db) == []


def test_each_message_type_has_its_own_switch(team, slack, monkeypatch):
    confirmed(team, THURSDAY, team.kim)
    team.db.commit()
    switched_on(monkeypatch, reminder=False, weekly_status=False)
    assert messages.send_reminders(at(WEDNESDAY, 15)) == "switched off ([messages] reminder)"
    assert messages.send_weekly_status(at(MONDAY, 9)) == "switched off ([messages] weekly_status)"
    switched_on(monkeypatch, channel=None)
    assert messages.send_weekly_status(at(MONDAY, 9)) == "no team channel configured ([messages] channel)"
    assert slack.posts == []
    # The example instance ships with everything off.
    assert load_instance(EXAMPLE_INSTANCE).messages == Messages()


def test_people_without_slack_id_or_inactive_get_nothing(team, slack):
    w = team
    w.lea.slack_id = None
    w.sam.active = False
    confirmed(w, THURSDAY, w.kim, [w.lea, w.sam])
    w.db.commit()
    assert messages.send_reminders(at(WEDNESDAY, 15)) == "1 reminders sent, 0 already sent"
    assert [c for c, _ in slack.posts] == ["U0KIM00001"]


def test_a_moved_termin_gets_a_new_reminder(team, slack):
    w = team
    t = confirmed(w, THURSDAY, w.kim)
    w.db.commit()
    messages.send_reminders(at(WEDNESDAY, 15))
    t.date = FRIDAY
    w.db.commit()
    messages.send_reminders(at(THURSDAY, 15))
    messages.send_reminders(at(THURSDAY, 16))
    assert len(slack.posts) == 2


def test_a_slack_error_is_retried_but_an_unclear_send_never(team, slack):
    w = team
    confirmed(w, THURSDAY, w.kim, [w.lea])
    w.db.commit()
    job = next(j for j in scheduler.JOBS if j.name == "slack-reminders")

    slack.errors = [messages.SlackError("ratelimited"), TimeoutError("timed out")]
    assert scheduler.run_job(job, at(WEDNESDAY, 15)) is False  # recorded as failed, retried in an hour
    assert sorted((r.status, r.error) for r in rows(w.db)) == [
        ("failed", "ratelimited"),
        ("unclear", "TimeoutError"),
    ]
    assert slack.posts == []

    assert scheduler.run_job(job, at(WEDNESDAY, 16)) is True
    assert len(slack.posts) == 1  # only the failed one; the unclear one may have arrived
    assert sorted((r.status, r.attempts) for r in rows(w.db)) == [("sent", 2), ("unclear", 1)]


def test_a_message_left_sending_by_a_crash_is_not_sent_again(team, slack):
    w = team
    t = confirmed(w, THURSDAY, w.kim)
    w.db.commit()
    key = f"reminder:{t.id}:{THURSDAY.isoformat()}:{w.kim.id}"
    assert messages.claim(key, "reminder", "U0KIM00001", t.id, w.kim.id) is not None  # then the crash
    assert messages.send_reminders(at(WEDNESDAY, 15)) == "0 reminders sent, 1 already sent"
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
    scheduler.run_job(next(j for j in scheduler.JOBS if j.name == "slack-reminders"), at(WEDNESDAY, 15))
    assert "2 reminders sent" in caplog.text
    for secret in ("Kim", "Lea", "U0KIM00001", "U0LEA00001", "Löten"):
        assert secret not in caplog.text


def test_a_failing_reminder_run_never_sends_the_next_days_reminders_at_night(team, slack):
    w = team
    confirmed(w, THURSDAY, w.kim)  # Kim's reminder keeps failing on Wednesday
    confirmed(w, FRIDAY, w.lea)  # Lea's reminder is due on Thursday at 15:00
    w.db.commit()
    slack.broken["U0KIM00001"] = "ratelimited"
    job = next(j for j in scheduler.JOBS if j.name == "slack-reminders")
    clock = Clock(at(WEDNESDAY, 15))
    tick_hourly(Scheduler(jobs=(job,), clock=clock), clock, at(THURSDAY, 18))

    # Retried hourly within the window of Wednesday's slot (15:00 to 21:00), then given up.
    kim, lea = sorted(rows(w.db), key=lambda r: r.id)
    assert (kim.status, kim.attempts, kim.error) == ("failed", 6, "ratelimited")
    # Lea's reminder belongs to Thursday's slot: not sent at midnight, but at Thursday 15:00.
    assert lea.status == "sent" and lea.sent_at.astimezone(LOCAL) == at(THURSDAY, 15)
    assert slack.to("U0LEA00001") and len(slack.posts) == 1


def test_the_slack_jobs_send_only_for_their_own_slot_until_the_evening(team, slack):
    assert messages.occasion(at(WEDNESDAY, 15), scheduler.SLACK_AFTERNOON) == WEDNESDAY
    assert messages.occasion(at(WEDNESDAY, 20, 59), scheduler.SLACK_AFTERNOON) == WEDNESDAY
    assert messages.occasion(at(WEDNESDAY, 21), scheduler.SLACK_AFTERNOON) is None
    assert messages.occasion(at(THURSDAY, 0, 5), scheduler.SLACK_AFTERNOON) is None
    assert messages.occasion(at(THURSDAY, 14), scheduler.SLACK_AFTERNOON) is None
    assert messages.occasion(at(THURSDAY, 8), scheduler.SLACK_MORNING) is None

    t = confirmed(team, THURSDAY, team.kim)
    workflow.mark_done(team.db, t, "Test")
    team.db.commit()
    # The day after at 22:00 and the next night: too late for Friday's DM, too early for Saturday's.
    for when in (at(FRIDAY, 22), at(FRIDAY + dt.timedelta(days=1), 1)):
        assert messages.send_after_course(when).startswith("too late for the ")
        assert messages.send_weekly_status(when).startswith("too late for the ")
        assert messages.send_reminders(when).startswith("too late for the ")
    assert slack.posts == [] and rows(team.db) == []


def test_a_stale_slack_id_is_undeliverable_and_does_not_fail_the_job(team, slack, caplog):
    w = team
    confirmed(w, THURSDAY, w.kim, [w.lea])
    w.db.commit()
    slack.broken["U0KIM00001"] = "channel_not_found"
    job = next(j for j in scheduler.JOBS if j.name == "slack-reminders")
    caplog.set_level(logging.DEBUG, logger="metrics")

    assert scheduler.run_job(job, at(WEDNESDAY, 15)) is True
    assert "1 reminders sent, 0 already sent, 1 undeliverable (channel_not_found)" in caplog.text
    assert sorted((r.status, r.error) for r in rows(w.db)) == [
        ("sent", None),
        ("undeliverable", "channel_not_found"),
    ]
    # Final: not tried again, even once the Slack ID works again.
    del slack.broken["U0KIM00001"]
    assert messages.send_reminders(at(WEDNESDAY, 16)) == "0 reminders sent, 2 already sent"
    assert [c for c, _ in slack.posts] == ["U0LEA00001"]
    assert "U0KIM00001" not in caplog.text and "Kim" not in caplog.text


def test_http_429_and_5xx_are_retried_but_a_broken_connection_never(team, slack):
    w = team
    confirmed(w, THURSDAY, w.kim, [w.lea, w.sam])
    w.db.commit()
    job = next(j for j in scheduler.JOBS if j.name == "slack-reminders")

    def http(code: int) -> urllib.error.HTTPError:
        return urllib.error.HTTPError(messages.API_URL, code, "error", hdrs=None, fp=None)  # type: ignore[arg-type]

    slack.errors = [http(429), http(503), ConnectionResetError("reset")]
    assert scheduler.run_job(job, at(WEDNESDAY, 15)) is False
    assert sorted((r.status, r.error) for r in rows(w.db)) == [
        ("failed", "http_429"),
        ("failed", "http_503"),
        ("unclear", "ConnectionResetError"),
    ]
    assert scheduler.run_job(job, at(WEDNESDAY, 16)) is True
    assert len(slack.posts) == 2
    assert sorted(r.status for r in rows(w.db)) == ["sent", "sent", "unclear"]
