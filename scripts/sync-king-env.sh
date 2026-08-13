#!/bin/bash
# Validate the runtime environment file after an in-app settings change.
# Compose loads this file directly on future starts; settings are also applied
# to the running process immediately, so no app update or restart is required.
set -euo pipefail

LOG=/var/log/sync-king-env.log
ROOT=${KING_ROOT:-/mnt/pool/apps/king-of-meal-prep}
ENV_PATH=${KING_ENV_PATH:-$ROOT/runtime/app.env}
MARKER=${KING_ENV_MARKER:-$ROOT/runtime/.env-changed}
APP_NAME=king-of-meal-prep

exec >>"$LOG" 2>&1
echo "--- $(date -u +%FT%TZ) ---"

if [[ ! -f "$MARKER" ]]; then
    exit 0
fi

if [[ -L "$ENV_PATH" || ! -f "$ENV_PATH" ]]; then
    echo "ERROR: $ENV_PATH must be a regular, non-symlink file"
    exit 1
fi

owner_uid=$(stat -c '%u' -- "$ENV_PATH")
if [[ "$owner_uid" != "1000" ]]; then
    echo "ERROR: $ENV_PATH has unexpected owner uid $owner_uid"
    exit 1
fi

chmod 600 -- "$ENV_PATH"
rm -f "$MARKER"
echo "validated + cleared marker"
