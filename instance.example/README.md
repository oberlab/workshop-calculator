# Instance directory (example)

Everything specific to one organisation lives in an instance directory that is mounted into the app container and never committed to the public repository. Copy this folder to `instance/` (gitignored) and adapt it. Without an instance directory the app runs with neutral defaults.

| Path | Content |
| --- | --- |
| `config.toml` | School year start, teacher-training contexts, Slack List column mapping and ignored columns |
| `theme/tokens.toml` | Colours, fonts, category colours and order |
| `theme/fonts/` | Font files referenced in `tokens.toml` |
| `seeds/locations.csv` | Every location with `school` = `ja`/`nein`; decides "Schulklasse" vs "Kinder & Jugendliche außerschulisch" |
| `mappings/kontext.csv` | Context aliases (`alias` → `canonical`) |
| `mappings/kontext_inferred.csv` | Context for empty values, inferred from the course name (`kursname` → `canonical`) |
| `mappings/kursname.csv` | Course name aliases (`alias` → `canonical`) |

Name mappings are applied to Slack data (API and CSV export). Locations missing from `locations.csv` are reported by the import and get the target group "Unbekannt" for children.
