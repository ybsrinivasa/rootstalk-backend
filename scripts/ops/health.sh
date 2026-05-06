#!/usr/bin/env bash
# Quick at-a-glance health: are all four services up, and are the
# four public hostnames responding?
#
# Usage:
#   ~/apps/ops/health.sh
#
# Tweak the HOSTS array below if you're running on the production
# server (rootstalk.eywa.farm / rootstalkapi.eywa.farm /
# rootstalk.in PWA) instead of the testing server.
set -uo pipefail

# Adjust per environment.
HOSTS=(
    "https://rstalkapi.eywa.farm/health"
    "https://rstalk.eywa.farm"
    "https://rstalk-ca.eywa.farm"
    "https://rstalk-pwa.eywa.farm"
)

echo "Services:"
for svc in backend frontend client-portal pwa; do
    state=$(systemctl is-active "rootstalk-$svc" 2>/dev/null || true)
    printf "  rootstalk-%-15s %s\n" "$svc" "$state"
done

echo
echo "Public endpoints:"
for url in "${HOSTS[@]}"; do
    code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 5 "$url" || echo "FAIL")
    printf "  %-40s HTTP %s\n" "$url" "$code"
done
