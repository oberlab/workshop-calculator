# Deployment on the server

The server runs a checkout of this repository at a release tag. `ops/update.sh` builds the Docker image on the server from that tag; no image is published anywhere. Private data stays outside Git: `.env` (secrets), `instance/` (config, theme, seeds) and `old_data/` (baseline CSV) are copied to the server once and live next to the checkout.

```
/opt/workshop-calculator/          git checkout of a release tag
├── .env                           secrets and server settings (never in Git)
├── instance/                      private instance directory (copied from the PC)
├── old_data/                      baseline CSV, only for the first load
└── backups/                       database dumps written by ops/update.sh
    └── nightly/                   nightly dumps written by the scheduler (owner uid 10001)
```

## Releasing a new version (PC)

1. Merge the work into `main` and push. Wait until the CI run on `main` is green.
2. Tag the release on `main` and push the tag (the tag push runs CI again):

   ```sh
   git switch main && git pull
   git tag -a v1.0.0 -m "v1.0.0: <what changed>"
   git push origin v1.0.0
   ```

3. On the server: `ops/update.sh v1.0.0` (next section).

Version numbers: raise the last number for fixes, the middle one for new features, the first one for changes that need manual steps on the server (write those steps into the tag message).

## Updating the server

```sh
cd /opt/workshop-calculator
ops/update.sh v1.0.0
```

The script refuses tags that are not on `main` and a checkout with local changes. It dumps the database into `backups/`, builds `workshop-calculator-app:v1.0.0`, restarts the app (database migrations run on start) and waits until `/healthz` reports the new version. `curl -s http://<APP_BIND>:8000/healthz` shows the running version at any time.

## Installing a prototype from a branch

A prototype that is not on `main` yet gets a tag with a suffix on its own branch, e.g. `v0.2.0-planung.1` on `planung-prototyp`. On the server, check out that tag first (so the script that knows `--from` is used), then install it:

```sh
git fetch origin planung-prototyp --tags
git -c advice.detachedHead=false checkout v0.2.0-planung.1
ops/update.sh v0.2.0-planung.1 --from planung-prototyp
```

Going back to a release from `main` works the same way with `ops/update.sh vX.Y.Z`. Since WM-110 planning lives in the metrics database; the prototype's `planning` volume is no longer used and can be removed by hand once nobody needs its demo data (`docker volume rm <project>_planning`).

## Scheduler and nightly backups

The `scheduler` service runs from the app's image (`metrics scheduler`, code in `src/metrics/scheduler.py`). Its jobs:

| Job | When (Europe/Berlin) | What | Overdue on `/healthz` |
| --- | --- | --- | --- |
| `stage-moves` | daily 00:15 | Termine in stage Bestätigt with a past date move to Durchgeführt (recorded in the Verlauf as `scheduler`); Geplant ones are not moved | no success for 26 h |
| `hints` | daily 00:30 | recomputes every hint (some depend on the date, e.g. "Geplant, Datum vorbei"); same as `metrics hints` | no success for 26 h |
| `backup` | daily 03:30 | `pg_dump` into `backups/nightly/`, see below | no success for 26 h |
| `login-sessions` | daily 04:00 | deletes login session rows 30 days after they expired | no success for 26 h |
| `slack-after-course` | daily 09:00 | DM to the Kursleitung 1, 2, 3 and 7 days after a Termin (from Bestätigt on) while participants or Herkunft are missing, with the link to the phone form | no success for 26 h |
| `slack-weekly-status` | daily 09:00, sends on Monday | status to the team channel: this year's Einsätze and Teilnehmende and this week's Termine; a missed Monday is caught up on a later day of the same week, one message per week | no success for 26 h |
| `slack-reminders` | daily 15:00 | DM to everyone on the team of a Bestätigt Termin of tomorrow: time, place, team, equipment | no success for 26 h |
| `calendar` | every 15 minutes | the calendar check: pushes pending and failed event updates, marks missing or changed events, reads the answers to invitations, lists events without a Termin on "Prüfen"; never creates or deletes an event | no success for 45 min |

A job that never ran also counts as overdue; on the first start every job runs once. A failed daily job is tried again an hour later. Every run is a row in `job_runs` (with the error message on failure); `/healthz` shows each job's state (`ok`, `failed`, `overdue`) and its last success, and `"status": "degraded"` (still HTTP 200) when one is not ok. Its log: `docker compose logs scheduler`. A job can be run by hand: `docker compose run --rm scheduler metrics scheduler --run backup`.

The Slack jobs send only with `SLACK_BOT_TOKEN` (scope `chat:write`) and with their switch on in `[messages]` of the instance `config.toml`; otherwise they succeed with "messaging off" or "switched off" in `job_runs`. A message goes only to a person with a Slack ID on the Personen page, and the weekly status needs `channel` with the bot as a member of that channel. Each message is sent at most once (recorded in `slack_messages`); a message Slack refused is tried again with the job's retry, but only until 21:00 of the day its slot belongs to, so nothing goes out at night and a late retry never sends the next day's messages early. A permanent error for one recipient (e.g. an outdated Slack ID) is recorded as `undeliverable` and does not fail the job. Without `CALDAV_URL` the `calendar` job succeeds with "no calendar configured".

**Backups.** Each night writes `metrics-daily-<date>.dump` (pg_dump custom format); the first dump of a month is also kept as `metrics-monthly-<month>.dump`. The newest 14 daily and 12 monthly dumps are kept, older ones with these exact names are deleted. Besides the `login-sessions` job, this is the only automatic deletion in the application: other files in the directory (such as the `before-*.dump` files of `ops/update.sh`) are never touched, and nothing is deleted when the dump fails. The dumps sit on the same server as the database; the PBS snapshots of the VM take them off it.

The directory is a bind mount and the only place the scheduler writes to. Create it once, owned by the image's user (uid 10001) and readable by your group:

```sh
cd /opt/workshop-calculator
mkdir -p backups && sudo install -d -o 10001 -g "$(id -g)" -m 2750 backups/nightly
```

`ops/update.sh` stops when it is missing. On the PC the default is `./backups/nightly` as well (`BACKUP_DIR` in `.env` points elsewhere); without the directory the backup job fails and `/healthz` says so.

### Restoring a dump into a fresh database

This leaves the running database alone. Check the counts, then decide whether to switch.

```sh
DUMP=backups/nightly/metrics-daily-2026-11-13.dump
docker compose exec -T db createdb --username metrics metrics_restore
docker compose exec -T db pg_restore --username metrics --dbname metrics_restore --no-owner --exit-on-error < "$DUMP"
docker compose exec -T db psql --username metrics --dbname metrics_restore -c "
  SELECT (SELECT count(*) FROM appointments WHERE stage = 'Abgeschlossen') AS einsaetze,
         (SELECT sum(participants_total) FROM appointments WHERE stage = 'Abgeschlossen' AND number = 1) AS teilnehmende,
         (SELECT count(DISTINCT c.place_id) FROM appointments a JOIN courses c ON c.id = a.course_id
           WHERE a.stage = 'Abgeschlossen') AS orte,
         (SELECT version_num FROM alembic_version) AS revision;"
```

Run the same query against `--dbname metrics` and compare. To make the restored copy the live database (a person decides this):

```sh
docker compose stop app scheduler
docker compose exec -T db psql --username metrics --dbname postgres -c 'ALTER DATABASE metrics RENAME TO metrics_old'
docker compose exec -T db psql --username metrics --dbname postgres -c 'ALTER DATABASE metrics_restore RENAME TO metrics'
docker compose start app scheduler
```

`metrics_old` stays until someone drops it by hand (`dropdb --username metrics metrics_old`).

## Security scan (OWASP ZAP)

Before going public and before a release that changes pages, headers or login, run the ZAP scan on the PC. It never touches the server or the shared dev database: it builds the image from the checkout, starts a throwaway PostgreSQL (data in memory) and the app with demo data only (example instance, the baseline from `tests/fixtures`, a random team password per run) on its own Docker network, and removes everything afterwards.

```sh
ops/zap-scan.sh                 # baseline scan without login and with a team login, about 5 minutes
ops/zap-scan.sh --active        # plus an active scan with the login, up to 30 minutes more
KEEP=1 ops/zap-scan.sh          # leave the app running on http://127.0.0.1:8021 for a look (ZAP_PORT changes the port)
```

Reports land in `zap-report/` (gitignored): `public.*` (front page, login), `team.*` (from `/mein-bereich`, with the session cookie on every request) and `active.*`, each as HTML, JSON and Markdown. The goal is no medium or high finding. The app runs over plain HTTP here, so ZAP cannot check HSTS or the `Secure` cookie flag. Both depend on the `https://` in `PUBLIC_URL` (or a request that reached the app over https): the app then sends `Strict-Transport-Security` and marks its cookies `Secure`. Check both once on the server with `curl -sI https://<address>/login`.

The script ends with the alerts of each report by risk and exits with 1 when one is medium or high. False positives checked by hand are listed in the script (`ACCEPTED`) with the reason and do not count; so far only "Bypassing 403" (the 403 is the refused CSRF check of a POST route, ZAP then gets 200 for a GET of the same path, and the app ignores `X-Original-URL`). Expected low and informational alerts: "Dangerous JS Functions" and "Suspicious Comments" (vendored Vega and htmx, comments in our scripts), "Storable and Cacheable Content" (static files and the theme, cacheable on purpose), "Session Management Response Identified" (the CSRF cookie), "User Controllable HTML Element Attribute" (filter values put back into the forms, escaped), "Cookie Slack Detector" and "User Agent Fuzzer".

## Rollback

Run the script with the previous tag: `ops/update.sh v0.9.0`. Its image is still on the server, so this is quick.

If the newer release changed the database schema (a new file in `migrations/versions/`), the old version cannot use the newer schema. Restore the dump taken before the update first:

```sh
docker compose stop app
docker compose exec -T db pg_restore --username metrics --dbname metrics --clean --if-exists < backups/before-v1.0.0-<time>.dump
ops/update.sh v0.9.0
```

## First installation

### 1. Server prerequisites

Debian with Docker Engine, the Compose plugin and Git (Docker's own apt repository, <https://docs.docker.com/engine/install/debian/>). The user running the app is in the `docker` group. Outbound HTTPS to github.com, pypi.org, files.pythonhosted.org, Docker Hub and ghcr.io (image build), to the Keycloak server (login), to slack.com (messages) and to the Nextcloud server (course calendar).

```sh
sudo apt install git qemu-guest-agent
sudo systemctl enable --now qemu-guest-agent   # consistent PBS snapshots
docker compose version                         # 2.24 or newer
```

### 2. Checkout

```sh
sudo mkdir -p /opt/workshop-calculator && sudo chown "$USER": /opt/workshop-calculator
git clone https://github.com/oberlab/workshop-calculator.git /opt/workshop-calculator
```

### 3. Private files from the PC

From the repository on the PC (`server` = the SSH name of the server):

```sh
rsync -av --delete instance/ server:/opt/workshop-calculator/instance/
rsync -av old_data/einsaetze_baseline_2026-09-30.csv server:/opt/workshop-calculator/old_data/
```

Later changes to `instance/` are copied the same way, then `docker compose restart app scheduler` (the scheduler reads `[messages]` and `[workload]` from it too).

Settings in the server's `instance/config.toml` that the PC copy may not have yet:

- `[messages]`: switch on `after_course`, `reminder` and `weekly_status` one by one, and set `channel` to the team channel's ID; invite the bot into that channel.
- `[workload]`: the factors and overhead hours of the workload estimate (Team page, Mein Bereich); the defaults are in `instance.example/config.toml`.
- `[slack.project_list]`: only for the one-time transfer of a project's own Slack List, together with `SLACK_PROJECT_LIST_ID`.

### 4. `.env` on the server

Start from `.env.example` and set at least these values. Do not copy the PC's `.env`: the server gets its own database password and session secret.

```sh
COMPOSE_FILE=compose.yaml:compose.server.yaml
COMPOSE_PROJECT_NAME=workshop-calculator

POSTGRES_PASSWORD=<new: openssl rand -hex 32>
SESSION_SECRET=<new: openssl rand -hex 32>

# The server's own Slack app: chat:write for the messages ([messages] in the instance config),
# users:read for sync-instructors, lists:read only until the one-time transfer is done.
SLACK_BOT_TOKEN=<token of the server's Slack app>
SLACK_LIST_ID=<same as on the PC>
SLACK_PROJECT_LIST_ID=<same as on the PC>

# Course calendar: the Nextcloud calendar of the app's own account and an app password.
CALDAV_URL=https://<nextcloud>/remote.php/dav/calendars/<user>/<calendar>/
CALDAV_USERNAME=<app account>
CALDAV_PASSWORD=<app password>
CALDAV_ORGANIZER=<app account's e-mail address>

AUTH_MODE=oidc
# The shared team password stays off on the server.
ALLOW_PASSWORD_LOGIN=false
OIDC_ISSUER=<same as on the PC>
OIDC_CLIENT_ID=<same as on the PC>
OIDC_CLIENT_SECRET=<same as on the PC>
OIDC_ALLOWED_GROUP=<same as on the PC>
OIDC_ADMIN_GROUP=<same as on the PC>
OIDC_GROUPS_CLAIM=<same as on the PC>
PUBLIC_URL=https://<address the team opens>

INSTANCE_DIR=./instance
LEGACY_DATA_DIR=./old_data

# Reachable by the reverse proxy: the server's own network address, not 0.0.0.0.
APP_BIND=<server IP>
APP_PORT=8000
# The reverse proxy's IP: only its X-Forwarded-* headers are trusted.
FORWARDED_ALLOW_IPS=<proxy IP>
```

`chmod 600 .env`. `APP_VERSION` is added by `ops/update.sh`. After a change to `.env`, run `docker compose up -d` (it recreates the app and the scheduler).

`PUBLIC_URL` is more than the login's return address: the links in Slack messages and calendar events are built from it (without it a DM only names Mein Bereich, and events written by the scheduler's calendar check carry no link), and an `https://` address makes the app send HSTS and mark its cookies `Secure`. Lead and support are invited to calendar events only if they have an e-mail address on the Personen page, and get Slack messages only with a Slack ID there.

The containers run hardened (`compose.yaml`): read-only root file system with `/tmp` as tmpfs, `no-new-privileges`, all capabilities dropped, non-root users (the database runs as the image's `postgres` user). The database publishes no port. The access log leaves out client addresses and query strings; Caddy keeps its own log.

**Without a reverse proxy** (the app is opened directly by IP address, e.g. the server's local IP and its Tailscale IP, plain HTTP): change these lines.

```sh
PUBLIC_URL=                 # empty: the login returns to the address that was used
APP_BIND=0.0.0.0            # listen on every interface (local network and Tailscale)
APP_PORT=8000
FORWARDED_ALLOW_IPS=127.0.0.1
```

Docker publishes the port past host firewalls such as ufw, so `0.0.0.0` must only be used on a server that has no interface facing the internet. Without TLS the login cookie is sent unencrypted on the local network (Tailscale traffic is encrypted); use this setup on trusted networks only and add the reverse proxy later.

### 5. Keycloak

On the client add `<PUBLIC_URL>/login/callback` as a valid redirect URI and `<PUBLIC_URL>` as a web origin. Keep the existing `http://127.0.0.1:8000/login/callback` for local work.

Without a reverse proxy, add one redirect URI per address people use, e.g. `http://<local IP>:8000/login/callback` and `http://<Tailscale IP>:8000/login/callback` (Keycloak compares them exactly).

### 6. Install and load the data

```sh
cd /opt/workshop-calculator
ops/update.sh v1.0.0
docker compose run --rm app metrics load-baseline /legacy/einsaetze_baseline_2026-09-30.csv \
  --version 2026-09-30 --sha256 553482b364f6caf944faa776f111e06db685fd52846bf57a3f7bc8e346267242
docker compose run --rm app metrics sync-instructors   # only with [features] instructor_ranking
```

Check the numbers against the PC: the overview's key figures for "Gesamter Zeitraum" must match the PC.

Then an admin enters the team on the Personen page: per person the e-mail address of their login (links the login to the person at the first login, and is the address for calendar invitations), the Slack ID (for the Slack messages) and optionally the weekly hours.

An existing database (revision 0002, baseline in `sessions`) is moved into the new tables by the migration that runs at start-up (`alembic upgrade head`, revision 0003). It reads the reference data from the instance directory, so `INSTANCE_DIR` must point to the real instance. It checks that held sessions, participants and places match before it drops `sessions` and `instructors`; synced Slack rows are not migrated (the transfer from Slack, WM-120, brings them in). Take a dump first (`ops/update.sh` does).

### 7. Reverse proxy (Caddy, optional)

Skip this step when the app is opened directly by IP address. Otherwise add a site to the existing Caddy that is reachable from the internal network only, for example:

```caddyfile
metrics.example.internal {
	@outside not remote_ip private_ranges
	respond @outside 403
	reverse_proxy <server IP>:8000
}
```

Use the address from `PUBLIC_URL`. `remote_ip` only works if Caddy sees the real client address: if outside requests reach Caddy through a NAT or another proxy that hides it, check what Caddy sees before relying on this rule. If the server has a firewall, allow port 8000 only from the proxy.

### 8. Check

- `<address>/healthz` (each address people use) answers with `"status": "ok"` and the version. A few minutes after the first start every scheduler job shows `"state": "ok"` and `backups/nightly/` holds the first dump.
- Logging in through Keycloak works and lands on Mein Bereich; an admin sees "Prüfen" and "Personen" in the sidebar.
- Behind https: `curl -sI https://<address>/login` shows `strict-transport-security` and cookies with `Secure` (see the security scan section).
- With the calendar: "Kalender jetzt prüfen" on "Prüfen" runs without an error. With Slack messages switched on: the `slack-*` jobs in `job_runs` report what they sent (`docker compose logs scheduler`).
- From outside the internal network the app is not reachable (with Caddy: answers 403).
