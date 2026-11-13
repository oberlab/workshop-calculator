# workshop-calculator

Course and workshop metrics for makerspaces. Sessions are recorded in PostgreSQL and shown as an interactive chart: sessions over time, stacked by course category, with selectable time bins (week, month, quarter, calendar year, school year) and period presets. Below the chart, key figures for the selection: sessions, participants and locations.

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
```

With the defaults, `/legacy` points to `tests/fixtures/`, so the commands above load the synthetic test data.

Legacy baseline: all reviewed data up to a cut-off date in one CSV file, loaded once and then frozen. Later legacy loads (quarterly hand-overs) may only add entries dated after it.

```sh
metrics export-baseline --harmonised … --slack-export … --out baseline.csv   # prints the sha256
docker compose exec app metrics load-baseline /legacy/baseline.csv --version 2026-09-30 --sha256 <sha256>
```

Quarterly hand-over from the Slack List into the legacy data (needs `lists:write`):

```sh
metrics archive-slack --until 2026-09-30 --out <dir>   # raw JSON backup + CSV of entries up to the date
metrics load-legacy … --slack-export <normalised CSV>  # loads the reviewed entries
metrics delete-slack --csv <archive CSV>                # deletes exactly the archived items, logs each one
```

Switch from Slack (WM-120): `metrics transfer-slack` takes every item of the Einsatztracking List and, if configured, of a project's own List (`SLACK_PROJECT_LIST_ID`, `[slack.project_list]` in the instance `config.toml`) dated after the baseline and creates a Termin for it: open items in their stage, held items as Durchgeführt, to be finally confirmed in the app. It first saves both Lists (raw JSON and a CSV of every column) in a new folder, and can run again from that folder without Slack. Slack is only read (`lists:read`); no item is changed or deleted. Names are stored as recorded; places, contexts, formats and people that are not list entries yet are added as proposed entries for an admin to confirm. Items already transferred are skipped, so a second run adds nothing.

```sh
docker compose run --rm -u "$(id -u)" -v "$PWD/slack_archive:/archive" app metrics transfer-slack --archive /archive --dry-run
docker compose run --rm -u "$(id -u)" -v "$PWD/slack_archive:/archive" app metrics transfer-slack --from-archive /archive/slack-listen_<time>
```

## Team login

The analysis page needs a login. `AUTH_MODE` in `.env` selects how:

| `AUTH_MODE` | Login |
| --- | --- |
| `password` (default) | One shared team password, for small instances only. It works only with `ALLOW_PASSWORD_LOGIN=true` and `TEAM_PASSWORD_HASH`; otherwise the page stays locked. |
| `oidc` | Single sign-on with an OpenID Connect provider such as Keycloak. |
| `off` | No login, for local tests only; the analysis page shows a warning. |

A login opens a server-side session in the database; the cookie holds only a random ID. A session lasts at most 12 hours and ends after 2 hours without activity. "Abmelden" ends it at once, and on the review page an admin can end all sessions of a person ("Überall abmelden"), for example after removing them from a Keycloak group: their next request goes to the login page, and the groups are read again at every login. Set `SESSION_SECRET` to a long random string (it signs the short-lived login state). After changing `.env`, run `docker compose up -d app`.

Every form carries a CSRF token and every POST must come from the app's own origin; the pages run under a strict Content-Security-Policy without inline scripts. Login attempts and the public data endpoint are rate-limited per client address.

Shared password (also set `ALLOW_PASSWORD_LOGIN=true`; leave it off wherever Keycloak is available):

```sh
docker compose run --rm app metrics hash-password   # prints TEAM_PASSWORD_HASH=…
```

OpenID Connect (Keycloak): create a confidential client (client authentication on, standard flow) with the valid redirect URI `<PUBLIC_URL>/login/callback`, for example `http://127.0.0.1:8000/login/callback` locally. Then set `OIDC_ISSUER` (`https://<host>/realms/<realm>`), `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` and `PUBLIC_URL`. To admit only part of the realm, set `OIDC_ALLOWED_GROUP` and add a "Group Membership" mapper (claim name `groups`, added to the ID token) to the client. If a shared client scope already uses the claim `groups` for something else, give the mapper its own claim name and set it in `OIDC_GROUPS_CLAIM`. Members of `OIDC_ADMIN_GROUP` (same claim) are admins: they see the admin pages (currently a placeholder review page) and may log in even outside `OIDC_ALLOWED_GROUP`. The shared team password never grants admin rights; `AUTH_MODE=off` treats everyone as admin. The app server must be able to reach the provider. "Abmelden" ends the app's login only; while the Keycloak session lasts, the next login goes through without asking for the password.

Optional internal view: with `[features] instructor_ranking = true` in the instance `config.toml`, the analysis page shows sessions per instructor with Slack profile pictures (`metrics sync-instructors`, needs `users:read`; legacy first names are matched to Slack users in `seeds/instructors.csv`). It is only reachable with the team login.

Project pages: each `[[projects]]` entry in the instance `config.toml` (`title`, optional `slug` and `contexts`) adds a page `/projekte/<slug>` to the sidebar for logged-in users. It shows the analysis limited to the project's contexts, with only the time controls (no category, target group or context filter). See `instance.example/config.toml`.

## Analysis figures

Besides the main chart, the analysis page shows sessions at schools per school type and a map of sessions per municipality of one district, with the sessions outside the district and those without a municipality beside it. Both use the places and school types in the database, which are added from two lookup tables in the instance directory when data is loaded: `seeds/locations.csv` (school type, address, coordinates and municipality per location) and `seeds/school_types.csv`. Check the lookup tables against the data with:

```sh
docker compose exec app metrics check-locations   # lists missing locations, contradictions, sessions without municipality
```

## Instance directory

Everything organisation-specific (theme, fonts, location list, name mappings, Slack column mapping) lives in an instance directory that is never committed. See [`instance.example/README.md`](instance.example/README.md). Point `INSTANCE_DIR` in `.env` to your own copy.

## Data model

One schema for planning, tracking and analysis (`src/metrics/db.py`, migrations in `migrations/`): list entries (places, contexts, formats, people, each `vorgeschlagen` or `bestätigt`), courses and their Termine (appointments) with stage, team, participants and home municipalities, plus history, proposals, hint decisions, job runs and import runs. Every imported row becomes a course with one Termin that keeps its source and original key; held rows are in stage `Abgeschlossen`. The reference data of the instance (`seeds/locations.csv`, `seeds/school_types.csv`, `[[projects]]`, `teacher_contexts`, `seeds/instructors.csv`) is added as confirmed list entries when the baseline or legacy data is loaded, and recorded names become confirmed entries exactly as recorded.

## Data rules

- Only Termine in stage `Abgeschlossen` with a date are counted.
- Target groups: teacher-training contexts and `Lehrkräfte` → Lehrkräfte; age from 19 or adult age bands → Erwachsene; children → Schulklasse at a school, außerschulisch elsewhere, Unbekannt if the location is empty or not in the location list.
- Key figures: *Einsätze* = held Termine; *Teilnehmende* = participants of Termine with Terminnummer 1 only, so children in multi-session courses count once (continuous formats use 1 on every date and count every time; entries without a number are not counted); *Orte* = distinct places.
- Names are stored exactly as recorded; only whitespace is tidied. Nothing is renamed or merged automatically; a legacy first name counts as the Slack user that `seeds/instructors.csv` names.

## Server deployment

Releases are Git tags on `main`; the server checks out a tag and builds the image itself with `ops/update.sh vX.Y.Z` (database dump first, then build, restart and a health check). First installation, releases and rollback: [ops/DEPLOY.md](ops/DEPLOY.md).

## Development

```sh
uv sync
uv run pytest
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
```

GitHub Actions runs the same commands on every push and pull request, on Python 3.13 (the Docker image) and 3.14.

Tests use only synthetic data and always the public `instance.example/`: a fixture in `tests/conftest.py` ignores a private `instance/` directory and any login or Slack settings from `.env`, and points the database URL at a closed port, so no test can read real data.

The database tests need a PostgreSQL server: start the throwaway one in `compose.test.yaml` (data in memory, a free host port chosen by Docker) or set `TEST_DATABASE_URL`. Each test run creates its own database there, runs the migrations and drops it at the end. Without a server these tests are skipped; in CI they must run.

```sh
docker compose -f compose.test.yaml up -d --wait
uv run pytest
docker compose -f compose.test.yaml down
```

`GET /healthz` is meant for monitoring: 200 with the time of the last finished import, 503 when the database does not answer (after at most 10 seconds).

The vendored chart libraries (Vega, Vega-Lite, Vega-Embed) are listed in [`src/metrics/web/static/vendor/README.md`](src/metrics/web/static/vendor/README.md).
