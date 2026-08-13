#!/bin/bash
# Launches a throttled refresh service; --worker performs the actual refresh.
set -euo pipefail

UNIT=${KING_REFRESH_UNIT:-king-nutrition-refresh}
SCRIPT=${KING_REFRESH_SCRIPT:-/usr/local/sbin/refresh-king-nutrition}
STATE_DIR=${KING_REFRESH_STATE_DIR:-/var/lib/king-nutrition-refresh}
RESULT=$STATE_DIR/result
LOG=/var/log/refresh-king-nutrition.log
ROOT=${KING_ROOT:-/mnt/pool/apps/king-of-meal-prep}
DATASETS=${KING_DATASETS:-$ROOT/datasets}
WORK=$STATE_DIR/work
IMAGE=king-of-meal-prep:local

if [[ "${1:-}" != "--worker" ]]; then
    if systemctl is-active --quiet "$UNIT.service"; then
        exit 0
    fi
    systemd-run \
        --unit="$UNIT" \
        --collect \
        -p Nice=19 \
        -p IOSchedulingClass=idle \
        -p CPUQuota=50% \
        "$SCRIPT" --worker
    exit 0
fi

exec >>"$LOG" 2>&1
echo "--- $(date -u +%FT%TZ) refresh start ---"
install -d -m 0750 -o root -g root "$STATE_DIR"
echo "running" >"$RESULT"
chmod 600 "$RESULT"

if [[ -L "$WORK" ]]; then
    echo "ERROR: refusing symlinked work directory: $WORK"
    echo "failed $(date -u +%FT%TZ)" >"$RESULT"
    exit 1
fi
install -d -m 0700 -o 1000 -g 1000 "$WORK"

if flock -n 9; then
    if docker run --rm \
        --name king-nutrition-refresh-job \
        --user 1000:1000 \
        --read-only \
        --cpus 0.5 \
        --memory 1g \
        --pids-limit 128 \
        --cap-drop ALL \
        --security-opt no-new-privileges:true \
        --mount "type=bind,src=$DATASETS,dst=/data/datasets" \
        --mount "type=bind,src=$WORK,dst=/worktmp" \
        --tmpfs /tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777 \
        -e NUTRITION_DB=/data/datasets/nutrition.db \
        -e KING_DATASETS=/data/datasets \
        -e TMPDIR=/worktmp \
        "$IMAGE" \
        python -m nutrition.bootstrap --force --refresh-downloads
    then
        rm -f \
            "$DATASETS/usda_foundation.zip" \
            "$DATASETS/usda_sr_legacy.zip" \
            "$DATASETS/off.jsonl.gz"
        echo "ok $(date -u +%FT%TZ)" >"$RESULT"
        echo "refresh complete"
        exit 0
    fi
fi 9>/run/lock/king-nutrition-refresh.lock

echo "failed $(date -u +%FT%TZ)" >"$RESULT"
echo "ERROR: nutrition refresh failed"
midclt call mail.send \
    '{"subject":"King nutrition refresh failed","text":"The nutrition database refresh failed. Check /var/log/refresh-king-nutrition.log."}' \
    || true
exit 1
