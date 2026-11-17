#!/usr/bin/env bash
# OWASP ZAP scan of a throwaway local instance with demo data (WM-153). Never point it at the server.
#
#   ops/zap-scan.sh [--active] [OUT_DIR]          (default OUT_DIR: zap-report/, gitignored)
#
# 1. builds the image from this checkout (workshop-calculator-app:zap)
# 2. starts a throwaway PostgreSQL (data in memory) and the app with the shared team password,
#    the example instance and the demo baseline from tests/fixtures, hardened like compose.yaml,
#    on its own Docker network and on http://127.0.0.1:${ZAP_PORT:-8021} for a look in the browser
# 3. runs zap-baseline.py without login (start page /) and with a team login (start page
#    /mein-bereich, the session cookie set on every request); with --active also a short active
#    scan (zap-full-scan.py) with the login. Reports: OUT_DIR/{public,team,active}.{html,json,md}
# 4. removes the containers, the network and the image (KEEP=1 leaves them running)
#
# The team password is random per run (ZAP_TEAM_PASSWORD sets one, e.g. to log in with KEEP=1) and
# only lives in this shell; no secret is written anywhere.
# The app is served over plain HTTP without a proxy, so findings about HSTS or cookies without
# `Secure` come from the missing TLS: with an https PUBLIC_URL the app sends both.
set -euo pipefail

cd "$(dirname "$0")/.."
die() { echo "zap-scan: $*" >&2; exit 1; }

ACTIVE=false
if [[ "${1:-}" == "--active" ]]; then ACTIVE=true; shift; fi
OUT="$(realpath -m "${1:-zap-report}")"
PORT="${ZAP_PORT:-8021}"
ZAP_IMAGE="${ZAP_IMAGE:-ghcr.io/zaproxy/zaproxy:stable}"
IMAGE=workshop-calculator-app:zap
NET=wc-zap
DB=wc-zap-db
APP=wc-zap-app
TARGET="http://$APP:8000"

cleanup() {
  if [[ "${KEEP:-0}" == 1 ]]; then
    echo "Left running: $APP on http://127.0.0.1:$PORT. Remove with: docker rm -f $APP $DB; docker network rm $NET; docker rmi $IMAGE"
    return
  fi
  docker rm -f "$APP" "$DB" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker rmi "$IMAGE" >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p "$OUT"
chmod 777 "$OUT"  # ZAP writes its reports as the image's own user

echo "== Building $IMAGE"
docker build --quiet --network host -t "$IMAGE" . >/dev/null

echo "== Starting a throwaway database and the app"
docker network create "$NET" >/dev/null
DB_PASSWORD="$(openssl rand -hex 16)"
docker run -d --name "$DB" --network "$NET" --tmpfs /var/lib/postgresql/data \
  -e POSTGRES_DB=metrics -e POSTGRES_USER=metrics -e POSTGRES_PASSWORD="$DB_PASSWORD" \
  postgres:17 >/dev/null
for _ in $(seq 60); do
  docker exec "$DB" pg_isready -U metrics -d metrics >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$DB" pg_isready -U metrics -d metrics >/dev/null || die "database did not start"

TEAM_PASSWORD="${ZAP_TEAM_PASSWORD:-$(openssl rand -hex 16)}"
PASSWORD_HASH="$(printf %s "$TEAM_PASSWORD" | docker run -i --rm "$IMAGE" \
  python -c 'import sys; from metrics.auth import hash_password; print(hash_password(sys.stdin.read()))')"

docker run -d --name "$APP" --network "$NET" -p "127.0.0.1:$PORT:8000" \
  --read-only --tmpfs /tmp --security-opt no-new-privileges:true --cap-drop ALL \
  -e DATABASE_URL="postgresql+psycopg://metrics:$DB_PASSWORD@$DB:5432/metrics" \
  -e INSTANCE_DIR=/instance -e AUTH_MODE=password -e ALLOW_PASSWORD_LOGIN=true \
  -e TEAM_PASSWORD_HASH="$PASSWORD_HASH" -e SESSION_SECRET="$(openssl rand -hex 32)" \
  -v "$PWD/instance.example:/instance:ro" -v "$PWD/tests/fixtures:/legacy:ro" \
  "$IMAGE" >/dev/null

for _ in $(seq 60); do
  curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null && break
  sleep 1
done
curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null || { docker logs "$APP" | tail -20; die "app did not start"; }

echo "== Loading the demo baseline"
docker exec "$APP" metrics export-baseline --harmonised /legacy/harmonised.csv \
  --slack-export /legacy/slack_export.csv --out /tmp/demo-baseline.csv >/dev/null
docker exec "$APP" metrics load-baseline /tmp/demo-baseline.csv --version demo >/dev/null

echo "== Logging in"
JAR="$(mktemp)"
LOGIN_PAGE="$(curl -sf -c "$JAR" "http://127.0.0.1:$PORT/login")"
CSRF="$(grep -o 'name="csrf_token" value="[^"]*"' <<<"$LOGIN_PAGE" | head -1 | sed 's/.*value="//; s/"$//')"
curl -sf -b "$JAR" -c "$JAR" -o /dev/null -H "Origin: http://127.0.0.1:$PORT" \
  --data-urlencode "csrf_token=$CSRF" --data-urlencode "password=$TEAM_PASSWORD" "http://127.0.0.1:$PORT/login"
SESSION="$(awk '$6 == "metrics_session" {print $7}' "$JAR")"
rm -f "$JAR"
[[ -n "$SESSION" ]] || die "login failed"

# Sends the team session cookie with every request of the scan. Without the CSRF cookie the
# spider's form posts are refused, so the scan does not log itself out.
LOGIN_CONFIG=(-config "replacer.full_list(0).description=session"
  -config "replacer.full_list(0).enabled=true"
  -config "replacer.full_list(0).matchtype=REQ_HEADER"
  -config "replacer.full_list(0).matchstr=Cookie"
  -config "replacer.full_list(0).regex=false"
  -config "replacer.full_list(0).replacement=metrics_session=$SESSION")

zap() {
  # Exit codes 1 and 2 only mean that ZAP found something; the reports say what.
  docker run --rm --network "$NET" -v "$OUT:/zap/wrk:rw" "$ZAP_IMAGE" "$@" || [[ $? -le 2 ]]
}

echo "== Baseline scan without login"
zap zap-baseline.py -t "$TARGET/" -r public.html -J public.json -w public.md -I

echo "== Baseline scan with a team login"
zap zap-baseline.py -t "$TARGET/mein-bereich" -r team.html -J team.json -w team.md -I \
  -z "${LOGIN_CONFIG[*]@Q}"

if $ACTIVE; then
  echo "== Active scan with a team login (local instance only)"
  zap zap-full-scan.py -t "$TARGET/mein-bereich" -r active.html -J active.json -w active.md -I \
    -m 2 -z "${LOGIN_CONFIG[*]@Q} -config scanner.maxScanDurationInMins=30"
fi

echo "Reports in $OUT"
# Alerts by risk; the exit code is 1 when a report holds a medium or high one (the goal is none),
# apart from false positives checked by hand and listed here with the reason (ops/DEPLOY.md).
python3 -I - "$OUT"/*.json <<'EOF'
import json, sys

ACCEPTED = {
    "40038": "Bypassing 403: the 403 is the refused CSRF check of a POST; the GET of the same "
    "path is allowed anyway, and the app ignores X-Original-URL / X-Rewrite-URL",
}
worst = False
for path in sys.argv[1:]:
    print(f"\n{path.rsplit('/', 1)[-1]}")
    for site in json.load(open(path))["site"]:
        for alert in sorted(site["alerts"], key=lambda a: -int(a["riskcode"])):
            note = f"  [accepted: {ACCEPTED[alert['pluginid']]}]" if alert["pluginid"] in ACCEPTED else ""
            print(f"  {alert['riskdesc']:<26} {alert['name']} ({alert['count']}){note}")
            worst |= int(alert["riskcode"]) >= 2 and not note
sys.exit(1 if worst else 0)
EOF
