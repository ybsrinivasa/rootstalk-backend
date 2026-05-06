#!/usr/bin/env bash
# Tail the last N lines of one of the RootsTalk services' systemd
# journal and follow new output. Ctrl+C to exit.
#
# Usage:
#   ~/apps/ops/logs.sh <backend|frontend|client-portal|pwa> [N]
#
# Examples:
#   ~/apps/ops/logs.sh backend         # last 200 lines, then follow
#   ~/apps/ops/logs.sh backend 500     # last 500 lines, then follow
#   ~/apps/ops/logs.sh frontend
#
# For Caddy access logs (HTTP requests from outside), run instead:
#   sudo tail -f /var/log/caddy/<host>-access.log
set -euo pipefail
REPO="${1:?Usage: logs.sh <backend|frontend|client-portal|pwa> [N]}"
N="${2:-200}"
sudo journalctl -u "rootstalk-$REPO" -n "$N" -f --no-pager
