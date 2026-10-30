from datetime import date

import pytest

from metrics import analytics as a

AUG = 8


@pytest.mark.parametrize(
    ("day", "bin_name", "start"),
    [
        (date(2025, 3, 13), "week", date(2025, 3, 10)),  # Thursday -> Monday
        (date(2025, 3, 10), "week", date(2025, 3, 10)),
        (date(2025, 3, 31), "month", date(2025, 3, 1)),
        (date(2025, 6, 30), "quarter", date(2025, 4, 1)),
        (date(2025, 10, 1), "quarter", date(2025, 10, 1)),
        (date(2025, 12, 31), "year", date(2025, 1, 1)),
        (date(2025, 7, 31), "school_year", date(2024, 8, 1)),
        (date(2025, 8, 1), "school_year", date(2025, 8, 1)),
    ],
)
def test_bin_start(day, bin_name, start):
    assert a.bin_start(day, bin_name, AUG) == start


def test_bin_end_and_labels():
    assert a.bin_end(date(2025, 12, 1), "month") == date(2026, 1, 1)
    assert a.bin_end(date(2025, 10, 1), "quarter") == date(2026, 1, 1)
    assert a.bin_end(date(2025, 8, 1), "school_year") == date(2026, 8, 1)
    assert a.bin_end(date(2025, 3, 10), "week") == date(2025, 3, 17)
    assert a.bin_label(date(2025, 3, 1), "month") == "März 2025"
    assert a.bin_label(date(2025, 4, 1), "quarter") == "Q2 2025"
    assert a.bin_label(date(2024, 8, 1), "school_year") == "2024/25"
    assert a.bin_label(date(2025, 1, 1), "year") == "2025"
    # ISO week: 29 Dec 2025 is in week 1 of 2026.
    assert a.bin_label(date(2025, 12, 29), "week") == "KW 1/2026"


@pytest.mark.parametrize(
    ("preset", "expected"),
    [
        ("all", (None, None)),
        ("this_school_year", (date(2026, 8, 1), date(2027, 7, 31))),
        ("last_school_year", (date(2025, 8, 1), date(2026, 7, 31))),
        ("this_year", (date(2026, 1, 1), date(2026, 12, 31))),
        ("last_year", (date(2025, 1, 1), date(2025, 12, 31))),
        ("last_12_months", (date(2025, 11, 1), date(2026, 10, 31))),
    ],
)
def test_resolve_period(preset, expected):
    assert a.resolve_period(preset, date(2026, 10, 30), AUG) == expected


def test_resolve_period_before_school_year_start():
    assert a.resolve_period("this_school_year", date(2026, 7, 31), AUG) == (
        date(2025, 8, 1),
        date(2026, 7, 31),
    )


def test_resolve_period_custom():
    assert a.resolve_period("custom", date(2026, 10, 30), AUG, date(2025, 1, 1), None) == (
        date(2025, 1, 1),
        None,
    )


def test_aggregate_sums_into_bins():
    rows = [
        (date(2025, 3, 3), "Code", 2),
        (date(2025, 3, 20), "Code", 1),
        (date(2025, 3, 20), "Science", 4),
        (date(2025, 4, 1), None, 1),
    ]
    segments = a.aggregate(rows, "month", AUG)
    assert [(s.label, s.category, s.count) for s in segments] == [
        ("März 2025", "Code", 3),
        ("März 2025", "Science", 4),
        ("April 2025", a.NO_CATEGORY, 1),
    ]
    assert segments[0].end == date(2025, 4, 1)


def test_period_caption():
    assert a.period_caption(date(2019, 12, 18), date(2026, 9, 3)) == "Dezember 2019 – September 2026"
    assert a.period_caption(date(2025, 3, 1), date(2025, 3, 30)) == "März 2025"
