# workshop-calculator

Course and workshop metrics for makerspaces. Sessions are recorded in a Slack List, synced into PostgreSQL and shown as an interactive chart: sessions over time, stacked by course category, with selectable time bins (week, month, quarter, calendar year, school year) and period presets. Below the chart, key figures for the selection: sessions, participants and locations.

Two pages:

- `/` (overview): chart and key figures; only time bin and period can be chosen.
- `/analyse` (team login): the same chart with filters for category, target group and context/project, plus internal views such as the instructor ranking.

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

## Team login

The analysis page needs a login. `AUTH_MODE` in `.env` selects how:

| `AUTH_MODE` | Login |
| --- | --- |
| `password` (default) | One shared team password. Without `TEAM_PASSWORD_HASH` the page stays locked. |
| `oidc` | Single sign-on with an OpenID Connect provider such as Keycloak. |
| `off` | No login, for local tests only; the analysis page shows a warning. |

After logging in, the app keeps its own login cookie for 14 days. Set `SESSION_SECRET` to a long random string, otherwise every restart logs everyone out. After changing `.env`, run `docker compose up -d app`.

Shared password:

```sh
docker compose run --rm app metrics hash-password   # prints TEAM_PASSWORD_HASH=…
```

OpenID Connect (Keycloak): create a confidential client (client authentication on, standard flow) with the valid redirect URI `<PUBLIC_URL>/login/callback`, for example `http://127.0.0.1:8000/login/callback` locally. Then set `OIDC_ISSUER` (`https://<host>/realms/<realm>`), `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` and `PUBLIC_URL`. To admit only part of the realm, set `OIDC_ALLOWED_GROUP` and add a "Group Membership" mapper (claim name `groups`, added to the ID token) to the client. If a shared client scope already uses the claim `groups` for something else, give the mapper its own claim name and set it in `OIDC_GROUPS_CLAIM`. The app server must be able to reach the provider. "Abmelden" ends the app's login only; while the Keycloak session lasts, the next login goes through without asking for the password.

Optional internal view: with `[features] instructor_ranking = true` in the instance `config.toml`, the analysis page shows sessions per instructor with Slack profile pictures (`metrics sync-instructors`, needs `users:read`; legacy first names are matched to Slack users in `seeds/instructors.csv`). It is only reachable with the team login.

## Analysis figures

Besides the main chart, the analysis page shows sessions at schools per school type and a map of sessions per municipality of one district, with the sessions outside the district and those without a municipality beside it. Both come from two lookup tables in the instance directory, `seeds/locations.csv` (school type, address, coordinates and municipality per location) and `seeds/school_types.csv`. Check them against the data with:

```sh
docker compose exec app metrics check-locations   # lists missing locations, contradictions, sessions without municipality
```

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
uv run ruff format --check src tests migrations
```

GitHub Actions runs the same commands on every push and pull request, on Python 3.13 (the Docker image) and 3.14.

Tests use only synthetic data and always the public `instance.example/`: a fixture in `tests/conftest.py` ignores a private `instance/` directory and any login or Slack settings from `.env`, and points the database URL at a closed port, so no test can read real data.

`GET /healthz` is meant for monitoring: 200 with the time of the last finished import, 503 when the database does not answer (after at most 10 seconds).

The vendored chart libraries (Vega, Vega-Lite, Vega-Embed) are listed in [`src/metrics/web/static/vendor/README.md`](src/metrics/web/static/vendor/README.md).
