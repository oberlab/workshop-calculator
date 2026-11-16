"""Corrections of abgeschlossen Termine (WM-132): proposed by the team, confirmed by an admin.

Demo data only: the synthetic baseline (legacy Termine, all Abgeschlossen) and the example instance.
"""

import datetime as dt
from html.parser import HTMLParser

import pytest
import sqlalchemy as sa

from metrics import analytics, auth
from metrics.db import (
    ABGESCHLOSSEN,
    CONFIRMED,
    DURCHGEFUEHRT,
    PROPOSED,
    Appointment,
    HistoryEntry,
    Person,
    Place,
    Proposal,
)
from metrics.ingest import store
from metrics.planning import corrections
from metrics.planning.corrections import CorrectionError

TODAY = dt.date(2026, 11, 16)


def form(**values) -> dict[str, list[str]]:
    return {k: v if isinstance(v, list) else [str(v)] for k, v in values.items() if v is not None}


@pytest.fixture
def legacy(db, instance, baseline):
    """The demo baseline in the database, committed so the web app sees it."""
    store.load_baseline(db, baseline, instance, "demo", "0" * 64)
    db.commit()
    return db


def _counted_termin(db) -> Appointment:
    """A legacy Termin whose participants count (Terminnummer 1, with a recorded total)."""
    return db.scalars(
        sa.select(Appointment)
        .where(
            Appointment.stage == ABGESCHLOSSEN,
            Appointment.number == 1,
            Appointment.participants_total.is_not(None),
            Appointment.participants_female.is_not(None),
        )
        .order_by(Appointment.id)
    ).first()


class _FormValues(HTMLParser):
    """What a browser submits from the correction form, unchanged: inputs, checked boxes, selects."""

    def __init__(self):
        super().__init__()
        self.inside = False
        self.select: str | None = None
        self.textarea: str | None = None
        self.values: list[tuple[str, str]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "form":
            self.inside = (a.get("action") or "").endswith("/korrektur")
        if not self.inside:
            return
        if tag == "input" and a.get("name"):
            if a.get("type") not in ("checkbox", "radio") or "checked" in a:
                self.values.append((a["name"], a.get("value") or ""))
        elif tag == "select":
            self.select = a.get("name")
        elif tag == "option" and self.select and "selected" in a:
            self.values.append((self.select, a.get("value") or ""))
        elif tag == "textarea":
            self.textarea = a.get("name")
            self.values.append((self.textarea, ""))

    def handle_data(self, data):
        if self.textarea:
            name, text = self.values[-1]
            self.values[-1] = (name, text + data)

    def handle_endtag(self, tag):
        if tag == "form":
            self.inside = False
        elif tag == "select":
            self.select = None
        elif tag == "textarea":
            self.textarea = None


def _correction_form(html: str, **changes) -> dict[str, list[str]]:
    parser = _FormValues()
    parser.feed(html)
    values: dict[str, list[str]] = {}
    for name, value in parser.values:
        values.setdefault(name, []).append(value)
    return values | {k: [str(v)] for k, v in changes.items()}


def _history(db, t: Appointment) -> list[HistoryEntry]:
    return list(
        db.scalars(
            sa.select(HistoryEntry)
            .where(HistoryEntry.table_name == "appointments", HistoryEntry.row_id == t.id)
            .order_by(HistoryEntry.id)
        )
    )


def test_done_when_a_participant_correction_changes_nothing_until_confirmed(legacy, monkeypatch):
    """The ticket's check: proposed, the chart and key figures stay; confirmed, they change."""
    from tests.test_lists import _admin, _team

    db = legacy
    t = _counted_termin(db)
    female, total = t.participants_female, t.participants_total
    team = _team(monkeypatch)
    chart = "/api/analyse/sessions-over-time?bin=year&preset=all"
    before = team.get(chart).json()

    # The whole form as the course page shows it, with one count changed.
    page = team.get(f"/planung/{t.course_id}?termin={t.id}").text
    data = _correction_form(page, participants_female=female + 4, grund="Liste nachgereicht")
    assert {"date", "lead", "place", "participants_male", "team"} <= set(data)
    proposed = team.post(f"/planung/{t.course_id}/korrektur", data=data, follow_redirects=False)
    assert proposed.status_code == 303
    db.expire_all()
    assert (t.participants_female, t.participants_total) == (female, total)
    assert team.get(chart).json() == before  # nothing changed in the figures
    page = team.get(f"/planung/{t.course_id}?termin={t.id}").text
    assert "wartet auf einen Admin" in page and "Liste nachgereicht" in page

    # Only admins decide.
    proposal = db.scalars(sa.select(Proposal).where(Proposal.kind == "korrektur")).one()
    assert team.post("/pruefen/korrektur/bestaetigen", data={"vorschlag": proposal.id}).status_code == 403
    admin = _admin(monkeypatch)
    queue = admin.get("/pruefen").text
    assert "Liste nachgereicht" in queue and f"{female + 4}" in queue
    assert proposal.proposed_values.keys() == {"participants_female", "participants_total"}
    done = admin.post(
        "/pruefen/korrektur/bestaetigen", data={"vorschlag": proposal.id}, follow_redirects=False
    )
    assert done.status_code == 303

    db.expire_all()
    assert (t.participants_female, t.participants_total) == (female + 4, total + 4)
    after = admin.get(chart).json()
    assert after["meta"]["totals"]["participants"] == before["meta"]["totals"]["participants"] + 4
    assert after["meta"]["totals"]["sessions"] == before["meta"]["totals"]["sessions"]
    assert (proposal.decision, proposal.decided_by) == ("bestätigt", auth.OFF_USER)
    # The Verlauf names both people: the admin who applied it and the team member who proposed it.
    rows = {h.field: h for h in _history(db, t)}
    assert rows["participants_female"].changed_by == auth.OFF_USER
    assert "Korrektur von Team" in rows["participants_female"].reason
    assert rows["participants_total"].new_value == str(total + 4)
    assert any("Korrektur vorgeschlagen" in (h.reason or "") for h in _history(db, t))
    # Decided: the queue is empty again and a second decision is refused.
    assert "Keine Korrekturen offen" in admin.get("/pruefen").text
    again = admin.post("/pruefen/korrektur/ablehnen", data={"vorschlag": proposal.id, "grund": "x"})
    assert again.status_code == 422 and "schon entschieden" in again.text


def test_a_date_correction_moves_the_termin_in_the_chart(legacy):
    db = legacy
    t = _counted_termin(db)
    filters = analytics.Filters()

    def by_year():
        rows, _ = analytics.count_by_date_and_category(db, filters, TODAY, 9)
        return {(s.label, s.category): s.count for s in analytics.aggregate(rows, "year", 9)}

    before = by_year()
    proposal = corrections.propose(db, t, form(date="2019-03-04"), "Tippfehler im Jahr", "Kim")
    db.flush()
    assert by_year() == before
    corrections.confirm(db, proposal.id, "Admin")
    db.flush()
    assert t.date == dt.date(2019, 3, 4)
    assert by_year() != before and ("2019", t.course.format.category) in by_year()


def test_proposals_need_an_abgeschlossen_termin_a_reason_and_a_change(legacy):
    db = legacy
    t = _counted_termin(db)
    with pytest.raises(CorrectionError, match="Grund"):
        corrections.propose(db, t, form(participants_male=1), " ", "Kim")
    with pytest.raises(CorrectionError, match="ändert nichts"):
        corrections.propose(db, t, form(participants_female=t.participants_female), "nichts", "Kim")
    with pytest.raises(CorrectionError, match="keine ganze Zahl"):
        corrections.propose(db, t, form(participants_male="viele"), "Zahl", "Kim")
    with pytest.raises(CorrectionError, match="braucht ein Datum"):
        corrections.propose(db, t, form(date=""), "ohne Datum", "Kim")
    t.stage = DURCHGEFUEHRT
    with pytest.raises(CorrectionError, match="nur für abgeschlossene"):
        corrections.propose(db, t, form(participants_male=1), "Grund", "Kim")
    assert db.scalar(sa.select(sa.func.count()).select_from(Proposal)) == 0


def test_reject_needs_a_reason_and_changes_nothing(legacy):
    db = legacy
    t = _counted_termin(db)
    male = t.participants_male
    proposal = corrections.propose(db, t, form(participants_male=male + 1), "vergessen", "Kim")
    with pytest.raises(CorrectionError, match="Grund"):
        corrections.reject(db, proposal.id, "Admin", "")
    corrections.reject(db, proposal.id, "Admin", "Die Liste sagt etwas anderes")
    assert t.participants_male == male
    assert (proposal.decision, proposal.decision_reason) == ("abgelehnt", "Die Liste sagt etwas anderes")
    assert "Korrektur von Kim abgelehnt" in _history(db, t)[-1].reason
    assert corrections.open_for(db, [t.id]) == []


def test_confirm_refuses_stale_values_and_unconfirmed_entries(legacy):
    db = legacy
    t = _counted_termin(db)
    first = corrections.propose(db, t, form(participants_male=t.participants_male + 1), "a", "Kim")
    second = corrections.propose(db, t, form(participants_male=t.participants_male + 2), "b", "Lea")
    corrections.confirm(db, first.id, "Admin")
    with pytest.raises(CorrectionError, match="Seit dem Vorschlag geändert: TN männlich"):
        corrections.confirm(db, second.id, "Admin")
    assert second.decision is None

    # A place that is only proposed would bring an unconfirmed entry into the charts.
    place = Place(name="Turnhalle Nord", status=PROPOSED, is_school=False, address="")
    db.add(place)
    db.flush()
    proposal = corrections.propose(db, t, form(place=place.id), "anderer Ort", "Kim")
    with pytest.raises(CorrectionError, match="Ort „Turnhalle Nord“ ist nicht bestätigt"):
        corrections.confirm(db, proposal.id, "Admin")
    place.status = CONFIRMED
    corrections.confirm(db, proposal.id, "Admin")
    assert t.course.place is place


def test_course_fields_team_and_legacy_totals(legacy):
    db = legacy
    # A legacy Termin with only a total: the total itself is corrected.
    t = db.scalars(
        sa.select(Appointment)
        .where(Appointment.participants_female.is_(None), Appointment.participants_total.is_not(None))
        .order_by(Appointment.id)
    ).first()
    assert corrections.only_total(t)
    total = t.participants_total
    p = corrections.propose(db, t, form(participants_total=total + 2), "Summe falsch", "Kim")
    corrections.confirm(db, p.id, "Admin")
    assert (t.participants_total, t.participants_female) == (total + 2, None)

    kim = Person(name="Kim Korrektur", status=CONFIRMED)
    db.add(kim)
    db.flush()
    p = corrections.propose(
        db,
        t,
        form(
            lead=kim.id, team=1, support=[], class_age_text="Lehrkräfte", start_time="09:00", end_time="10:30"
        ),
        "Leitung und Zielgruppe",
        "Kim",
    )
    labels = [row[0] for row in corrections.changes(p)]
    assert "Kursleitung" in labels and "Klasse/Alter" in labels
    corrections.confirm(db, p.id, "Admin")
    assert t.lead is kim and t.support == [] and t.course.target_group == "Lehrkräfte"
    assert str(t.duration_hours) in ("1.50", "1.5")
    with pytest.raises(CorrectionError, match="Ende: muss nach dem Beginn"):
        corrections.propose(db, t, form(end_time="08:00"), "zu früh", "Kim")
