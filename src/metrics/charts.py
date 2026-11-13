"""Vega-Lite specs, styled with the instance theme."""

import math
from collections import defaultdict

from metrics.analytics import BIN_PER, NO_CATEGORY, MunicipalityCounts, Segment
from metrics.instance import MapConfig, Theme

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
    colors = theme.colors_for(order)
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


def _stacked(rows: list[tuple[str, str, int]], order: list[str], stack_order: list[str]) -> list[dict]:
    """Bar segments with explicit y0/y1 per group, first category on top (as in the main chart)."""
    values = []
    for group in order:
        parts = sorted((r for r in rows if r[0] == group), key=lambda r: stack_order.index(r[1]))
        total, y = sum(r[2] for r in parts), 0
        for _, category, count in parts:
            values.append(
                {
                    "group": group,
                    "category": category,
                    "count": count,
                    "y0": y,
                    "y1": y + count,
                    "total": total,
                }
            )
            y += count
    return values


def sessions_by_school_type(rows: list[tuple[str, str, int]], type_order: list[str], theme: Theme) -> dict:
    """Stacked bars of sessions at schools per school type, total above each bar."""
    categories = category_order(theme, {category for _, category, _ in rows})
    colors = theme.colors_for(categories)
    values = _stacked(rows, type_order, list(reversed(categories)))
    totals = [{"group": group, "total": sum(r[2] for r in rows if r[0] == group)} for group in type_order]
    x = {
        "field": "group",
        "type": "ordinal",
        "title": "Schulart",
        "sort": type_order,
        "axis": {"labelAngle": 0},
    }
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 360,
        "autosize": {"type": "fit-x", "contains": "padding"},
        "layer": [
            {
                "data": {"values": values},
                # Inside the layer: a top-level selection would be copied into every layer.
                "params": [
                    {
                        "name": "highlight",
                        "select": {"type": "point", "fields": ["category"]},
                        "bind": "legend",
                    }
                ],
                "mark": {"type": "bar"},
                "encoding": {
                    "x": x,
                    "y": {
                        "field": "y1",
                        "type": "quantitative",
                        "title": "Anzahl der Einsätze",
                        "axis": {"tickMinStep": 1, "format": "d"},
                    },
                    "y2": {"field": "y0"},
                    "color": {
                        "field": "category",
                        "type": "nominal",
                        "title": "Kurskategorie",
                        "scale": {"domain": categories, "range": colors},
                        "legend": {"orient": "top", "direction": "horizontal"},
                    },
                    "opacity": {
                        "condition": {"param": "highlight", "value": theme.bar_opacity, "empty": True},
                        "value": 0.2,
                    },
                    "tooltip": [
                        {"field": "group", "title": "Schulart"},
                        {"field": "category", "title": "Kurskategorie"},
                        {"field": "count", "title": "Einsätze"},
                        {"field": "total", "title": "Einsätze der Schulart gesamt"},
                    ],
                },
            },
            {
                "data": {"values": totals},
                "mark": {
                    "type": "text",
                    "baseline": "bottom",
                    "dy": -4,
                    "font": theme.heading_font,
                    "fontWeight": "bold",
                    "fontSize": 13,
                    "color": theme.colors.get("text", "#333333"),
                },
                "encoding": {
                    "x": x,
                    "y": {"field": "total", "type": "quantitative"},
                    "text": {"field": "total", "type": "quantitative", "format": "d"},
                },
            },
        ],
        "config": theme_config(theme),
    }


def _rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _luminance(color: str) -> float:
    """WCAG relative luminance of a hex colour."""
    channels = [c / 255 for c in _rgb(color)]
    r, g, b = (c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: str, b: str) -> float:
    high, low = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def _gradient_at(gradient: list[str], t: float) -> str:
    """Colour at position t (0..1) along evenly spaced gradient stops (linear in sRGB)."""
    if len(gradient) == 1:
        return gradient[0]
    position = min(max(t, 0.0), 1.0) * (len(gradient) - 1)
    i = min(int(position), len(gradient) - 2)
    a, b = _rgb(gradient[i]), _rgb(gradient[i + 1])
    f = position - i
    return "#" + "".join(f"{round(x + (y - x) * f):02x}" for x, y in zip(a, b, strict=True))


def _label_ink(fill: str, text: str, background: str) -> str:
    """Text or background colour, whichever reads better on the fill (dark fills get light labels)."""
    return text if _contrast(text, fill) >= _contrast(background, fill) else background


def municipality_map(map_config: MapConfig, counts: MunicipalityCounts, theme: Theme) -> dict:
    """Choropleth of sessions per municipality, labelled with name and count.

    Colour runs along the theme's map gradient on a log scale, so a few municipalities with
    many sessions do not push all others into the first colour. No sessions = grid colour.
    """
    text = theme.colors.get("text", "#333333")
    background = theme.colors.get("background", "#ffffff")
    highest = max(counts.inside.values(), default=0)
    top = max(highest, 2)

    def fill(count: int) -> str:
        """Approximately the fill Vega draws (log scale over the gradient), to pick the label ink."""
        if count <= 0:
            return theme.colors.get("grid", "#eeeeee")
        return _gradient_at(theme.map_gradient, math.log(count) / math.log(top))

    features = [
        {
            "type": "Feature",
            "geometry": m.geometry,
            "properties": {"name": m.name, "count": counts.inside.get(m.name, 0)},
        }
        for m in map_config.municipalities
    ]
    labels = [
        {
            "label": f"{map_config.label(m.name)}\n{counts.inside.get(m.name, 0)}",
            "lon": m.label_lon,
            "lat": m.label_lat,
            "ink": _label_ink(fill(counts.inside.get(m.name, 0)), text, background),
        }
        for m in map_config.municipalities
    ]
    return {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "width": "container",
        "height": 520,
        "autosize": {"type": "fit-x", "contains": "padding"},
        "projection": {"type": "mercator"},
        "title": {
            "text": map_config.attribution,
            "orient": "bottom",
            "anchor": "end",
            "fontSize": 10,
            "fontWeight": "normal",
            "color": theme.colors.get("muted", "#777777"),
        },
        "layer": [
            {
                "data": {"values": features},
                "mark": {
                    "type": "geoshape",
                    "stroke": theme.colors.get("background", "#ffffff"),
                    "strokeWidth": 1.5,
                },
                "encoding": {
                    "color": {
                        "condition": {
                            "test": "datum.properties.count == 0",
                            "value": theme.colors.get("grid", "#eeeeee"),
                        },
                        "field": "properties.count",
                        "type": "quantitative",
                        "title": "Einsätze",
                        "scale": {
                            "type": "log",
                            "domain": [1, top],
                            "range": theme.map_gradient,
                            # Blend the gradient colours like ggplot2 (CIELAB), as in the corinna map.
                            "interpolate": "lab",
                        },
                        "legend": {
                            "orient": "bottom",
                            "direction": "horizontal",
                            "gradientLength": 260,
                            "format": "d",
                        },
                    },
                    "tooltip": [
                        {"field": "properties.name", "title": "Gemeinde"},
                        {"field": "properties.count", "title": "Einsätze"},
                    ],
                },
            },
            {
                "data": {"values": labels},
                "mark": {
                    "type": "text",
                    "lineBreak": "\n",
                    "font": theme.heading_font,
                    "fontWeight": "bold",
                    "fontSize": 11,
                    # Per label (not an encoding, which would share the fill's colour scale).
                    "fill": {"expr": "datum.ink"},
                },
                "encoding": {
                    "longitude": {"field": "lon", "type": "quantitative"},
                    "latitude": {"field": "lat", "type": "quantitative"},
                    "text": {"field": "label"},
                },
            },
        ],
        "config": theme_config(theme),
    }
