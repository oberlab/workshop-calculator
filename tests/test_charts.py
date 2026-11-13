from dataclasses import replace
from datetime import date

from metrics import analytics, charts
from metrics.instance import load_theme


def segments(bin_name):
    rows = [
        (date(2025, 3, 3), "Code", 2),
        (date(2025, 3, 4), "Science", 3),
        (date(2025, 3, 5), "Engineering", 1),
        (date(2025, 9, 1), "Code", 1),
    ]
    return analytics.aggregate(rows, bin_name, 8)


def test_stacking_and_colours(instance):
    spec = charts.sessions_over_time(segments("month"), "month", instance.theme, "März 2025 – September 2025")
    values = spec["data"]["values"]
    march = [v for v in values if v["label"] == "März 2025"]
    # Science at the bottom, Code on top (theme order Code, Engineering, Science).
    assert [(v["category"], v["y0"], v["y1"]) for v in march] == [
        ("Science", 0, 3),
        ("Engineering", 3, 4),
        ("Code", 4, 6),
    ]
    assert {v["total"] for v in march} == {6}
    color = spec["encoding"]["color"]["scale"]
    assert color["domain"] == ["Code", "Engineering", "Science"]
    assert color["range"] == ["#4477aa", "#ee6677", "#ccbb44"]
    assert spec["encoding"]["x"]["type"] == "temporal"
    assert spec["encoding"]["x"]["scale"]["domain"] == ["2025-03-01", "2025-10-01"]
    assert spec["encoding"]["y"]["title"] == "Anzahl der Einsätze pro Monat"


def test_school_year_is_ordinal(instance):
    spec = charts.sessions_over_time(segments("school_year"), "school_year", instance.theme, "")
    assert spec["encoding"]["x"]["type"] == "ordinal"
    assert spec["encoding"]["x"]["sort"] == ["2024/25", "2025/26"]
    assert "x2" not in spec["encoding"]
    assert spec["encoding"]["y"]["title"] == "Anzahl der Einsätze pro Schuljahr"


def test_unknown_categories_get_palette_colours(instance):
    segs = analytics.aggregate([(date(2025, 1, 1), "Kunst", 1), (date(2025, 1, 1), None, 1)], "month", 8)
    spec = charts.sessions_over_time(segs, "month", instance.theme, "")
    assert spec["encoding"]["color"]["scale"]["domain"] == ["Kunst", analytics.NO_CATEGORY]
    # The example palette starts with the three category colours, so new series continue after them.
    assert spec["encoding"]["color"]["scale"]["range"] == ["#228833", "#66ccee"]


def test_new_series_take_the_next_free_palette_colour(instance):
    theme = replace(
        instance.theme,
        category_colors={"Code": "#00A4C4", "Engineering": "#c7326c"},
        palette=["#00a4c4", "#c7326c", "#fabb3a", "#2e58b1"],
        palette_configured=True,
    )
    assert theme.colors_for(["Code", "Projekt A", "Engineering", "Projekt B", "Projekt C"]) == [
        "#00A4C4",
        "#fabb3a",
        "#c7326c",
        "#2e58b1",
        "#fabb3a",  # past the end of the palette: start again with the free colours
    ]


def test_without_a_configured_palette_colours_follow_the_position(instance):
    theme = replace(
        instance.theme,
        category_colors={"Code": "#00a4c4"},
        palette=["#111111", "#222222", "#333333"],
        palette_configured=False,
    )
    assert theme.colors_for(["Code", "Kunst", "Musik"]) == ["#00a4c4", "#222222", "#333333"]


def test_palette_flag_follows_the_tokens(tmp_path):
    (tmp_path / "theme").mkdir()
    tokens = tmp_path / "theme" / "tokens.toml"
    tokens.write_text('[color]\ntext = "#000000"\n')
    theme = load_theme(tmp_path)
    assert not theme.palette_configured
    assert theme.palette == ["#4477aa", "#ee6677", "#ccbb44", "#228833"]
    tokens.write_text('[palette]\ncategorical = ["#123456", "#abcdef"]\n')
    theme = load_theme(tmp_path)
    assert theme.palette_configured
    assert theme.palette == ["#123456", "#abcdef"]


def test_time_axis_labels():
    assert charts.time_axis("month", 7 * 365)["format"] == "%Y"
    assert charts.time_axis("quarter", 2 * 365)["format"] == "Q%q %Y"
    assert charts.time_axis("month", 2 * 365)["tickCount"] == {"interval": "month", "step": 3}
    assert charts.time_axis("week", 120)["tickCount"] == {"interval": "month", "step": 1}


def test_temporal_scale_is_utc(instance):
    spec = charts.sessions_over_time(segments("week"), "week", instance.theme, "")
    assert spec["encoding"]["x"]["scale"]["type"] == "utc"
    assert spec["encoding"]["x2"] == {"field": "end"}
