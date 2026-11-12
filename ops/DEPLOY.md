# Deployment on the server

The server runs a checkout of this repository at a release tag. `ops/update.sh` builds the Docker image on the server from that tag; no image is published anywhere. Private data stays outside Git: `.env` (secrets), `instance/` (config, theme, seeds) and `old_data/` (baseline CSV) are copied to the server once and live next to the checkout.

```
/opt/workshop-calculator/          git checkout of a release tag
├── .env                           secrets and server settings (never in Git)
├── instance/                      private instance directory (copied from the PC)
├── old_data/                      baseline CSV, only for the first load
└── backups/                       database dumps written by ops/update.sh
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

Going back to a release from `main` works the same way with `ops/update.sh vX.Y.Z`. The planning prototype keeps its data in its own volume (`planning`), never in the metrics database.

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

Debian with Docker Engine, the Compose plugin and Git (Docker's own apt repository, <https://docs.docker.com/engine/install/debian/>). The user running the app is in the `docker` group. Outbound HTTPS to github.com, pypi.org, files.pythonhosted.org, Docker Hub and ghcr.io (image build) and to the Keycloak server (login).

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

Later changes to `instance/` are copied the same way, then `docker compose restart app`.

### 4. `.env` on the server

Start from `.env.example` and set at least these values. Do not copy the PC's `.env`: the server gets its own database password and session secret.

```sh
COMPOSE_FILE=compose.yaml:compose.server.yaml
COMPOSE_PROJECT_NAME=workshop-calculator

POSTGRES_PASSWORD=<new: openssl rand -hex 32>
SESSION_SECRET=<new: openssl rand -hex 32>

SLACK_BOT_TOKEN=<same as on the PC>
SLACK_LIST_ID=<same as on the PC>

AUTH_MODE=oidc
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

`chmod 600 .env`. `APP_VERSION` is added by `ops/update.sh`.

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
docker compose run --rm app metrics sync-slack
docker compose run --rm app metrics sync-instructors
```

Check the numbers against the PC: the overview's key figures for "Gesamter Zeitraum" must match the PC after the same Slack sync.

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

- `<address>/healthz` (each address people use) answers with `"status": "ok"` and the version.
- Logging in through Keycloak works and lands on the analysis page; an admin sees "Review" in the sidebar.
- From outside the internal network the app is not reachable (with Caddy: answers 403).
