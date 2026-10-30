"""Vega-Lite specs, styled with the instance theme."""

from collections import defaultdict

from metrics.analytics import BIN_PER, NO_CATEGORY, Segment
from metrics.instance import Theme

ORDINAL_BINS = ("year", "school_year")


def category_order(theme: Theme, categories: set[str]) -> list[str]:
    """Legend order: the theme's order, then other categories, 'Ohne Kategorie' last."""
    known = [c for c in theme.category_order if c in categories]
    others = sorted(c for c in categories if c not in theme.category_order and c != NO_CATEGORY)
    return known + others + ([NO_CATEGORY] if NO_CATEGORY in categories else [])


def theme_config(theme: Theme) -> dict:
    text = theme.colors.get("text", "#333333")
    border = theme.colors.get("border", text)
    return {
        "background": theme.colors.get("background", "#ffffff"),
        "font": theme.body_font,
        # Vega draws the view border below the grid, which hides it; axis lines are drawn on top.
        "view": {"stroke": None},
        "axis": {
            "labelFont": theme.body_font,
            "titleFont": theme.body_font,
            "labelColor": text,
            "titleColor": text,
            "labelFontSize": 12,
            "titleFontSize": 14,
            "titleFontWeight": "normal",
            "gridColor": theme.colors.get("grid", "#eeeeee"),
            "domain": True,
            "domainColor": border,
            "tickColor": border,
        },
        "legend": {
            "titleFont": theme.heading_font,
            "titleFontWeight": "bold",
            "titleColor": text,
            "titleFontSize": 13,
            "labelFont": theme.body_font,
            "labelColor": text,
            "labelFontSize": 13,
            "symbolType": "square",
            "symbolSize": 200,
        },
    }


def time_axis(bin_name: str, span_days: int) -> dict:
    """Ticks at bin starts, labelled to the right of the tick (the label names the period)."""
    if span_days > 3 * 366:
        ticks, label_format = {"interval": "year", "step": 1}, "%Y"
    elif bin_name == "quarter":
        ticks, label_format = {"interval": "month", "step": 3}, "Q%q %Y"
    elif span_days > 400:
        ticks, label_format = {"interval": "month", "step": 3}, "%b %Y"
    else:
        ticks, label_format = {"interval": "month", "step": 1}, "%b %Y"
    return {
        "grid": True,
        "tickCount": ticks,
        "format": label_format,
        "labelAlign": "left",
        "labelOffset": 3,
        "labelOverlap": True,
    }


def sessions_over_time(segments: list[Segment], bin_name: str, theme: Theme, x_title: str) -> dict:
    order = category_order(theme, {s.category for s in segments})
    colors = [theme.color_for(category, i) for i, category in enumerate(order)]
    stack_order = list(reversed(order))  # first legend entry on top, as in the old figure

    by_bin: dict[str, list[Segment]] = defaultdict(list)
    for segment in segments:
        by_bin[segment.start.isoformat()].append(segment)
    values = []
    for start in sorted(by_bin):
        parts = sorted(by_bin[start], key=lambda s: stack_order.index(s.category))
        total, y = sum(s.count for s in parts), 0
        for s in parts:
            values.append(
                {
                    "start": s.start.isoformat(),
                    "end": s.end.isoformat(),
                    "label": s.label,
                    "category": s.category,
                    "count": s.count,
                    "y0": y,
                    "y1": y + s.count,
                    "total": total,
                }
            )
            y += s.count

    if bin_name in ORDINAL_BINS:
        x = {
            "field": "label",
            "type": "ordinal",
            "title": x_title,
            "sort": sorted({v["label"] for v in values}),
            "axis": {"labelAngle": 0},
        }
        x2 = None
    else:
        first = min((s.start for s in segments), default=None)
        last = max((s.end for s in segments), default=None)
        # Dates are plain calendar dates: a UTC scale keeps bin edges and ticks exact in any time zone.
        scale = {"type": "utc"}
        if first and last:
            scale["domain"] = [first.isoformat(), last.isoformat()]
        x = {
            "field": "start",
            "type": "temporal",
            "title": x_title,
            "scale": scale,
            "axis": time_axis(bin_name, (last - first).days if first and last else 0),
        }
        x2 = {"field": "end"}

    encoding = {
        "x": x,
        "y": {
            "field": "y1",
            "type": "quantitative",
            "title": f"Anzahl der Einsätze pro {BIN_PER[bin_name]}",
            "axis": {"tickMinStep": 1, "format": "d"},
        },
        "y2": {"field": "y0"},
        "color": {
            "field": "category",
            "type": "nominal",
            "title": "Kurskategorie",
            "scale": {"domain": order, "range": colors},
            "legend": {"orient": "top", "direction": "horizontal"},
        },
        "opacity": {
            "condition": {"param": "highlight", "value": theme.bar_opacity, "empty": True},
            "value": 0.2,
        },
        "tooltip": [
            {"field": "label", "title": "Zeitraum"},
            {"field": "category", "title": "Kurskategorie"},
            {"field": "count", "title": "Einsätze"},
            {"field": "total", "title": "Einsätze im Zeitraum gesamt"},
        ],
    }
    if x2:
        encoding["x2"] = x2

    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 440,
        "autosize": {"type": "fit-x", "contains": "padding"},
        "data": {"values": values},
        "params": [
            {"name": "highlight", "select": {"type": "point", "fields": ["category"]}, "bind": "legend"}
        ],
        "mark": {"type": "bar"},
        "encoding": encoding,
        "config": theme_config(theme),
    }
