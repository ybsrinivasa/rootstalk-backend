#!/usr/bin/env bash
# Pull latest from origin/main for one of the four RootsTalk repos,
# rebuild it, and restart the matching systemd service.
#
# Usage:
#   ~/apps/ops/deploy.sh <backend|frontend|client-portal|pwa>
#
# What it does:
# - cd into ~/apps/rootstalk-<repo>
# - git pull --ff-only origin main (refuses to merge — fails loudly
#   if the working tree has diverged)
# - For backend: pip install (in case requirements.txt changed),
#   then alembic upgrade head.
# - For the three Next.js apps: npm ci + npm run build.
# - Restart the matching systemd service.
#
# After it finishes, follow the service logs via:
#   ~/apps/ops/logs.sh <repo>
set -euo pipefail

REPO="${1:?Usage: deploy.sh <backend|frontend|client-portal|pwa>}"
APP_DIR="$HOME/apps/rootstalk-$REPO"
SVC="rootstalk-$REPO"

if [ ! -d "$APP_DIR" ]; then
    echo "ERROR: $APP_DIR does not exist. Did you clone the repo into ~/apps/?" >&2
    exit 1
fi

cd "$APP_DIR"
echo "→ git pull in $APP_DIR"
git pull --ff-only origin main

if [ "$REPO" = "backend" ]; then
    echo "→ pip install (in case requirements.txt changed)"
    venv/bin/pip install -r requirements.txt
    echo "→ alembic upgrade head"
    venv/bin/alembic upgrade head
else
    echo "→ npm ci"
    npm ci
    echo "→ npm run build"
    npm run build
fi

echo "→ restarting $SVC"
sudo systemctl restart "$SVC"
echo "→ done. Tail logs with: ~/apps/ops/logs.sh $REPO"
