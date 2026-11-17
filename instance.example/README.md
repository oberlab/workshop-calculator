# Instance directory (example)

Everything specific to one organisation lives in an instance directory that is mounted into the app container and never committed to the public repository. Copy this folder to `instance/` (gitignored) and adapt it. Without an instance directory the app runs with neutral defaults.

| Path | Content |
| --- | --- |
| `config.toml` | School year start, teacher-training contexts, Slack List column mapping and ignored columns; for `metrics transfer-slack` the status-to-stage mapping and a project's own List (`[slack.project_list]`) |
| `theme/tokens.toml` | Colours (text, background, accent and the hint colours `critical`, `warning`, `success`; the app derives surfaces and shades from them; optional dark inks `accent_ink`, `critical_ink`, `warning_ink`, `success_ink` for links and text on tints, else the colour mixed with black), fonts, category colours and order, `[palette] categorical` (chart series without a category colour take the next palette colour not used by a category), `[map] gradient` (map fill from few to many sessions, light to dark) |
| `theme/fonts/` | Font files referenced in `tokens.toml` |
| `seeds/locations.csv` | Lookup table, one row per location: `school` = `ja`/`nein` (decides "Schulklasse" vs "Kinder & Jugendliche außerschulisch"), `school_type` (a code from `school_types.csv`), `address`, `lat`/`lon`, `municipality` (a `name` from the map for places in the district, else the town; empty = no place, e.g. "Online"), `source`, `note`, `reviewed` |
| `seeds/school_types.csv` | School type codes and labels (`code`, `label`, `note`); row order = order in charts |
| `geo/municipalities.geojson` | Municipality boundaries for the map (`[map]` in `config.toml`); German districts: `python3 scripts/fetch_municipalities.py <district key> <file>` (BKG VG250, credit "© GeoBasis-DE / BKG") |
| `mappings/kontext.csv` | Context aliases (`alias` → `canonical`) |
| `mappings/kontext_inferred.csv` | Context for empty values, inferred from the course name (`kursname` → `canonical`) |
| `mappings/kursname.csv` | Course name aliases (`alias` → `canonical`) |
| `mappings/ort.csv` | Location aliases (`alias` → `canonical`) |
| `seeds/instructors.csv` | Legacy first names that belong to a Slack user (`alias` → `slack_id`), for the instructor ranking |

`config.toml` also holds `[[projects]]` (seeds the Kontexte list with project pages on the first load; afterwards the pages are managed under Stammdaten > Kontexte), `[map]` (title, GeoJSON file, labels and the label for places outside the map), `[workload]` (the default rule of the Team page's working-hours estimate: lead and support factors, preparation factor, fixed hours; every value can be overridden per course) and feature switches: `[features] instructor_ranking` (internal ranking with Slack profile pictures, off by default; keep it off for anything public) and `[messages]` (the bot's Slack messages: `after_course`, `reminder`, `weekly_status`, each off by default, and the team `channel` for the weekly status; nothing is sent without `SLACK_BOT_TOKEN`).

`[privacy]` holds the organisation's part of the privacy notice at `/datenschutz` (WM-153); everything the app does itself is described from the code, so only these values are needed: `responsible` (name and address, several lines), `contact_email` (for data protection requests), `hosting` (who runs the server and where, and how long the server's own logs and backups are kept), `legal_basis` (the legal basis wording per group of people, as checked by the organisation), `supervisory_authority` (name and address of the supervisory authority for complaints, several lines), optional `dpo` (the data protection officer) and `updated` (a date such as `2026-12-01`). Each missing required value shows a marked placeholder ("[… in der Instanz-Konfiguration eintragen]") and admins see which keys are missing; without `updated` the page is marked "Entwurf".

`[imprint]` holds the Impressum at `/impressum` ("Angaben gemäß § 5 DDG"): `name` (legal name and form), `address` (several lines), `represented_by` (several lines), `register` (register court and number), `email`, and optionally `phone`, `vat_id`, `responsible_content` (several lines) and `extra` (free text, paragraphs separated by an empty line, for anything else the organisation wants, such as disclaimers). Missing required values show placeholders and an admin notice as for `[privacy]`. Both pages are public and linked at the bottom of every page and on the login page.

The mapping tables are suggestions only: imports never rename anything. Locations missing from `locations.csv` are reported by the import and get the target group "Unbekannt" for children.
