"""Municipality boundaries for the map: loading, ring orientation and label points.

The GeoJSON comes from the instance directory (see scripts/fetch_municipalities.py). Each
feature needs a `name` property; that name is what `municipality` in seeds/locations.csv refers to.
"""

import json
from dataclasses import dataclass
from pathlib import Path

Ring = list[list[float]]


@dataclass(frozen=True)
class Municipality:
    name: str
    geometry: dict
    label_lon: float
    label_lat: float


def _signed_area(ring: Ring) -> float:
    """Shoelace area in degrees²: positive for counter-clockwise rings (north up)."""
    return sum(x0 * y1 - x1 * y0 for (x0, y0, *_), (x1, y1, *_) in zip(ring, ring[1:], strict=False)) / 2


def _polygons(geometry: dict) -> list[list[Ring]]:
    if geometry["type"] == "Polygon":
        return [geometry["coordinates"]]
    if geometry["type"] == "MultiPolygon":
        return geometry["coordinates"]
    raise ValueError(f"Unsupported geometry type: {geometry['type']}")


def rewind_for_drawing(geometry: dict) -> dict:
    """Outer rings clockwise, holes counter-clockwise.

    Vega draws with d3-geo, which reads a counter-clockwise outer ring (the GeoJSON standard
    orientation) as "the whole globe except this area" and fills everything around it.
    """
    polygons = []
    for polygon in _polygons(geometry):
        rings = []
        for index, ring in enumerate(polygon):
            clockwise = _signed_area(ring) < 0
            want_clockwise = index == 0
            rings.append(ring if clockwise == want_clockwise else list(reversed(ring)))
        polygons.append(rings)
    return {"type": "MultiPolygon", "coordinates": polygons}


def label_point(geometry: dict) -> tuple[float, float]:
    """Area centroid of the largest outer ring: where the name and the count are written."""
    outer = max((polygon[0] for polygon in _polygons(geometry)), key=lambda ring: abs(_signed_area(ring)))
    area = _signed_area(outer)
    if area == 0:
        xs, ys = zip(*((p[0], p[1]) for p in outer), strict=True)
        return sum(xs) / len(xs), sum(ys) / len(ys)
    cx = cy = 0.0
    for (x0, y0, *_), (x1, y1, *_) in zip(outer, outer[1:], strict=False):
        cross = x0 * y1 - x1 * y0
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    return cx / (6 * area), cy / (6 * area)


def load_municipalities(path: Path) -> list[Municipality]:
    collection = json.loads(path.read_text(encoding="utf-8"))
    municipalities = []
    for feature in collection["features"]:
        name = (feature.get("properties") or {}).get("name")
        if not name:
            raise ValueError(f"{path}: every feature needs a 'name' property")
        lon, lat = label_point(feature["geometry"])
        municipalities.append(Municipality(name, rewind_for_drawing(feature["geometry"]), lon, lat))
    return sorted(municipalities, key=lambda m: m.name)


def _in_ring(lon: float, lat: float, ring: Ring) -> bool:
    """Even-odd ray casting; points exactly on an edge may fall either way."""
    inside = False
    for (x0, y0, *_), (x1, y1, *_) in zip(ring, ring[1:], strict=False):
        if (y0 > lat) != (y1 > lat) and lon < x0 + (lat - y0) * (x1 - x0) / (y1 - y0):
            inside = not inside
    return inside


def contains(geometry: dict, lon: float, lat: float) -> bool:
    """True if the point lies in an outer ring of the geometry and in none of its holes."""
    for polygon in _polygons(geometry):
        if _in_ring(lon, lat, polygon[0]) and not any(_in_ring(lon, lat, hole) for hole in polygon[1:]):
            return True
    return False


def municipality_at(municipalities: list[Municipality], lon: float, lat: float) -> str | None:
    """Name of the municipality on the map that contains the point, None if outside the map."""
    return next((m.name for m in municipalities if contains(m.geometry, lon, lat)), None)
