# Instance directory (example)

Everything specific to one organisation lives in an instance directory that is mounted into the app container and never committed to the public repository. Copy this folder to `instance/` (gitignored) and adapt it. Without an instance directory the app runs with neutral defaults.

| Path | Content |
| --- | --- |
| `config.toml` | School year start, teacher-training contexts, Slack List column mapping and ignored columns |
| `theme/tokens.toml` | Colours, fonts, category colours and order |
| `theme/fonts/` | Font files referenced in `tokens.toml` |
| `seeds/locations.csv` | Lookup table, one row per location: `school` = `ja`/`nein` (decides "Schulklasse" vs "Kinder & Jugendliche außerschulisch"), `school_type` (a code from `school_types.csv`), `address`, `lat`/`lon`, `municipality` (a `name` from the map for places in the district, else the town; empty = no place, e.g. "Online"), `source`, `note`, `reviewed` |
| `seeds/school_types.csv` | School type codes and labels (`code`, `label`, `note`); row order = order in charts |
| `geo/municipalities.geojson` | Municipality boundaries for the map (`[map]` in `config.toml`); German districts: `python3 scripts/fetch_municipalities.py <district key> <file>` (BKG VG250, credit "© GeoBasis-DE / BKG") |
| `mappings/kontext.csv` | Context aliases (`alias` → `canonical`) |
| `mappings/kontext_inferred.csv` | Context for empty values, inferred from the course name (`kursname` → `canonical`) |
| `mappings/kursname.csv` | Course name aliases (`alias` → `canonical`) |
| `mappings/ort.csv` | Location aliases (`alias` → `canonical`) |
| `seeds/instructors.csv` | Legacy first names that belong to a Slack user (`alias` → `slack_id`), for the instructor ranking |

`config.toml` also holds feature switches: `[features] instructor_ranking` (internal ranking with Slack profile pictures, off by default; keep it off for anything public).

The mapping tables are suggestions only: imports never rename anything. Locations missing from `locations.csv` are reported by the import and get the target group "Unbekannt" for children.
