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

    y_axis = {"tickMinStep": 1, "format": "d"}
    highest = max((v["total"] for v in values), default=0)
    if 0 < highest < 10:
        # Vega ignores tickMinStep here and ticks small counts at halves, which "d" shows as
        # repeated integers (0, 1, 1, 2): name the integer ticks (WM-151).
        y_axis["values"] = list(range(highest + 1))
    encoding = {
        "x": x,
        "y": {
            "field": "y1",
            "type": "quantitative",
            "title": f"Anzahl der Einsätze pro {BIN_PER[bin_name]}",
            "axis": y_axis,
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


MAP_HEIGHT = 520  # the most a map takes; static/app.js makes it lower on narrow screens


def _coordinates(geometry: dict):
    """Every (lon, lat) of a Polygon or MultiPolygon."""
    polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
    for polygon in polygons:
        for ring in polygon:
            for lon, lat, *_ in ring:
                yield lon, lat


def mercator_aspect(geometries=(), points=()) -> float:
    """Width / height of the area in the Mercator projection (1 for a single point or nothing)."""
    coordinates = [c for g in geometries for c in _coordinates(g)] + list(points)
    if not coordinates:
        return 1.0
    lons = [math.radians(lon) for lon, _ in coordinates]
    ys = [math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) for _, lat in coordinates]
    width, height = max(lons) - min(lons), max(ys) - min(ys)
    if width <= 0 or height <= 0:
        return 1.0
    return round(min(max(width / height, 0.4), 3.0), 3)


def municipality_map(
    map_config: MapConfig, counts: MunicipalityCounts, theme: Theme, measure: str = "Einsätze"
) -> dict:
    """Choropleth of sessions per municipality, labelled with name and count.

    `measure` names what is counted in the legend and tooltip (participants for the Herkunft map).

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
        "height": MAP_HEIGHT,
        # static/app.js sets the height from the width with this ratio, so a narrow screen has no
        # empty space above and below the map.
        "usermeta": {"aspectRatio": mercator_aspect(m.geometry for m in map_config.municipalities)},
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
                        "title": measure,
                        "scale": {
                            "type": "log",
                            "domain": [1, top],
                            "range": theme.map_gradient,
                            # Blend the gradient colours like ggplot2 (CIELAB), as in the corinna map.
                            "interpolate": "lab",
                        },
                        # Above the map: below it the attribution covered the last label on phones.
                        "legend": {
                            "orient": "top",
                            "direction": "horizontal",
                            "gradientLength": 220,
                            "format": "d",
                        },
                    },
                    "tooltip": [
                        {"field": "properties.name", "title": "Gemeinde"},
                        {"field": "properties.count", "title": measure},
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


# ---- Standard views and planning insights (WM-151) ---------------------------------------------

SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"
BAR_STEP = 26  # height per bar of the horizontal bar charts


def sessions_by_group(
    rows: list[tuple[str, str, int]], order: list[str], theme: Theme, group_title: str
) -> dict:
    """Horizontal stacked bars of sessions per group (context, format, school, age) by category,
    the total at the end of each bar; groups in `order`, top to bottom."""
    categories = category_order(theme, {category for _, category, _ in rows})
    colors = theme.colors_for(categories)
    values = _stacked(rows, order, list(reversed(categories)))
    totals = [{"group": group, "total": sum(r[2] for r in rows if r[0] == group)} for group in order]
    y = {"field": "group", "type": "nominal", "title": None, "sort": order, "axis": {"labelLimit": 180}}
    return {
        "$schema": SCHEMA,
        "width": "container",
        "height": max(BAR_STEP * len(order), 80),
        "autosize": {"type": "fit-x", "contains": "padding"},
        "layer": [
            {
                "data": {"values": values},
                "params": [
                    {
                        "name": "highlight",
                        "select": {"type": "point", "fields": ["category"]},
                        "bind": "legend",
                    }
                ],
                "mark": {"type": "bar", "height": {"band": 0.75}},
                "encoding": {
                    "y": y,
                    "x": {
                        "field": "y1",
                        "type": "quantitative",
                        "title": "Anzahl der Einsätze",
                        "axis": {"tickMinStep": 1, "format": "d", "orient": "top"},
                    },
                    "x2": {"field": "y0"},
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
                        {"field": "group", "title": group_title},
                        {"field": "category", "title": "Kurskategorie"},
                        {"field": "count", "title": "Einsätze"},
                        {"field": "total", "title": "Einsätze gesamt"},
                    ],
                },
            },
            {
                "data": {"values": totals},
                "mark": {
                    "type": "text",
                    "align": "left",
                    "dx": 4,
                    "font": theme.heading_font,
                    "fontWeight": "bold",
                    "fontSize": 12,
                    "color": theme.colors.get("text", "#333333"),
                },
                "encoding": {
                    "y": y,
                    "x": {"field": "total", "type": "quantitative"},
                    "text": {"field": "total", "type": "quantitative", "format": "d"},
                },
            },
        ],
        "config": theme_config(theme),
    }


def cumulative_sessions(points: list[tuple], theme: Theme, x_title: str) -> dict:
    """Held Termine added up over time, one step per day with Termine; the last point is the total."""
    values = [{"date": day.isoformat(), "total": total} for day, total in points]
    scale = {"type": "utc"}
    if points:
        scale["domain"] = [points[0][0].isoformat(), points[-1][0].isoformat()]
    span = (points[-1][0] - points[0][0]).days if points else 0
    accent = theme.colors.get("accent", "#00a4c4")
    return {
        "$schema": SCHEMA,
        "width": "container",
        "height": 360,
        "autosize": {"type": "fit-x", "contains": "padding"},
        "data": {"values": values},
        "mark": {
            "type": "area",
            "interpolate": "step-after",
            "line": {"color": accent, "strokeWidth": 2},
            "color": accent,
            "opacity": 0.25,
        },
        "encoding": {
            "x": {
                "field": "date",
                "type": "temporal",
                "title": x_title,
                "scale": scale,
                "axis": time_axis("month", span),
            },
            "y": {
                "field": "total",
                "type": "quantitative",
                "title": "Einsätze insgesamt",
                "axis": {"tickMinStep": 1, "format": "d"},
            },
            "tooltip": [
                {"field": "date", "type": "temporal", "title": "Bis", "format": "%d.%m.%Y"},
                {"field": "total", "title": "Einsätze insgesamt"},
            ],
        },
        "config": theme_config(theme),
    }


def place_points(map_config: MapConfig | None, points: list, theme: Theme) -> dict:
    """One circle per place with coordinates, sized by its sessions, over the municipality outlines
    when the instance has a map."""
    located = [p for p in points if p.lat is not None and p.lon is not None]
    values = [
        {"name": p.place, "municipality": p.municipality or "", "count": p.count, "lat": p.lat, "lon": p.lon}
        for p in located
    ]
    layers = []
    if map_config:
        outlines = [
            {"type": "Feature", "geometry": m.geometry, "properties": {"name": m.name}}
            for m in map_config.municipalities
        ]
        layers.append(
            {
                "data": {"values": outlines},
                "mark": {
                    "type": "geoshape",
                    "fill": theme.colors.get("grid", "#eeeeee"),
                    "stroke": theme.colors.get("background", "#ffffff"),
                    "strokeWidth": 1.5,
                },
                "encoding": {"tooltip": [{"field": "properties.name", "title": "Gemeinde"}]},
            }
        )
    layers.append(
        {
            "data": {"values": values},
            "mark": {
                "type": "circle",
                "color": theme.colors.get("accent", "#00a4c4"),
                "opacity": 0.75,
                "stroke": theme.colors.get("background", "#ffffff"),
                "strokeWidth": 1,
            },
            "encoding": {
                "longitude": {"field": "lon", "type": "quantitative"},
                "latitude": {"field": "lat", "type": "quantitative"},
                "size": {
                    "field": "count",
                    "type": "quantitative",
                    "title": "Einsätze",
                    # Area grows with the count from zero, so circles compare like the numbers.
                    "scale": {"range": [0, 1500], "zero": True},
                    "legend": {
                        "orient": "top",
                        "direction": "horizontal",
                        "format": "d",
                        "symbolType": "circle",
                    },
                },
                "tooltip": [
                    {"field": "name", "title": "Ort"},
                    {"field": "municipality", "title": "Gemeinde"},
                    {"field": "count", "title": "Einsätze"},
                ],
            },
        }
    )
    geometries = [m.geometry for m in map_config.municipalities] if map_config else []
    spec = {
        "$schema": SCHEMA,
        "width": "container",
        "height": MAP_HEIGHT,
        "usermeta": {"aspectRatio": mercator_aspect(geometries, [(p.lon, p.lat) for p in located])},
        "autosize": {"type": "fit-x", "contains": "padding"},
        "projection": {"type": "mercator"},
        "layer": layers,
        "config": theme_config(theme),
    }
    if map_config:
        spec["title"] = {
            "text": map_config.attribution,
            "orient": "bottom",
            "anchor": "end",
            "fontSize": 10,
            "fontWeight": "normal",
            "color": theme.colors.get("muted", "#777777"),
        }
    return spec


def hours_over_time(bins: list[dict], bin_name: str, theme: Theme) -> dict:
    """The team's estimated working hours per time bin, lead and support stacked.

    `bins`: {"start", "end", "label", "lead", "support"} per bin, dates as ISO strings; the start and
    end let a click on a bar choose that period (static/app.js)."""
    parts = [("Leitung", "lead"), ("Unterstützung", "support")]
    values = []
    for b in bins:
        y = 0.0
        total = round(float(b["lead"]) + float(b["support"]), 1)
        for title, key in parts:
            hours = round(float(b[key]), 1)
            values.append(
                {
                    "start": b["start"],
                    "end": b["end"],
                    "label": b["label"],
                    "role": title,
                    "hours": hours,
                    "y0": y,
                    "y1": round(y + hours, 1),
                    "total": total,
                }
            )
            y = round(y + hours, 1)
    accent = theme.colors.get("accent", "#00a4c4")
    return {
        "$schema": SCHEMA,
        "width": "container",
        "height": 300,
        "autosize": {"type": "fit-x", "contains": "padding"},
        "data": {"values": values},
        "mark": {"type": "bar"},
        "encoding": {
            "x": {
                "field": "label",
                "type": "ordinal",
                "title": None,
                "sort": [b["label"] for b in bins],
                "axis": {"labelAngle": 0, "labelOverlap": True},
            },
            "y": {
                "field": "y1",
                "type": "quantitative",
                "title": f"Geschätzte Stunden pro {BIN_PER[bin_name]}",
            },
            "y2": {"field": "y0"},
            "color": {
                "field": "role",
                "type": "nominal",
                "title": None,
                "scale": {
                    "domain": [p[0] for p in parts],
                    "range": [accent, theme.colors.get("muted", "#999999")],
                },
                "legend": {"orient": "top", "direction": "horizontal"},
            },
            "tooltip": [
                {"field": "label", "title": "Zeitraum"},
                {"field": "role", "title": "Rolle"},
                {"field": "hours", "title": "Stunden", "format": ",.1f"},
                {"field": "total", "title": "Stunden gesamt", "format": ",.1f"},
            ],
        },
        "config": theme_config(theme),
    }
