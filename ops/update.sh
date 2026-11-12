#!/usr/bin/env bash
# Install or update the app on the server to a release tag, e.g.  ops/update.sh v1.0.0
# A tag from another branch (a prototype) needs the branch named:
#   ops/update.sh v0.2.0-planung.1 --from planung-prototyp
#
# 1. checks the tag (exists, is on main or the named branch, no local changes to tracked files)
# 2. dumps the database into backups/ (skipped on the first install)
# 3. checks out the tag and builds the image workshop-calculator-app:<tag>
# 4. records APP_VERSION=<tag> in .env and restarts the app (migrations run on start)
# 5. waits until /healthz reports the new version
#
# Rollback: run it again with the previous tag. If the newer release changed the database
# schema, restore the dump from step 2 first (ops/DEPLOY.md, "Rollback").
set -euo pipefail

cd "$(dirname "$0")/.."
die() { echo "update: $*" >&2; exit 1; }

VERSION="${1:-}"
BRANCH=main
if [[ "${2:-}" == "--from" ]]; then
  BRANCH="${3:-}"
  [[ -n "$BRANCH" ]] || die "usage: ops/update.sh vX.Y.Z[-suffix] [--from BRANCH]"
fi

[[ "$VERSION" =~ ^v[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.]+)?$ ]] || die "usage: ops/update.sh vX.Y.Z[-suffix] [--from BRANCH]"
[[ -f .env ]] || die ".env missing (see ops/DEPLOY.md)"
grep -q '^COMPOSE_FILE=.*compose.server.yaml' .env || die ".env needs COMPOSE_FILE=compose.yaml:compose.server.yaml"
[[ -z "$(git status --porcelain --untracked-files=no)" ]] || die "local changes to tracked files; the server must run a tag unchanged"

echo "==> Fetching tags from GitHub"
git fetch --quiet --tags origin "$BRANCH" || die "branch $BRANCH not found on GitHub"
git rev-parse --quiet --verify "refs/tags/$VERSION" >/dev/null || die "tag $VERSION not found on GitHub"
git merge-base --is-ancestor "$VERSION" "origin/$BRANCH" || die "tag $VERSION is not on $BRANCH"
[[ "$BRANCH" == main ]] || echo "==> Note: $VERSION comes from branch $BRANCH, not from main"

CURRENT="$(sed -n 's/^APP_VERSION=//p' .env | tail -n 1)"
echo "==> Installed: ${CURRENT:-nothing}, installing: $VERSION"

if [[ -n "$(docker compose ps --quiet --status running db 2>/dev/null)" ]]; then
  mkdir -p backups && chmod 700 backups
  umask 077
  DUMP="backups/before-${VERSION}-$(date +%Y%m%d-%H%M%S).dump"
  echo "==> Database dump: $DUMP"
  docker compose exec -T db pg_dump --username metrics --dbname metrics --format custom >"$DUMP"
  [[ -s "$DUMP" ]] || die "database dump is empty, stopping before any change"
else
  echo "==> Database not running yet: no dump (first install)"
fi

PREVIOUS_REF="$(git rev-parse HEAD)"
echo "==> Checking out $VERSION and building the image"
git -c advice.detachedHead=false checkout --quiet "$VERSION"
if ! APP_VERSION="$VERSION" docker compose build app; then
  git checkout --quiet "$PREVIOUS_REF"
  die "build failed; nothing was restarted, the working copy is back at the previous version"
fi

# Record the version in .env so plain `docker compose ...` commands use this release.
if grep -q '^APP_VERSION=' .env; then
  sed -i "s/^APP_VERSION=.*/APP_VERSION=$VERSION/" .env
else
  printf '\nAPP_VERSION=%s\n' "$VERSION" >>.env
fi

echo "==> Starting"
docker compose up -d --remove-orphans

echo -n "==> Waiting for /healthz to report $VERSION "
HEALTH='import json, urllib.request; print(json.load(urllib.request.urlopen("http://127.0.0.1:8000/healthz", timeout=5))["version"])'
for _ in $(seq 60); do
  if [[ "$(docker compose exec -T app python -c "$HEALTH" 2>/dev/null)" == "$VERSION" ]]; then
    echo
    echo "==> Done: $VERSION is running."
    exit 0
  fi
  echo -n "."
  sleep 2
done
echo
docker compose logs --tail 40 app >&2
die "$VERSION did not become healthy within 2 minutes (logs above). Rollback: ops/update.sh ${CURRENT:-<previous tag>}"
