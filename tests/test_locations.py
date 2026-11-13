from dataclasses import replace

from metrics import analytics, charts
from metrics.analytics import UNKNOWN_SCHOOL_TYPE


def test_example_instance_loads_school_types_municipalities_and_map(instance):
    assert instance.school_type("Grundschule Musterstadt") == "GS"
    assert instance.school_type("Makerspace") is None
    assert instance.municipality("Makerspace") == "Beispieldorf"
    assert instance.municipality("Online") is None
    assert [m.name for m in instance.map.municipalities] == ["Beispieldorf", "Musterstadt", "Neudorf"]
    assert list(instance.school_types) == ["GS", "MS", "RS", "GYM"]


def _place(name, category, count):
    """A count row with the place details of the example seeds (unlisted places have none)."""
    from metrics.instance import load_instance
    from tests.conftest import EXAMPLE_INSTANCE

    example = load_instance(EXAMPLE_INSTANCE)
    return analytics.PlaceCount(
        name,
        example.location_is_school(name),
        example.school_type(name),
        example.municipality(name),
        category,
        count,
    )


ROWS = [
    _place("Grundschule Musterstadt", "Code", 3),
    _place("Grundschule Musterstadt", "Science", 2),
    _place("Mittelschule Musterstadt", "Code", 4),
    _place("Gymnasium Nachbarstadt", None, 1),
    _place("Makerspace", "Code", 5),
    _place("Online", "Code", 6),
    _place("Unbekannte Schule", "Code", 7),
    _place(None, "Code", 8),
]


def test_by_school_type_counts_only_schools_in_configured_order(instance):
    counts, order = analytics.by_school_type(ROWS, instance.school_types)
    assert order == ["Grundschule", "Mittelschule", "Gymnasium"]
    assert sorted(counts) == [
        ("Grundschule", "Code", 3),
        ("Grundschule", "Science", 2),
        ("Gymnasium", analytics.NO_CATEGORY, 1),
        ("Mittelschule", "Code", 4),
    ]


def test_school_without_type_is_shown_last(instance):
    new_school = analytics.PlaceCount("Neue Schule", True, None, None, "Code", 2)
    counts, order = analytics.by_school_type([*ROWS, new_school], instance.school_types)
    assert order[-1] == UNKNOWN_SCHOOL_TYPE
    assert (UNKNOWN_SCHOOL_TYPE, "Code", 2) in counts


def test_by_municipality_splits_inside_outside_and_unassigned(instance):
    counts = analytics.by_municipality(ROWS, instance)
    assert counts.inside == {"Beispieldorf": 5, "Musterstadt": 9, "Neudorf": 0}
    assert counts.outside == 1  # Gymnasium Nachbarstadt: municipality set, not on the map
    assert counts.unassigned == 6 + 7 + 8  # Online, a location not in the seeds, no location
    assert sum(counts.inside.values()) + counts.outside + counts.unassigned == sum(r.count for r in ROWS)


def test_school_type_chart_keeps_the_legend_selection_inside_the_bar_layer(instance):
    counts, order = analytics.by_school_type(ROWS, instance.school_types)
    spec = charts.sessions_by_school_type(counts, order, instance.theme)
    assert "params" not in spec
    bars, totals = spec["layer"]
    assert bars["params"][0]["name"] == "highlight"
    assert totals["data"]["values"] == [
        {"group": "Grundschule", "total": 5},
        {"group": "Mittelschule", "total": 4},
        {"group": "Gymnasium", "total": 1},
    ]


def test_map_spec_labels_every_municipality_and_greys_out_zero(instance):
    counts = analytics.by_municipality(ROWS, instance)
    spec = charts.municipality_map(instance.map, counts, instance.theme)
    shapes, labels = spec["layer"]
    assert {
        f["properties"]["name"]: f["properties"]["count"] for f in shapes["data"]["values"]
    } == counts.inside
    assert [v["label"] for v in labels["data"]["values"]] == [
        "Beispieldorf\n5",
        "Musterstadt\n9",
        "Neudorf\n0",
    ]
    assert shapes["encoding"]["color"]["condition"]["test"] == "datum.properties.count == 0"
    assert spec["title"]["text"] == instance.map.attribution


def test_map_labels_on_dark_fills_are_light(instance):
    theme = replace(instance.theme, map_gradient=["#d9f1f6", "#06657b"])
    counts = analytics.by_municipality(ROWS, instance)
    spec = charts.municipality_map(instance.map, counts, theme)
    inks = {v["label"]: v["ink"] for v in spec["layer"][1]["data"]["values"]}
    # Log scale over 1..9: Beispieldorf (5) is already about three quarters along, Musterstadt (9) at the
    # dark end; Neudorf (none) has the light grid colour.
    assert inks == {"Beispieldorf\n5": "#ffffff", "Musterstadt\n9": "#ffffff", "Neudorf\n0": "#333333"}
    light = replace(instance.theme, map_gradient=["#ffffff", "#d9f1f6"])
    spec = charts.municipality_map(instance.map, counts, light)
    assert {v["ink"] for v in spec["layer"][1]["data"]["values"]} == {"#333333"}
    assert spec["layer"][1]["mark"]["fill"] == {"expr": "datum.ink"}


def test_map_uses_short_labels(instance, monkeypatch):
    monkeypatch.setitem(instance.map.labels, "Musterstadt", "M-Stadt")
    counts = analytics.by_municipality(ROWS, instance)
    labels = charts.municipality_map(instance.map, counts, instance.theme)["layer"][1]["data"]["values"]
    assert "M-Stadt\n9" in [v["label"] for v in labels]


def test_example_lookup_tables(instance):
    assert instance.school_types == {
        "GS": "Grundschule",
        "MS": "Mittelschule",
        "RS": "Realschule",
        "GYM": "Gymnasium",
    }
    assert instance.location_coordinates["Makerspace"] == (48.08, 11.30)
    assert "Online" not in instance.location_coordinates


def test_check_names_flags_variants_and_never_changes_them(instance):
    from collections import Counter

    from metrics.names_check import check_names

    known = {"course_name": {"Löten", "Repair Café"}, "location": set(), "context": {"Lehrkräftefortbildung"}}
    new = {
        "course_name": Counter({"Loeten": 2, "Löten": 3, "Ganz neu": 1}),
        "location": Counter({"Makerspace Hauptstr.": 1, "Grundschule Musterstadt": 4}),
        "context": Counter({"LKF": 1}),
    }
    report = check_names(instance, known, new)
    courses = {d.value: (d.sessions, d.suggestion) for d in report.divergent["course_name"]}
    assert courses == {"Loeten": (2, "Löten"), "Ganz neu": (1, None)}  # alias from instance mappings
    assert [(d.value, d.suggestion) for d in report.divergent["location"]] == [
        ("Makerspace Hauptstr.", "Makerspace")
    ]  # lookup names count as known; the alias is only a suggestion
    assert [(d.value, d.suggestion) for d in report.divergent["context"]] == [
        ("LKF", "Lehrkräftefortbildung")
    ]
    assert report.problems == 4


def test_projects_from_config():
    import pytest

    from metrics.instance import Project, load_projects

    assert load_projects({"projects": [{"title": "Kurse"}]}) == [Project("kurse", "Kurse", ("Kurse",))]
    with pytest.raises(ValueError):
        load_projects({"projects": [{"title": "A", "slug": "Mit Leerzeichen"}]})
    with pytest.raises(ValueError):
        load_projects({"projects": [{"title": "A"}, {"title": "a"}]})
