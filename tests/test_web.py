from datetime import date

import pytest
from fastapi import HTTPException
from starlette.datastructures import QueryParams

from metrics.web import app as web


def test_parse_filters_defaults():
    filters = web.parse_filters(QueryParams(""))
    assert (filters.bin, filters.preset, filters.categories) == ("month", "all", [])


def test_parse_filters_values():
    filters = web.parse_filters(
        QueryParams(
            "bin=quarter&period=custom&from=2024-01-01&to=2024-12-31&category=Code&category=Science&context=A"
        )
    )
    assert filters.bin == "quarter"
    assert (filters.date_from, filters.date_to) == (date(2024, 1, 1), date(2024, 12, 31))
    assert filters.categories == ["Code", "Science"]
    assert filters.contexts == ["A"]


def test_dates_only_count_for_custom_period():
    filters = web.parse_filters(QueryParams("period=all&from=2024-01-01"))
    assert filters.date_from is None


@pytest.mark.parametrize("query", ["bin=day", "period=forever", "period=custom&from=gestern"])
def test_parse_filters_rejects_invalid(query):
    with pytest.raises(HTTPException) as error:
        web.parse_filters(QueryParams(query))
    assert error.value.status_code == 400


def test_theme_css(instance):
    css = web._theme_css(instance.theme)
    assert "--color-text: #333333;" in css
    assert "--font-body: system-ui, sans-serif;" in css


def test_healthz():
    assert web.healthz() == {"status": "ok"}
