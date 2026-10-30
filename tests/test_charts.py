from datetime import date

from metrics import analytics, charts


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


def test_time_axis_labels():
    assert charts.time_axis("month", 7 * 365)["format"] == "%Y"
    assert charts.time_axis("quarter", 2 * 365)["format"] == "Q%q %Y"
    assert charts.time_axis("month", 2 * 365)["tickCount"] == {"interval": "month", "step": 3}
    assert charts.time_axis("week", 120)["tickCount"] == {"interval": "month", "step": 1}


def test_temporal_scale_is_utc(instance):
    spec = charts.sessions_over_time(segments("week"), "week", instance.theme, "")
    assert spec["encoding"]["x"]["scale"]["type"] == "utc"
    assert spec["encoding"]["x2"] == {"field": "end"}
