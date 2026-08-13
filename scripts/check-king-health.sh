#!/bin/bash
# Health probe for King of Meal Prep. Emails after two consecutive failures
# when OWNER_EMAIL is configured.
set -euo pipefail

LOG=/var/log/check-king-health.log
STATE=/var/lib/king-of-meal-prep-health.state
URL=http://127.0.0.1:5002/health
EMAIL=${OWNER_EMAIL:-}

exec >>"$LOG" 2>&1
echo "--- $(date -u +%FT%TZ) ---"

mkdir -p "$(dirname "$STATE")"
fail_count=$(cat "$STATE" 2>/dev/null || echo 0)

if curl -fsS -m 8 "$URL" >/dev/null; then
    if [[ "$fail_count" -gt 0 ]]; then
        echo "ok: recovered after $fail_count failures"
    fi
    echo 0 > "$STATE"
    exit 0
fi

fail_count=$((fail_count + 1))
echo "$fail_count" > "$STATE"
echo "fail #$fail_count"

if [[ "$fail_count" -eq 2 ]]; then
    if [[ -n "$EMAIL" ]]; then
        sudo midclt call mail.send "{\"subject\":\"king-of-meal-prep DOWN\",\"text\":\"Health check has failed twice in a row.\",\"to\":[\"$EMAIL\"]}" || true
    else
        echo "OWNER_EMAIL is unset; skipping failure email"
    fi
fi
