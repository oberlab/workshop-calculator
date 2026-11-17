# workshop-calculator

A lean course planning, tracking and analysis platform for a makerspace. The team plans courses and series of Termine (appointments), records the participants after each course, sees the team's workload and analyses the results; a public front page shows the key figures. FastAPI, Jinja, htmx, Vega-Lite and PostgreSQL, run with Docker Compose. The user interface is in German; a short guide for the team is in [`docs/anleitung.md`](docs/anleitung.md), one for admins in [`docs/anleitung-admin.md`](docs/anleitung-admin.md).

## What it does

**Planning** (sidebar group "Planung"):

- **Kurse** (`/planung`) lists the Termine by week, with tabs per stage plus "Alle offenen" and "Alle" and filters. A course has shared fields (Format, Kontext, Ort, Klasse/Alter, Notiz) and one or more Termine with date, times, team, equipment, participants and billing. Each Termin goes through the stages Geplant → Bestätigt → Durchgeführt → Abgeschlossen (or Abgesagt). Fields become required with the stage; "Final bestätigen" takes a Termin into the key figures and locks it. Rules: `src/metrics/planning/workflow.py`.
- **Hints** (`src/metrics/hints.py`) at three levels, Kritisch, Wichtig and Info: double-booked people, missing required fields, implausible values, equipment that runs short, calendar differences and more. Blocking hints keep a Termin from the final confirmation; other hints can be accepted with a reason. They are recomputed after every change and nightly.
- **Series:** "+ Termine hinzufügen" adds Termine weekly, every two weeks or on picked dates; changes can apply to "nur diesen Termin" or "diesen und alle folgenden"; bulk actions confirm all or cancel the remaining. The Terminnummer is computed from the order of the Termine.
- **Mein Bereich** (`/mein-bereich`, the page after login): the person's open tasks (as Kursleitung), their next Termine for four weeks and their estimated workload. After a course the lead enters the participants (and the home municipalities where the Kontext asks for them) in a **phone form** (`/mein-bereich/nach-dem-kurs/<id>`).
- **Kalender** (`/kalender`): week and month view with a lane per person, or per piece of equipment.
- **Team** (`/team`): estimated working hours per person and week (course hours plus preparation, overhead and travel, see `[workload]` below), who is free on a day, and the equipment in use.
- **Equipment** ("Material"): critical kits with the number available; a Format names what it needs, each Termin can change it, and a day on which the Termine need more than there is gets a Kritisch hint.
- **Contacts:** a contact person at a place can be chosen on the course page (panel "Kontakt"). Contacts are shown only on the course and place pages, never in the analysis or exports, and are deleted on request.
- **Corrections:** an Abgeschlossen Termin is locked; the team proposes a correction ("Korrektur vorschlagen"), and it changes the figures only once an admin confirms it on "Prüfen".
- **Course calendar** (optional): with a CalDAV calendar, each confirmed Termin can be entered as one event; lead and support are invited and get updates and cancellations from the calendar server. The app never deletes events.
- **Slack messages** (optional, each switched on in the instance config): a DM to the lead after the course while participants are missing, a reminder to the team the day before, and a weekly status to the team channel.

**Analysis** (sidebar group "Auswertung"):

- **Übersicht** (`/`, public): the main chart and the key figures, with time bin and period only.
- **Analyse** (`/analyse`): key figure tiles (Einsätze, Teilnehmende, Orte, Stunden, Schulen) with the comparison to the previous period, filters for category, target group, context and municipality, and the views Zeitverlauf, Kumuliert, Schularten, Schulen, Kontexte & Formate, Klasse / Alter, Gemeinden (map), Orte (point map), plus the optional instructor ranking. A click on a bar or a municipality filters the page. Every chart can be exported as PNG or SVG in fixed sizes with the period as caption ("Grafik exportieren").
- **Project pages** (`/projekte/<slug>`): the analysis limited to a project's contexts, with a Herkunft map where the context asks for home municipalities.
- **Planungszahlen** (`/analyse/planung`): the team's estimated hours, equipment usage, cancellations with reasons, lead time and open billing.
- **Tabelle** (`/tabelle`): the held Termine of a selection with chosen columns, sums per group and a CSV download (UTF-8, semicolon, decimal comma). Contacts, notes and addresses are never exported.

**Reference lists** ("Stammdaten": Orte, Kontexte, Formate, Material, Personen): any team member can propose a new entry, usable at once; an admin confirms, merges or rejects it. Nothing is renamed, merged or deleted automatically.

**Admins** (members of the admin group) also see **Prüfen** (`/pruefen`: proposed entries, proposed corrections, open hints and calendar events without a Termin) and **Personen** (the team, linked logins, "Überall abmelden").

A **scheduler** service runs the time-based jobs (stage moves, hints, calendar check, Slack messages, nightly backups, see [ops/DEPLOY.md](ops/DEPLOY.md#scheduler-and-nightly-backups)).

## Quick start

Requirements: Docker with the Compose plugin.

```sh
cp .env.example .env          # set POSTGRES_PASSWORD; everything else is optional
docker compose up -d --build  # starts PostgreSQL, the app (runs the migrations) and the scheduler
```

The app listens on <http://127.0.0.1:8000> (use `127.0.0.1`, not `localhost`, if you log in through Keycloak: redirect URIs are compared exactly). Without an instance directory it uses the public example in `instance.example/` (neutral theme, sample places). The database starts empty; to see the app with data, use the demo instance below or load a baseline.

## Demo instance

For screenshots, trials and the public repository, `metrics demo-data` fills an empty database with an invented makerspace: places in the example map's municipalities, contexts (one project page that asks for home municipalities), formats with equipment needs, a team with weekly hours, fake contacts (`example.org`, 0123 numbers), and about two school years of courses and Termine in every stage, plus a few deliberate cases (a day the Calliope kits run short, a week above someone's hours, Termine waiting for participants, proposals and an open correction). Hints are computed, calendar fields stay empty. Nothing in it is real, and it reads nothing from `instance/` or old files. The same `--seed` and `--today` give the same data. The demo person "Kim Beispiel" is linked to the login of `AUTH_MODE=off`, so Mein Bereich shows their tasks.

The command refuses a database that has data. `--reset` first empties a database it filled before (and only such a database), to build it again around today. Never point it at the real database: use a throwaway one, e.g. on the test server:

```sh
docker compose -f compose.test.yaml up -d --wait
docker compose -f compose.test.yaml exec db-test psql -U test -d test -c 'CREATE DATABASE demo'
export DATABASE_URL="postgresql+psycopg://test:test@$(docker compose -f compose.test.yaml port db-test 5432)/demo"
export INSTANCE_DIR=instance.example
uv run alembic upgrade head
uv run metrics demo-data                      # --seed N, --today YYYY-MM-DD, --reset
AUTH_MODE=off uv run uvicorn metrics.web.app:app --host 127.0.0.1 --port 8022
# afterwards
docker compose -f compose.test.yaml exec db-test psql -U test -d test -c 'DROP DATABASE demo WITH (FORCE)'
```

Then open <http://127.0.0.1:8022>. `AUTH_MODE=off` opens every page to everyone on that port and treats everyone as admin; use it only locally.

## Team login

Every page except the front page needs a login. `AUTH_MODE` in `.env` selects how:

| `AUTH_MODE` | Login |
| --- | --- |
| `password` (default) | One shared team password, for small instances only. It works only with `ALLOW_PASSWORD_LOGIN=true` and `TEAM_PASSWORD_HASH`; otherwise the pages stay locked. A shared login has no Mein Bereich (tasks belong to a person) and never admin rights. |
| `oidc` | Single sign-on with an OpenID Connect provider such as Keycloak. |
| `off` | No login, for local tests only; everyone is admin. |

A login opens a server-side session in the database; the cookie holds only a random ID. A session lasts at most 12 hours and ends after 2 hours without activity. "Abmelden" ends it at once, and on the Personen page an admin can end all sessions of a person ("Überall abmelden"), for example after removing them from a Keycloak group: their next request goes to the login page, and the groups are read again at every login. Session rows are deleted 30 days after they expired. Set `SESSION_SECRET` to a long random string (it signs the short-lived login state). After changing `.env`, run `docker compose up -d`.

Every form carries a CSRF token and every POST must come from the app's own origin; the pages run under a strict Content-Security-Policy without inline scripts. Login attempts and the public data endpoint are rate-limited per client address. Served over https (or with an `https://` `PUBLIC_URL`), the app sends HSTS and marks its cookies `Secure`.

Shared password (also set `ALLOW_PASSWORD_LOGIN=true`; leave it off wherever Keycloak is available):

```sh
docker compose run --rm app metrics hash-password   # prints TEAM_PASSWORD_HASH=…
```

OpenID Connect (Keycloak): create a confidential client (client authentication on, standard flow) with the valid redirect URI `<PUBLIC_URL>/login/callback`, for example `http://127.0.0.1:8000/login/callback` locally. Then set `OIDC_ISSUER` (`https://<host>/realms/<realm>`), `OIDC_CLIENT_ID`, `OIDC_CLIENT_SECRET` and `PUBLIC_URL`. To admit only part of the realm, set `OIDC_ALLOWED_GROUP` and add a "Group Membership" mapper (claim name `groups`, added to the ID token) to the client. If a shared client scope already uses the claim `groups` for something else, give the mapper its own claim name and set it in `OIDC_GROUPS_CLAIM`. Members of `OIDC_ADMIN_GROUP` (same claim) are admins: they see Prüfen and Personen, decide on proposals and corrections, and may log in even outside `OIDC_ALLOWED_GROUP`. The app server must be able to reach the provider. "Abmelden" ends the app's login only; while the Keycloak session lasts, the next login goes through without asking for the password.

A login is linked to its person in the team at the first login, through the e-mail address an admin entered on the Personen page. Without that link the login works, but Mein Bereich stays empty.

## Configuration

`.env` (start from [`.env.example`](.env.example), which explains every value):

| Values | For |
| --- | --- |
| `POSTGRES_PASSWORD` | The database (required). |
| `AUTH_MODE`, `ALLOW_PASSWORD_LOGIN`, `TEAM_PASSWORD_HASH`, `OIDC_*`, `SESSION_SECRET` | Login, see above. |
| `PUBLIC_URL` | The address people use to open the app. Needed behind a reverse proxy, for https detection, and for the links in Slack messages and calendar events (without it a DM names Mein Bereich instead of linking). |
| `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD`, `CALDAV_ORGANIZER` | The course calendar (optional), see below. |
| `SLACK_BOT_TOKEN` | Slack messages (scope `chat:write`) and the profile pictures of the instructor ranking (`users:read`). Without it nothing is sent. |
| `SLACK_LIST_ID`, `SLACK_PROJECT_LIST_ID` | Only for the one-time transfer from the Slack Lists (`lists:read`). |
| `INSTANCE_DIR`, `LEGACY_DATA_DIR`, `BACKUP_DIR` | Instance directory, legacy CSV directory (mounted at `/legacy`), nightly dumps (default `./backups/nightly`). |
| `APP_BIND`, `APP_PORT`, `FORWARDED_ALLOW_IPS` | Where the app listens and which reverse proxy is trusted. |

### Instance directory

Everything organisation-specific lives in an instance directory that is never committed: `config.toml`, theme and fonts, map boundaries, place lookup table, name mappings, Slack column mapping. See [`instance.example/README.md`](instance.example/README.md) and the commented [`instance.example/config.toml`](instance.example/config.toml). Point `INSTANCE_DIR` in `.env` to your own copy; after a change, restart the app and the scheduler (`docker compose restart app scheduler`).

Sections of `config.toml`:

| Section | What |
| --- | --- |
| `[calendar]` | First month of the school year. |
| `[target_groups]`, `[[projects]]` | Teacher-training contexts and project pages. Both are added to the Kontexte list when the baseline or legacy data is loaded; after that they are maintained on the Stammdaten page "Kontexte" ("Adresse der Projektseite", "Fragt nach den Heimatgemeinden", "Fortbildung: zählt als Lehrkräfte"). |
| `[workload]` | The estimate of working hours on the Team page and in Mein Bereich: lead = course hours × `lead_factor` + `preparation_factor` × course hours + `lead_overhead_hours` + travel; support = course hours × `support_factor` + `support_overhead_hours` + travel. A Format's own fixed hours replace the lead overhead, a place's "Fahrtzeit" is the travel, and each course can override every value. |
| `[messages]` | The Slack messages `after_course`, `reminder` and `weekly_status` (each off by default) and the team `channel` for the weekly status. |
| `[features]` | `instructor_ranking`: sessions per instructor with Slack profile pictures on the analysis page (`metrics sync-instructors`, needs `users:read`; legacy first names are matched to Slack users in `seeds/instructors.csv`). Off by default. |
| `[map]`, `[map.labels]` | Municipality map: GeoJSON file, title, label for sessions outside the map, shorter labels. |
| `[slack]`, `[slack.columns]`, `[slack.stages]`, `[slack.project_list]` | Only for importing from the Slack Lists. |

### Course calendar

With `CALDAV_URL` (a CalDAV calendar such as a Nextcloud calendar of the app's own account, with an app password in `CALDAV_USERNAME`/`CALDAV_PASSWORD`) a Bestätigt Termin can be entered as one event ("In Kalender eintragen"); with `CALDAV_ORGANIZER` (that account's e-mail address) lead and support are invited as attendees, and the calendar server sends invitations, updates and cancellations. Geplant Termine have no event; a Termin that goes back to Geplant or is cancelled gets its event cancelled, never deleted. The scheduler checks the calendar every 15 minutes: it pushes pending changes, reads the answers to invitations (a decline becomes a hint) and lists events without a Termin on "Prüfen".

To try it locally without a real calendar and without e-mail, run the fake calendar and point the app at it:

```sh
uv run metrics fake-calendar --port 5232     # outbox of would-be e-mails: http://127.0.0.1:5232/_outbox
CALDAV_URL=http://127.0.0.1:5232/cal/ CALDAV_ORGANIZER=planung@example.org ...
```

### Absences and weekly hours from Kimai

With `KIMAI_URL` and `KIMAI_TOKEN` (a read-only Kimai Cloud account) the scheduler reads every hour each person's approved absences and the weekly hours of their working contract; people are matched by the Kimai user an admin assigned on the Personen page, else by e-mail. Absences show on the Team page and in the calendar, mark people in the team pickers, and a Termin with an absent person gets a Kritisch hint. The app never writes to Kimai and never deletes an absence: one that disappears from Kimai is marked as withdrawn. Every assumption about Kimai's API is in `src/metrics/kimai_map.py`; `metrics kimai-check` prints the matching, name suggestions for unmatched people, the preference names the token can see (never values) and what the absence endpoints answer per user (read only). `metrics fake-kimai --eingeschraenkt` plays a token without the permissions for other users' contracts and absences.

To try it without Kimai, run the fake with invented users matching `metrics demo-data`:

```sh
uv run metrics fake-kimai --port 8765        # prints KIMAI_URL and KIMAI_TOKEN; state: http://127.0.0.1:8765/_state
KIMAI_URL=http://127.0.0.1:8765/api KIMAI_TOKEN=fake-kimai-token uv run metrics kimai-check
KIMAI_URL=... KIMAI_TOKEN=... uv run metrics scheduler --run kimai
curl -X POST 'http://127.0.0.1:8765/_abwesenheit?email=kim.beispiel@example.org&date=2026-11-20'
curl -X POST 'http://127.0.0.1:8765/_wochenstunden?email=kim.beispiel@example.org&hours=10'
```

## Command line

`metrics <command>`, inside the container `docker compose run --rm app metrics <command>` (or `docker compose exec app …` while the app runs), locally `uv run metrics <command>`. `metrics <command> --help` lists the options.

| Command | What |
| --- | --- |
| `hash-password` | Asks for the shared team password and prints the `TEAM_PASSWORD_HASH=…` line for `.env`. |
| `scheduler [--run JOB]` | The scheduler loop (the `scheduler` service runs it); `--run` runs one job now and exits. |
| `hints [--today DATE]` | Recomputes every hint, as the nightly job does, and prints the count per rule. |
| `demo-data [--seed N] [--today DATE] [--reset]` | Fills an empty throwaway database with demo data (see above). |
| `fake-calendar [--host H] [--port P]` | A fake CalDAV server for local tries, with an outbox instead of e-mail. |
| `fake-kimai [--host H] [--port P]` | A fake Kimai Cloud API for local tries (invented users matching `demo-data`). |
| `kimai-check` | Read only: shows which people match a Kimai user and what the Kimai endpoints answer. |
| `sync-instructors` | Fetches names and profile pictures of the instructors from Slack (needs `SLACK_BOT_TOKEN` with `users:read`). |
| `export-baseline --harmonised CSV --slack-export CSV --out FILE` | Writes the reviewed legacy data into one baseline CSV and prints its sha256. |
| `load-baseline CSV --version NAME [--sha256 HASH]` | Loads the baseline once and freezes it. |
| `load-legacy [--harmonised CSV …] [--slack-export CSV …]` | Replaces all legacy rows with the given CSV files (only entries after the baseline). |
| `transfer-slack (--archive DIR \| --from-archive FOLDER) [--dry-run]` | The one-time transfer from the Slack Lists into Termine, see below. |
| `archive-slack --until DATE --out DIR` | Saves the Slack List as raw JSON and writes the items up to a date as CSV. |
| `delete-slack --csv FILE` | Deletes exactly the items of an `archive-slack` CSV from the Slack List and logs each one (needs `lists:write`). |

The commands from `export-baseline` down only serve the move of the old data into the platform; they will be removed once the team works in the platform.

## Loading the old data

Legacy baseline: all reviewed data up to a cut-off date in one CSV file, loaded once and then frozen. Later legacy loads may only add entries dated after it.

```sh
metrics export-baseline --harmonised … --slack-export … --out baseline.csv   # prints the sha256
docker compose exec app metrics load-baseline /legacy/baseline.csv --version 2026-09-30 --sha256 <sha256>
```

Legacy CSV files from `LEGACY_DATA_DIR` (mounted at `/legacy`; by default `tests/fixtures/`, so these commands load the synthetic test data):

```sh
docker compose exec app metrics load-legacy \
  --harmonised /legacy/harmonised.csv \
  --slack-export /legacy/slack_export.csv
```

Switch from Slack: `metrics transfer-slack` takes every item of the Slack List and, if configured, of a project's own List (`SLACK_PROJECT_LIST_ID`, `[slack.project_list]`) dated after the baseline and creates a Termin for it: open items in their stage, held items as Durchgeführt, to be finally confirmed in the app. It first saves both Lists (raw JSON and a CSV of every column) in a new folder, and can run again from that folder without Slack. Slack is only read (`lists:read`); no item is changed or deleted. Names are stored as recorded; places, contexts, formats and people that are not list entries yet are added as proposed entries for an admin to confirm. Items already transferred are skipped, so a second run adds nothing.

```sh
docker compose run --rm -u "$(id -u)" -v "$PWD/slack_archive:/archive" app metrics transfer-slack --archive /archive --dry-run
docker compose run --rm -u "$(id -u)" -v "$PWD/slack_archive:/archive" app metrics transfer-slack --from-archive /archive/slack-listen_<time>
```

## Data model

One schema for planning, tracking and analysis (`src/metrics/db.py`, migrations in `migrations/`): list entries (places, contexts, formats, people, equipment, each `vorgeschlagen` or `bestätigt`), contact persons at places, courses and their Termine with stage, team, equipment, participants and home municipalities, plus history (Verlauf), proposals and corrections, hints and hint decisions, calendar answers, Slack messages, job runs, login sessions and import runs. Every imported row becomes a course with one Termin that keeps its source and original key; held rows are in stage Abgeschlossen. The reference data of the instance (`seeds/locations.csv`, `seeds/school_types.csv`, `[[projects]]`, `teacher_contexts`, `seeds/instructors.csv`) is added as confirmed list entries when the baseline or legacy data is loaded, and recorded names become confirmed entries exactly as recorded.

## Data rules

All counting rules are in `src/metrics/metrics.py`; the charts, the front page, the tiles, the table and the Slack weekly status count through it.

- **Einsätze:** Termine in stage Abgeschlossen with a date. Planned, held but not finally confirmed and cancelled Termine never count.
- **Teilnehmende:** participants of Termine with Terminnummer 1 only, so children in multi-session courses count once. For a Termin made in the app for a continuous format ("Fortlaufend" on the Formate page) every Termin counts; imported rows of continuous formats carry number 1 on every date. Termine without a number count nothing.
- **Orte:** distinct places with at least one Einsatz; **Schulen:** those of them marked as a school; **Stunden:** the sum of the Dauer of the Einsätze (course hours, not the team's working hours).
- **Target groups:** teacher-training contexts and `Lehrkräfte` → Lehrkräfte; age from 19 or adult age bands → Erwachsene; children → Schulklasse at a school, außerschulisch elsewhere, Unbekannt if the place is unknown.
- Names are stored exactly as recorded; only whitespace is tidied. Nothing is renamed or merged automatically.

## Analysis figures

Besides the main chart, the analysis page shows Einsätze at schools per school type and a map of Einsätze per municipality of one district, with those outside the district and those without a municipality beside it, and a point map of the places. They use the places and school types in the database, which are added from two lookup tables in the instance directory when data is loaded: `seeds/locations.csv` (school type, address, coordinates and municipality per place) and `seeds/school_types.csv`. After that the places are maintained on the Stammdaten page "Orte"; its filter "nur Orte mit Lücken" lists places without the school question, school type, address or municipality, with an unknown school type, or whose municipality does not match their coordinates.

`GET /healthz` is meant for monitoring: 200 with the version, the time of the last finished import and the state of each scheduler job (`"status": "degraded"` when one failed or is overdue), 503 when the database does not answer (after at most 10 seconds).

## Server deployment

Releases are Git tags on `main`; the server checks out a tag and builds the image itself with `ops/update.sh vX.Y.Z` (database dump first, then build, restart and a health check). First installation, releases, the scheduler and backups, the security scan and rollback: [ops/DEPLOY.md](ops/DEPLOY.md).

## Development

```sh
uv sync
uv run pytest
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
```

GitHub Actions runs the same commands on every push and pull request, on Python 3.13 (the Docker image) and 3.14.

Tests use only synthetic data and always the public `instance.example/`: a fixture in `tests/conftest.py` ignores a private `instance/` directory and any login, Slack or calendar settings from `.env`, and points the database URL at a closed port, so no test can read real data. It also caps the test process's memory (`TEST_MEMORY_LIMIT_MB`, 4 GiB by default).

The database tests need a PostgreSQL server: start the throwaway one in `compose.test.yaml` (data in memory, a free host port chosen by Docker) or set `TEST_DATABASE_URL`. Each test run creates its own database there, runs the migrations and drops it at the end. Without a server these tests are skipped; in CI they must run.

```sh
docker compose -f compose.test.yaml up -d --wait
uv run pytest
docker compose -f compose.test.yaml down
```

The vendored libraries (Vega, Vega-Lite, Vega-Embed, htmx) are listed in [`src/metrics/web/static/vendor/README.md`](src/metrics/web/static/vendor/README.md).
