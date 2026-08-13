"""In-process settings overlay over BOTH app.env (secrets) AND settings_kv (knobs).

Two different storage layers because they have different lifecycles:

  * app.env — secrets + auth + email config. Written via persist_env() with
    fcntl, mode 0600, and a marker file. Compose reads it directly on start;
    cron validates its permissions and clears the marker.

  * settings_kv (SQLite table) — long-tail editable knobs from the design
    doc. No file lock needed because SQLite handles concurrency. No marker
    file needed because the values are read from the DB on every request.

Public surface:
    get(key) -> any            — reads overlay → app.env → kv → ""
    set_env(values: dict)      — in-process only, app.env-shaped keys
    persist_env(values: dict)  — writes app.env + marker (returns bool)
    kv_get(key)                — direct DB read (no overlay)
    kv_set(key, value)         — DB write, is_default=False
    kv_reset(key)              — DB write back to KV_DEFAULTS, is_default=True
    public_view()              — full settings as the UI sees them
"""
from __future__ import annotations

import fcntl
import logging
import os
import re
import threading
from datetime import datetime

import config
import db

log = logging.getLogger("king-of-meal-prep.settings")

ENV_PATH = config.APP_ENV_PATH
MARKER_PATH = config.ENV_MARKER_PATH
_LOCK_PATH = ENV_PATH + ".lock"

# Keys exposed via /api/settings (env section). Anything not in this list
# is not user-editable from the UI even if present in app.env.
MANAGED_ENV_KEYS: tuple[str, ...] = (
    "GEMINI_API_KEY",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASS",
    "SMTP_FROM",
    "OWNER_EMAIL",
)
SECRET_ENV_KEYS: frozenset[str] = frozenset({
    "GEMINI_API_KEY", "SMTP_PASS",
})

_overlay: dict[str, str] = {}
_lock = threading.Lock()


def get(key: str) -> str:
    """Read order: in-process overlay → config attr (which read os.environ
    at startup) → empty string. KV knobs go through kv_get() instead."""
    with _lock:
        if key in _overlay:
            return _overlay[key]
    val = getattr(config, key, "") or os.environ.get(key, "")
    return val or ""


def set_env(values: dict[str, str]) -> None:
    with _lock:
        for k, v in values.items():
            _overlay[k] = v or ""


def public_view() -> dict[str, dict]:
    """Render every managed env key for the Settings UI.

    Secrets become {"set": bool, "length": int} so the actual value never
    round-trips through the browser. Plain values come back as
    {"value": "..."} for prefilling form inputs.
    """
    out: dict[str, dict] = {}
    for k in MANAGED_ENV_KEYS:
        v = get(k)
        if k in SECRET_ENV_KEYS:
            out[k] = {"set": bool(v), "length": len(v)}
        else:
            out[k] = {"value": v}
    return out


def persist_env(values: dict[str, str]) -> bool:
    """Write keys into app.env on disk + touch the marker.

    flock-protected; mode 0600. Cron 6 (sync-king-env.sh) picks up the
    marker file. Compose reads this file directly on the next container start;
    values are also applied to the running process through the overlay.
    """
    try:
        if any(
            not isinstance(key, str)
            or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key)
            or not isinstance(value, str)
            or any(char in value for char in ("\n", "\r", "\0"))
            for key, value in values.items()
        ):
            raise ValueError("invalid environment key or multiline value")
        with open(_LOCK_PATH, "w") as lock:
            os.chmod(_LOCK_PATH, 0o600)
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                try:
                    with open(ENV_PATH) as f:
                        lines = f.readlines()
                except FileNotFoundError:
                    lines = []
                seen: set[str] = set()
                for i, line in enumerate(lines):
                    m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=", line)
                    if not m:
                        continue
                    key = m.group(1)
                    if key in values:
                        lines[i] = f"{key}={values[key]}\n"
                        seen.add(key)
                for k, v in values.items():
                    if k not in seen:
                        lines.append(f"{k}={v}\n")
                tmp = ENV_PATH + ".tmp"
                with open(tmp, "w") as f:
                    f.writelines(lines)
                os.chmod(tmp, 0o600)
                os.replace(tmp, ENV_PATH)
                try:
                    with open(MARKER_PATH, "w") as f:
                        f.write(datetime.utcnow().isoformat())
                    os.chmod(MARKER_PATH, 0o644)
                except Exception as e:
                    log.warning("env-changed marker write failed: %s", e)
                return True
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        log.error("persist_env to %s failed: %s", ENV_PATH, e)
        return False


# Re-export the kv helpers from db so callers have one settings module to import
kv_get = db.kv_get
kv_set = db.kv_set
kv_reset = db.kv_reset
kv_all = db.kv_all
