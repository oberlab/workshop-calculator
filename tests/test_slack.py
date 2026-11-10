from datetime import date

import pytest

from metrics.ingest import records as r
from metrics.ingest.slack import ListSnapshot, SlackError, map_items


def column(cid, name, kind, choices=None):
    col = {"id": cid, "name": name, "key": cid, "type": kind}
    if choices:
        col["options"] = {"choices": [{"value": v, "label": label} for v, label in choices.items()]}
    return col


SCHEMA = [
    column("C1", "Kursname", "text"),
    column("C2", "Datum", "date"),
    column("C3", "Dauer (h)", "number"),
    column("C4", "Ort", "select", {"o1": "Grundschule Musterstadt", "o2": "Makerspace"}),
    column(
        "C5", "Klassenstufe/\nDurchschnittsalter", "select", {"k1": "Kl. 4 / 9 Jahre", "k2": "Lehrkräfte"}
    ),
    column("C6", "TN :female_sign:", "number"),
    column("C7", "TN :male_sign:", "number"),
    column("C8", "Kurskategorie", "select", {"c1": "Code"}),
    column("C9", "Kontext", "select", {"x1": "LKF"}),
    column("C10", "Terminnummer", "number"),
    column("C11", "Status", "select", {"s1": "Durchgeführt", "s2": "In Planung"}),
    column("C12", "Kursleiter", "user"),
    column("C13", "Abgerechnet", "checkbox"),
    column("C14", "Notizen", "text"),
]


def item(item_id, **cells):
    fields = []
    for cid, (kind, value) in cells.items():
        f = {"column_id": cid, "key": cid, "value": value}
        f[kind] = value if kind == "text" else [value]
        fields.append(f)
    return {"id": item_id, "fields": fields}


def test_map_items(instance):
    snapshot = ListSnapshot(
        SCHEMA,
        [
            item(
                "R1",
                C1=("text", "Loeten"),
                C2=("date", "2026-02-03"),
                C3=("number", 1.5),
                C4=("select", "o1"),
                C5=("select", "k1"),
                C6=("number", 10),
                C7=("number", 12),
                C8=("select", "c1"),
                C10=("number", 1),
                C11=("select", "s1"),
                C12=("user", "U001"),
                C13=("checkbox", True),
            ),
            item(
                "R2",
                C1=("text", "Fortbildung"),
                C2=("date", "2026-02-04"),
                C4=("select", "o2"),
                C5=("select", "k2"),
                C9=("select", "x1"),
                C11=("select", "s2"),
            ),
            item("R3", C1=("text", "ohne Datum")),
        ],
    )
    mapped = map_items(snapshot, instance)
    assert mapped.skipped_without_date == 1
    assert mapped.unmapped_columns == ["Notizen"]  # Abgerechnet is ignored on purpose
    first, second = mapped.records
    assert first.source_key == "slack:R1"
    assert first.date == date(2026, 2, 3)
    assert first.course_name == "Loeten"  # stored as recorded: no alias is applied
    assert first.location == "Grundschule Musterstadt"
    assert (first.class_level, first.age) == (4, 9)
    assert first.participants_total == 22
    assert first.category == "Code"
    assert first.status == "Durchgeführt"
    assert first.instructor == "U001"
    assert first.target_group == r.SCHOOL_CLASS
    assert second.context == "LKF"  # as recorded; the variant is flagged by metrics check-names
    assert second.status == "In Planung"
    assert second.target_group == r.TEACHERS  # from the class/age field "Lehrkräfte", not the context


def test_missing_column_is_an_error(instance):
    with pytest.raises(SlackError, match="Kursleiter"):
        map_items(ListSnapshot([c for c in SCHEMA if c["name"] != "Kursleiter"], []), instance)
