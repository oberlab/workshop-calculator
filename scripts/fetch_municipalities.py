"""Fetch the municipality boundaries of one German district as GeoJSON (standard library only).

Source: BKG VG250 (Verwaltungsgebiete 1:250 000) through the public WFS of the Federal Agency
for Cartography and Geodesy. Open data under the Datenlizenz Deutschland – Namensnennung 2.0
(dl-de/by-2-0); credit "© GeoBasis-DE / BKG (<year>)" wherever the map is shown.

    python3 scripts/fetch_municipalities.py 09182 instance/geo/municipalities.geojson

The first argument is the district key (Kreisschlüssel, the first five digits of the
municipality key AGS; 09182 = Landkreis Miesbach). Each feature keeps only `ags` and `name`
(the BKG short name, e.g. "Gmund a.Tegernsee"); `municipality` in `seeds/locations.csv` must use
exactly these names. The app fixes the ring orientation for drawing, so the file is written as
the service returns it.
"""

import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

WFS_URL = "https://sgx.geodatenzentrum.de/wfs_vg250"
TIMEOUT = 60


def fetch(district_key: str) -> dict:
    fes_filter = (
        '<fes:Filter xmlns:fes="http://www.opengis.net/fes/2.0">'
        '<fes:PropertyIsLike wildCard="*" singleChar="." escapeChar="!">'
        f"<fes:ValueReference>ags</fes:ValueReference><fes:Literal>{district_key}*</fes:Literal>"
        "</fes:PropertyIsLike></fes:Filter>"
    )
    query = urllib.parse.urlencode(
        {
            "SERVICE": "WFS",
            "REQUEST": "GetFeature",
            "VERSION": "2.0.0",
            "TYPENAMES": "vg250:vg250_gem",
            "OUTPUTFORMAT": "application/json",
            "SRSNAME": "EPSG:4326",
            "FILTER": fes_filter,
        }
    )
    with urllib.request.urlopen(f"{WFS_URL}?{query}", timeout=TIMEOUT) as response:
        return json.load(response)


def slim(collection: dict) -> dict:
    features = [
        {
            "type": "Feature",
            "properties": {"ags": f["properties"]["ags"], "name": f["properties"]["gen"]},
            "geometry": f["geometry"],
        }
        for f in sorted(collection["features"], key=lambda f: f["properties"]["ags"])
    ]
    return {"type": "FeatureCollection", "features": features}


def main() -> None:
    if len(sys.argv) != 3 or not (sys.argv[1].isdigit() and len(sys.argv[1]) == 5):
        sys.exit("Usage: fetch_municipalities.py <5-digit district key> <output.geojson>")
    district_key, out = sys.argv[1], Path(sys.argv[2])
    collection = slim(fetch(district_key))
    if not collection["features"]:
        sys.exit(f"No municipalities found for district key {district_key}.")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(collection, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    names = ", ".join(f["properties"]["name"] for f in collection["features"])
    print(f"{len(collection['features'])} municipalities written to {out}: {names}")


if __name__ == "__main__":
    main()
