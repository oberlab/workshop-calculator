# workshop-calculator

Course and workshop metrics for makerspaces. Sessions are recorded in a Slack List, synced into PostgreSQL and shown as an interactive chart: sessions over time, stacked by course category, with selectable time bins (week, month, quarter, calendar year, school year), period presets and filters for category, target group and context/project. Below the chart, key figures for the selection: sessions, participants and locations.

The user interface is in German.

## Quick start

Requirements: Docker with the Compose plugin.

```sh
cp .env.example .env          # set POSTGRES_PASSWORD; Slack values only for the sync
docker compose up -d --build  # starts PostgreSQL and the app, runs migrations
```

The app listens on <http://127.0.0.1:8000>. Without an instance directory it uses the public example in `instance.example/` (neutral theme, sample locations).

Load data:

```sh
# Legacy CSV files from LEGACY_DATA_DIR (mounted at /legacy); replaces all legacy rows.
docker compose exec app metrics load-legacy \
  --harmonised /legacy/harmonised.csv \
  --slack-export /legacy/slack_export.csv

# Read-only sync of the Slack List (needs SLACK_BOT_TOKEN with lists:read and SLACK_LIST_ID).
docker compose exec app metrics sync-slack
```

With the defaults, `/legacy` points to `tests/fixtures/`, so the commands above load the synthetic test data.

Quarterly hand-over from the Slack List into the legacy data (needs `lists:write`):

```sh
metrics archive-slack --until 2026-09-30 --out <dir>   # raw JSON backup + CSV of entries up to the date
metrics load-legacy … --slack-export <normalised CSV>  # archived entries replace their synced Slack rows
metrics delete-slack --csv <archive CSV>                # deletes exactly the archived items, logs each one
```

Optional internal view: with `[features] instructor_ranking = true` in the instance `config.toml`, the page shows sessions per instructor with Slack profile pictures (`metrics sync-instructors`, needs `users:read`; legacy first names are matched to Slack users in `seeds/instructors.csv`). Keep it disabled for anything public.

## Instance directory

Everything organisation-specific (theme, fonts, location list, name mappings, Slack column mapping) lives in an instance directory that is never committed. See [`instance.example/README.md`](instance.example/README.md). Point `INSTANCE_DIR` in `.env` to your own copy.

## Data rules

- Only sessions with status `Durchgeführt` and a date are counted.
- Only mapped Slack columns are transferred; unmapped columns are reported by the sync, columns listed as ignored (e.g. billing) are dropped silently.
- Slack items that disappear from the List stay in the database and are marked `removed_from_slack`.
- Target groups: teacher-training contexts and `Lehrkräfte` → Lehrkräfte; age from 19 or adult age bands → Erwachsene; children → Schulklasse at a school, außerschulisch elsewhere, Unbekannt if the location is empty or not in the location list.
- Key figures: *Einsätze* = held sessions; *Teilnehmende* = participants of sessions with session number 1 only, so children in multi-session courses count once (continuous formats use 1 on every date and count every time; entries without a session number are not counted); *Orte* = distinct location names.
- Name aliases (course names, contexts, locations) from the instance directory are applied on every import, including the Slack sync.
- Archived Slack entries loaded as legacy data replace the synced rows of the same Slack items, so nothing is counted twice.

## Development

```sh
uv sync
uv run pytest
uv run ruff check src tests migrations
```

Tests use only synthetic data. The vendored chart libraries (Vega, Vega-Lite, Vega-Embed) are listed in [`src/metrics/web/static/vendor/README.md`](src/metrics/web/static/vendor/README.md).
