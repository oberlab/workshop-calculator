"""One-time transfer of the Slack Lists into Termine (WM-120), with demo Lists only."""

import csv
import dataclasses
import sys
from datetime import date, timedelta

import pytest
import sqlalchemy as sa

from metrics import analytics, cli
from metrics.db import (
    ABGESAGT,
    BESTAETIGT,
    CONFIRMED,
    DURCHGEFUEHRT,
    GEPLANT,
    PROPOSED,
    Appointment,
    Context,
    Format,
    HistoryEntry,
    Person,
    Place,
    Proposal,
)
from metrics.ingest import store, transfer
from metrics.ingest.slack import ListSnapshot
from metrics.instance import ProjectList, load_instance
from tests.conftest import EXAMPLE_INSTANCE

TODAY = date(2026, 11, 20)
KIM = "U0000000001"  # a confirmed person in the example instance (seeds/instructors.csv)
NEW_PERSON = "U0000000002"


def column(cid, name, kind, choices=None):
    col = {"id": cid, "name": name, "key": cid, "type": kind}
    if choices:
        col["options"] = {"choices": [{"value": v, "label": label} for v, label in choices.items()]}
    return col


def item(item_id, **cells):
    """A List item as slackLists.items.list returns it; a cell is (type, value or list of values)."""
    fields = []
    for cid, (kind, value) in cells.items():
        values = value if isinstance(value, list) else [value]
        f = {"column_id": cid, "key": cid, "value": values[0]}
        f[kind] = value if kind == "text" else values
        fields.append(f)
    return {"id": item_id, "fields": fields, "updated_timestamp": "1763000000"}


MAIN_SCHEMA = [
    column("C1", "Kursname", "text"),
    column("C2", "Datum", "date"),
    column("C3", "Dauer (h)", "number"),
    column("C4", "Ort", "select", {"o1": "Grundschule Musterstadt", "o2": "Makerspace", "o3": "Neue Halle"}),
    column("C5", "Klassenstufe/\nDurchschnittsalter", "select", {"k1": "Kl. 4", "k2": "12 Jahre"}),
    column("C6", "TN :female_sign:", "number"),
    column("C7", "TN :male_sign:", "number"),
    column("C8", "Kurskategorie", "select", {"c1": "Code", "c2": "Engineering"}),
    column("C9", "Kontext", "select", {"x1": "Projekt A", "x2": "Neuer Kontext"}),
    column("C10", "Terminnummer", "number"),
    column(
        "C11",
        "Status",
        "select",
        {"s1": "In Planung", "s2": "Bestätigt", "s3": "Durchgeführt", "s4": "Abgesagt", "s5": "Vielleicht"},
    ),
    column("C12", "Kursleiter", "user"),
    column("C13", "Abgerechnet", "checkbox"),
]
STATUS = {"In Planung": "s1", "Bestätigt": "s2", "Durchgeführt": "s3", "Abgesagt": "s4", "Vielleicht": "s5"}


def main_item(item_id, day, status="Durchgeführt", place="o1", context="x1", users=KIM, name="Robotik"):
    cells = {
        "C1": ("text", name),
        "C3": ("number", 1.5),
        "C4": ("select", place),
        "C5": ("select", "k1"),
        "C6": ("number", 10),
        "C7": ("number", 12),
        "C8": ("select", "c1"),
        "C9": ("select", context),
        "C10": ("number", 1),
        "C12": ("user", users),
        "C13": ("checkbox", True),
    }
    if day:
        cells["C2"] = ("date", day.isoformat())
    if status:
        cells["C11"] = ("select", STATUS[status])
    return item(item_id, **cells)


PROJECT_SCHEMA = [
    column("P1", "Datum", "date"),
    column("P2", "Mädchen", "number"),
    column("P3", "Jungen", "number"),
    column("P4", "Musterstadt", "number"),
    column("P5", "Beispieldorf w", "number"),
    column("P6", "Beispieldorf m", "number"),
    column("P7", "Notiz", "text"),
]

PROJECT_LIST = ProjectList(
    label="Projekt-A-Liste",
    columns={"date": "Datum", "participants_female": "Mädchen", "participants_male": "Jungen"},
    defaults={
        "context": "Projekt A",
        "course_name": "Projekt-A-Treff",
        "location": "Makerspace",
        "category": "Engineering",
        "duration_hours": "4",
        "class_age": "12 Jahre",
        "session_number": "1",
        "instructor": KIM,
    },
    origins={"Musterstadt": ("Musterstadt",), "Beispieldorf": ("Beispieldorf w", "Beispieldorf m")},
    ignored_columns=frozenset({"Notiz"}),
)


def project_item(item_id, day, girls=None, boys=None, musterstadt=None, beispieldorf=(None, None)):
    cells = {"P1": ("date", day.isoformat()), "P7": ("text", "intern")}
    for cid, value in (
        ("P2", girls),
        ("P3", boys),
        ("P4", musterstadt),
        ("P5", beispieldorf[0]),
        ("P6", beispieldorf[1]),
    ):
        if value is not None:
            cells[cid] = ("number", value)
    return item(item_id, **cells)


@pytest.fixture
def cutoff(db, instance, baseline):
    store.load_baseline(db, baseline, instance, "demo", "0" * 64)
    db.commit()
    return store.baseline_end(db)


@pytest.fixture
def with_project(instance):
    return dataclasses.replace(instance, slack_project_list=PROJECT_LIST)


def lists(main_items, project_items=None):
    result = {transfer.MAIN: ("F0MAIN", ListSnapshot(MAIN_SCHEMA, main_items))}
    if project_items is not None:
        result[transfer.PROJECT] = ("F0PROJ", ListSnapshot(PROJECT_SCHEMA, project_items))
    return result


def slack_termine(db) -> dict[str, Appointment]:
    rows = db.scalars(sa.select(Appointment).where(Appointment.source == "slack"))
    return {a.source_data["item_id"]: a for a in rows}


def test_stages_and_cutoff(db, instance, cutoff):
    after = cutoff + timedelta(days=1)
    report = transfer.transfer(
        db,
        lists(
            [
                main_item("R1", after, "In Planung"),
                main_item("R2", after, "Bestätigt"),
                main_item("R3", after, "Durchgeführt"),
                main_item("R4", after, "Abgesagt"),
                main_item("R5", after, None),  # no status: open
                main_item("R6", after, "Vielleicht"),  # nobody mapped it
                main_item("R7", cutoff, "Durchgeführt"),  # in the baseline
                main_item("R8", cutoff, "In Planung"),  # before the cut-off, never held
                main_item("R9", None, "In Planung"),  # a planned course without a date yet
                main_item("R10", None, "Durchgeführt"),
            ]
        ),
        instance,
        TODAY,
    )
    stages = {key: a.stage for key, a in slack_termine(db).items()}
    assert stages == {
        "R1": GEPLANT,
        "R2": BESTAETIGT,
        "R3": DURCHGEFUEHRT,
        "R4": ABGESAGT,
        "R5": GEPLANT,
        "R9": GEPLANT,
    }
    skipped = {reason: [s.split()[1] for s in items] for reason, items in report.skipped.items()}
    assert skipped == {
        transfer.SKIP_UNKNOWN_STATUS: ["R6"],
        transfer.SKIP_BASELINE: ["R7"],
        transfer.SKIP_BEFORE: ["R8"],
        transfer.SKIP_HELD_NO_DATE: ["R10"],
    }
    assert report.cutoff == cutoff and report.read == {"Einsatztracking-Liste": 10}
    assert report.created[("Einsatztracking-Liste", GEPLANT)] == 3
    text = transfer.format_report(report)
    assert "Neue Termine: 6" in text and "'Vielleicht'" in text
    assert "Spalten ohne Zuordnung" not in text  # Abgerechnet is ignored on purpose


def test_stages_from_the_instance_config(db, instance, cutoff):
    custom = dataclasses.replace(instance, slack_stages={"Vielleicht": GEPLANT})
    transfer.transfer(db, lists([main_item("R1", cutoff + timedelta(days=3), "Vielleicht")]), custom, TODAY)
    assert slack_termine(db)["R1"].stage == GEPLANT
    with pytest.raises(transfer.TransferError, match="Abgeschlossen"):
        transfer.stage_map(dataclasses.replace(instance, slack_stages={"Durchgeführt": "Abgeschlossen"}))


def test_termin_keeps_the_recorded_values_and_history(db, instance, cutoff):
    day = cutoff + timedelta(days=5)
    transfer.transfer(db, lists([main_item("R1", day, users=[KIM, NEW_PERSON])]), instance, TODAY)
    db.commit()
    termin = slack_termine(db)["R1"]
    assert (termin.source, termin.source_key) == ("slack", "slack:R1")
    assert (termin.date, termin.duration_hours, termin.number) == (day, 1.5, 1)
    assert (termin.participants_female, termin.participants_male, termin.participants_total) == (10, 12, 22)
    assert termin.billed is False  # Abgerechnet is not transferred
    assert termin.course.format.name == "Robotik" and termin.course.format.category == "Code"
    assert termin.course.class_age_text == "Kl. 4" and termin.course.target_group == "Schulklasse"
    assert termin.source_data["recorded"]["location"] == "Grundschule Musterstadt"
    assert "Abgerechnet" not in str(termin.source_data)
    # People by Slack user ID: the first is the lead, the others support.
    assert termin.lead.slack_id == KIM and termin.lead.status == CONFIRMED
    assert [p.slack_id for p in termin.support] == [NEW_PERSON]

    history = db.scalars(
        sa.select(HistoryEntry).where(
            HistoryEntry.table_name == "appointments", HistoryEntry.row_id == termin.id
        )
    ).one()
    assert history.reason == "aus Slack übernommen (Einsatztracking-Liste, Eintrag R1)"
    assert (history.field, history.new_value, history.changed_by) == (
        "source_key",
        "slack:R1",
        "metrics transfer-slack",
    )


def test_unknown_names_become_proposed_entries(db, instance, cutoff):
    day = cutoff + timedelta(days=2)
    report = transfer.transfer(
        db,
        lists(
            [
                main_item("R1", day, place="o3", context="x2", users=[NEW_PERSON], name="Neuer Kurs"),
                main_item("R2", day, place="o3", users=[NEW_PERSON], name="Neuer Kurs"),
                main_item("R3", day, place="o2", users=[KIM]),
            ]
        ),
        instance,
        TODAY,
    )
    db.commit()
    place = db.scalars(sa.select(Place).where(Place.name == "Neue Halle")).one()
    assert place.status == PROPOSED and place.is_school is None
    assert db.scalars(sa.select(Place).where(Place.name == "Makerspace")).one().status == CONFIRMED
    assert db.scalars(sa.select(Context).where(Context.name == "Neuer Kontext")).one().status == PROPOSED
    person = db.scalars(sa.select(Person).where(Person.slack_id == NEW_PERSON)).one()
    assert (person.status, person.active) == (PROPOSED, True)
    formats = {(f.name, f.category): f.status for f in db.scalars(sa.select(Format))}
    assert formats[("Neuer Kurs", "Code")] == PROPOSED

    # One entry and one Vorschlag each, however many items use it.
    proposals = db.scalars(sa.select(Proposal).order_by(Proposal.id)).all()
    assert [(p.target_table, p.proposed_values.get("name")) for p in proposals] == [
        ("formats", "Neuer Kurs"),
        ("contexts", "Neuer Kontext"),
        ("places", "Neue Halle"),
        ("people", NEW_PERSON),
    ]  # R3 uses only entries the demo baseline already has
    assert all(p.kind == "eintrag" and p.decision is None for p in proposals)
    assert proposals[2].target_id == place.id
    assert proposals[2].reason == "aus Slack übernommen (Einsatztracking-Liste, Eintrag R1)"
    status_history = db.scalars(sa.select(HistoryEntry).where(HistoryEntry.field == "status")).all()
    assert {(h.table_name, h.row_id) for h in status_history} == {
        (p.target_table, p.target_id) for p in proposals
    }
    assert report.proposed["places"] == ["Neue Halle"] and report.proposed["people"] == [NEW_PERSON]
    assert "Orte (1): Neue Halle" in transfer.format_report(report)


def test_second_run_creates_nothing(db, instance, cutoff):
    items = lists([main_item("R1", cutoff + timedelta(days=1)), main_item("R2", cutoff + timedelta(days=9))])
    first = transfer.transfer(db, items, instance, TODAY)
    db.commit()
    counts = {
        t: db.scalar(sa.text(f"SELECT count(*) FROM {t}")) for t in ("appointments", "proposals", "history")
    }
    second = transfer.transfer(db, items, instance, TODAY)
    db.commit()
    assert first.total_created == 2 and second.total_created == 0
    assert len(second.skipped[transfer.SKIP_DONE]) == 2
    assert counts == {t: db.scalar(sa.text(f"SELECT count(*) FROM {t}")) for t in counts}
    assert "Nichts Neues" in transfer.format_report(second)


def test_project_list_with_origins(db, with_project, cutoff):
    week = [cutoff + timedelta(days=7 * n + 1) for n in range(4)]
    future = TODAY + timedelta(days=7)
    report = transfer.transfer(
        db,
        lists(
            [main_item("R1", week[0], context="x1")],  # the same session in the Einsatztracking List
            [
                project_item("J1", week[0], girls=2, boys=3, musterstadt=1, beispieldorf=(1, 3)),
                project_item("J2", week[1], girls=1, boys=1, musterstadt=1),  # one is missing
                project_item("J3", week[2]),  # past, nothing recorded
                project_item("J4", future),
                project_item("J5", cutoff, girls=1, boys=1),  # in the baseline
            ],
        ),
        with_project,
        TODAY,
    )
    db.commit()
    termine = slack_termine(db)
    j1 = termine["J1"]
    assert j1.stage == DURCHGEFUEHRT and j1.participants_total == 5
    assert {o.municipality: o.participants for o in j1.origins} == {"Musterstadt": 1, "Beispieldorf": 4}
    assert sum(o.participants for o in j1.origins) == j1.participants_total
    # Fields the List has no column for come from the instance config, and say so.
    assert (j1.course.context.name, j1.course.place.name, j1.course.format.name) == (
        "Projekt A",
        "Makerspace",
        "Projekt-A-Treff",
    )
    assert j1.duration_hours == 4 and j1.course.age == 12 and j1.lead.slack_id == KIM
    assert j1.source_data["defaults"]["context"] == "Projekt A"
    assert j1.source_data["recorded"]["Herkunft Beispieldorf"] == "4"
    assert termine["J3"].stage == GEPLANT and termine["J4"].stage == GEPLANT
    assert "J5" not in termine

    assert report.read == {"Einsatztracking-Liste": 1, "Projekt-A-Liste": 5}
    assert len(report.origin_mismatches) == 1 and "J2" in report.origin_mismatches[0]
    assert len(report.possible_duplicates) == 1
    assert "R1" in report.possible_duplicates[0] and "J1" in report.possible_duplicates[0]
    history = db.scalars(sa.select(HistoryEntry.reason).where(HistoryEntry.row_id == j1.id)).all()
    assert "aus Slack übernommen (Projekt-A-Liste, Eintrag J1)" in history


def test_a_missing_column_stops_the_transfer(db, with_project, cutoff):
    schema = [c for c in PROJECT_SCHEMA if c["name"] != "Beispieldorf m"]
    broken = lists([], [])
    broken[transfer.PROJECT] = ("F0PROJ", ListSnapshot(schema, []))
    with pytest.raises(transfer.TransferError, match="Beispieldorf m"):
        transfer.transfer(db, broken, with_project, TODAY)


def test_no_transfer_without_a_baseline(db, instance):
    with pytest.raises(transfer.TransferError, match="baseline"):
        transfer.transfer(db, lists([]), instance, TODAY)


def test_baseline_is_untouched(db, instance, cutoff):
    def snapshot():
        totals = analytics.totals(db, analytics.Filters(preset="all"), TODAY, 8)
        rows = db.execute(
            sa.text(
                "SELECT id, course_id, date, stage, participants_total, lead_id FROM appointments"
                " WHERE source = 'baseline' ORDER BY id"
            )
        ).all()
        return totals, rows

    before = snapshot()
    transfer.transfer(
        db,
        lists(
            [
                main_item("R1", cutoff + timedelta(days=1)),
                main_item("R2", cutoff + timedelta(days=2), place="o3"),
            ]
        ),
        instance,
        TODAY,
    )
    db.commit()
    assert snapshot() == before  # held Termine count only after their final confirmation


def test_archive_round_trip(tmp_path, with_project):
    fetched = lists(
        [main_item("R1", date(2026, 10, 1), users=[KIM, NEW_PERSON])], [project_item("J1", TODAY)]
    )
    folder = transfer.write_archive(tmp_path, fetched)
    assert sorted(p.name for p in folder.iterdir()) == [
        "einsatztracking.csv",
        "einsatztracking.json",
        "projekt.csv",
        "projekt.json",
    ]
    assert transfer.read_archive(folder, with_project) == fetched
    with (folder / "einsatztracking.csv").open(encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    # The CSV keeps every column, including those the transfer leaves out.
    assert rows[0]["Abgerechnet"] == "True"
    assert rows[0]["Kursleiter"] == f"{KIM}, {NEW_PERSON}"
    assert rows[0]["Ort"] == "Grundschule Musterstadt"
    assert (rows[0]["Slack-ID"], rows[0]["Slack-geändert"]) == ("R1", "1763000000")

    with pytest.raises(transfer.TransferError, match="projekt.json"):
        (folder / "projekt.json").unlink()
        transfer.read_archive(folder, with_project)


def run_cli(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["metrics", "transfer-slack", *args])
    cli.main()


def test_cli_archives_then_transfers_and_runs_again_from_the_archive(
    db, cutoff, tmp_path, monkeypatch, capsys
):
    """Live: Slack is only read (fetch_list is replaced here); offline: the same archive again."""
    fetched = []

    def fake_fetch(token, list_id):
        fetched.append(list_id)
        return ListSnapshot(MAIN_SCHEMA, [main_item("R1", cutoff + timedelta(days=1), "In Planung")])

    monkeypatch.setattr(transfer, "fetch_list", fake_fetch)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-demo")
    monkeypatch.setenv("SLACK_LIST_ID", "F0MAIN")

    run_cli(monkeypatch, "--archive", str(tmp_path), "--dry-run")
    assert "Probelauf" in capsys.readouterr().out
    assert not slack_termine(db)

    run_cli(monkeypatch, "--archive", str(tmp_path))
    out = capsys.readouterr().out
    assert "Neue Termine: 1" in out and "Geplant 1" in out
    assert fetched == ["F0MAIN", "F0MAIN"]
    db.expire_all()
    assert list(slack_termine(db)) == ["R1"]

    folder = sorted(tmp_path.iterdir())[-1]
    run_cli(monkeypatch, "--from-archive", str(folder))
    assert "Nichts Neues" in capsys.readouterr().out
    assert len(slack_termine(db)) == 1


def test_project_list_config(tmp_path):
    config = '[slack]\nignored_columns = ["Abgerechnet"]\n'
    (tmp_path / "config.toml").write_text(
        config
        + """
[slack.stages]
"Fixiert" = "Bestätigt"

[slack.project_list]
label = "Projekt-A-Liste"
ignored_columns = ["Notiz"]

[slack.project_list.columns]
date = "Datum"
participants_female = "Mädchen"

[slack.project_list.defaults]
context = "Projekt A"

[slack.project_list.origins]
"Musterstadt" = "Musterstadt"
"Beispieldorf" = ["Beispieldorf w", "Beispieldorf m"]
""",
        encoding="utf-8",
    )
    loaded = load_instance(tmp_path)
    assert loaded.slack_stages == {"Fixiert": "Bestätigt"}
    assert loaded.slack_project_list == ProjectList(
        label="Projekt-A-Liste",
        columns={"date": "Datum", "participants_female": "Mädchen"},
        defaults={"context": "Projekt A"},
        origins={"Musterstadt": ("Musterstadt",), "Beispieldorf": ("Beispieldorf w", "Beispieldorf m")},
        ignored_columns=frozenset({"Notiz"}),
    )
    assert load_instance(EXAMPLE_INSTANCE).slack_project_list is None

    (tmp_path / "config.toml").write_text(
        config + '\n[slack.project_list.columns]\ndate = "Datum"\n', encoding="utf-8"
    )
    with pytest.raises(ValueError, match="context"):
        load_instance(tmp_path)
