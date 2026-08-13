"""King of Meal Prep Flask application and HTTP API."""
from __future__ import annotations

import json
import logging
import os
from io import BytesIO
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

from flask import (
    Flask, jsonify, redirect, render_template, request, send_file,
    send_from_directory, session,
)
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.middleware.proxy_fix import ProxyFix

import config
import data_portability
import db
import image_processing
import meal_service
import prepared
import receipts
import recognition
import settings as app_settings
from barcodes import service as barcode_service
from barcodes.gtin import BarcodeError, parse as parse_barcode
from auth import auth_bp, check_auth, require_auth
from extensions import limiter
from planner import targets as targets_mod
from planner import solver as planner_solver
from recipes import dao as recipes_dao
from recipes import scrape as recipes_scrape
from recipes import llm as recipes_llm
from recipes import text_import as recipes_text_import
from nutrition import resolve as nutrition_resolve
from i18n import translate as i18n_translate
from pantry import dao as pantry_dao
from pantry.units import (
    canonical_key,
    from_canonical,
    normalize_unit,
    to_canonical,
)
from validation import (
    ValidationError,
    enum,
    finite_number,
    http_url,
    integer,
    iso_date,
    object_body,
    reject_unknown,
    string_list,
    text,
)


# --- structured JSON logging (clone of recsbot-ui pattern) -----------------
class _JSONFormatter(logging.Formatter):
    # Static set of secret keys that must never appear in logs. Cheap belt-and-
    # suspenders against accidental f-string of an env-shaped dict.
    _SECRET_KEY_RE = ("GEMINI_API_KEY=", "SMTP_PASS=", "ADMIN_PASS_HASH=")

    def format(self, record):
        msg = record.getMessage()
        for k in self._SECRET_KEY_RE:
            if k in msg:
                msg = "<redacted>"
                break
        out = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
            "level": record.levelname,
            "logger": record.name,
            "msg": msg,
        }
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, ensure_ascii=False)


_root = logging.getLogger()
_root.setLevel(logging.INFO)
for h in _root.handlers[:]:
    _root.removeHandler(h)
_h = logging.StreamHandler(sys.stdout)
_h.setFormatter(_JSONFormatter())
_root.addHandler(_h)
log = logging.getLogger("king-of-meal-prep")


# --- Flask app -------------------------------------------------------------
app = Flask(__name__, static_folder="static", template_folder="templates")

BACKUP_UPLOAD_MAX_BYTES = 512 * 1024 * 1024

if not config.SECRET_KEY:
    raise RuntimeError("SECRET_KEY must be set in app.env")
if config.FORCE_HTTPS:
    # Hardened deployments expose Gunicorn behind one trusted reverse proxy.
    # ProxyFix therefore trusts exactly one forwarded value.
    app.wsgi_app = ProxyFix(
        app.wsgi_app, x_for=1, x_proto=1, x_host=1
    )

app.config.update(
    SECRET_KEY=config.SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    # Strict (was Lax) — there's no cross-site link flow that needs the
    # cookie carried over (no OAuth callbacks, no public sharing of pages).
    # Strict denies the cookie on any cross-site navigation, closing the
    # tailnet-adjacent CSRF amplifier.
    SESSION_COOKIE_SAMESITE="Strict",
    SESSION_COOKIE_SECURE=config.FORCE_HTTPS,
    PERMANENT_SESSION_LIFETIME=24 * 3600,
    JSON_AS_ASCII=False,
    # Encrypted backup validation needs a larger multipart envelope. A
    # route-specific guard below retains the 8 MiB cap everywhere else.
    MAX_CONTENT_LENGTH=BACKUP_UPLOAD_MAX_BYTES + 64 * 1024,
    MAX_FORM_MEMORY_SIZE=1024 * 1024,
    MAX_FORM_PARTS=20,
    TRUSTED_HOSTS=list(config.TRUSTED_HOSTS),
    PREFERRED_URL_SCHEME="https" if config.FORCE_HTTPS else "http",
)
limiter.init_app(app)
app.register_blueprint(auth_bp)

# DB init runs once per process. Idempotent.
db.init()


@app.context_processor
def _template_runtime_config():
    return {
        "app_timezone": app_settings.kv_get("timezone") or "Europe/Dublin",
        "prepared_shelf_life_days": (
            app_settings.kv_get("prepared_shelf_life_days") or 4
        ),
        "frozen_shelf_life_days": (
            app_settings.kv_get("frozen_shelf_life_days") or 90
        ),
    }


@app.errorhandler(ValidationError)
def _validation_error(exc):
    return jsonify(exc.as_dict()), 422


@app.errorhandler(sqlite3.IntegrityError)
def _integrity_error(exc):
    log.warning("database constraint rejected request: %s", exc)
    return jsonify({"error": "request conflicts with existing data"}), 409


@app.errorhandler(RequestEntityTooLarge)
def _request_too_large(_exc):
    return jsonify({"error": "upload exceeds the allowed size"}), 413


# --- CSRF + setup-gate middleware ------------------------------------------

# Pages that don't require setup to be done:
_SETUP_EXEMPT_PATHS = (
    "/login", "/reset-password", "/setup",
    "/api/login", "/api/logout", "/api/logout-all", "/api/me",
    "/api/forgot-password", "/api/reset-password",
    "/health", "/favicon.svg",
)
# API endpoints that the setup wizard itself needs to call. These are exempt
# from the setup-gate so a logged-in user can fill in their profile / prefs /
# secrets / kv knobs even though setup_completed_at is still null. Anything
# NOT in this set returns 409 setup_required while the wizard is unfinished.
_SETUP_API_ALLOWED = (
    "/api/settings",
    "/api/settings/profile",
    "/api/settings/preferences",
    "/api/settings/secrets",
    "/api/settings/recompute-targets",
    "/api/settings/test/gemini",
    "/api/setup/finish",
)


def _setup_api_allowed(path: str) -> bool:
    if path in _SETUP_API_ALLOWED:
        return True
    # KV PATCH/reset endpoints have a key suffix; allow those too.
    if path.startswith("/api/settings/kv/"):
        return True
    return False


@app.after_request
def _security_headers(resp):
    """Set defensive headers on every response.

    - CSP locks scripts to self (drops the unpkg.com surface from earlier).
      'unsafe-inline' is needed for the small <script> blocks in login.html
      and reset.html; could be removed by extracting those.
    - Tighten cookie scope to same-site Strict — cross-site links won't
      carry the king cookie even if the user clicks them.
    - Ban framing entirely.
    """
    csp = (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    resp.headers.setdefault("Content-Security-Policy", csp)
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    resp.headers.setdefault(
        "Permissions-Policy",
        "camera=(self), microphone=(), screen-wake-lock=(self)",
    )
    if config.FORCE_HTTPS or request.is_secure:
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    # Don't cache API responses (avoid stale data after writes)
    if request.path.startswith("/api/"):
        resp.headers.setdefault("Cache-Control", "no-store")
    started = getattr(request, "_king_started", None)
    if started is not None:
        log.info(
            "request method=%s path=%s status=%s duration_ms=%.1f",
            request.method,
            request.path,
            resp.status_code,
            (time.monotonic() - started) * 1000,
        )
    return resp


@app.before_request
def _start_request_timer():
    request._king_started = time.monotonic()


@app.before_request
def _route_size_limit():
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    limit = (
        BACKUP_UPLOAD_MAX_BYTES + 64 * 1024
        if request.path == "/api/data/backup/validate"
        else image_processing.MAX_INPUT_BYTES + 64 * 1024
    )
    if request.content_length is not None and request.content_length > limit:
        raise RequestEntityTooLarge()
    return None


@app.before_request
def _csrf_check():
    """Every non-GET /api/* call must carry our per-session CSRF token in
    X-CSRF-Token, matching session.csrf_token. Belt: also requires
    X-Requested-With as a second-layer CORS hint.

    Login + forgot/reset are exempted (the user has no session yet for those).
    """
    if request.method in {"GET", "HEAD", "OPTIONS"}:
        return None
    if not request.path.startswith("/api/"):
        return None
    if request.path in {"/api/login", "/api/forgot-password", "/api/reset-password"}:
        return None
    # Belt: keep the X-Requested-With check
    if request.headers.get("X-Requested-With") != "XMLHttpRequest":
        return jsonify({"error": "CSRF: missing X-Requested-With"}), 403
    # Real CSRF: per-session random token issued at login (or on first /api/me
    # call for already-authenticated tabs from before this deploy).
    sent = request.headers.get("X-CSRF-Token") or ""
    expected = session.get("csrf_token") or ""
    if not expected:
        # Unauthenticated request to a protected endpoint — let the route's
        # @require_auth produce the 401, don't shadow with a 403 here.
        return None
    # Constant-time compare to avoid timing oracles
    import hmac
    if not hmac.compare_digest(sent, expected):
        return jsonify({"error": "CSRF: token mismatch"}), 403
    return None


@app.before_request
def _setup_gate():
    """Redirect authenticated users to /setup until they've finished the wizard.

    Logic:
      - public/auth pages: pass through (login flow needs to work)
      - logged out: pass through; auth check happens at the route level
      - logged in + setup not done + path not in exempt list → redirect /setup
      - logged in + setup done + on /setup → redirect /
    """
    if request.path in _SETUP_EXEMPT_PATHS or request.path.startswith("/static/"):
        if request.path == "/setup" and check_auth() and db.setup_completed():
            return redirect("/")
        return None
    if not check_auth():
        return None  # let the route handle 401
    if not db.setup_completed():
        if request.path.startswith("/api/"):
            # Wizard-needed endpoints pass through; everything else 409.
            if _setup_api_allowed(request.path):
                return None
            return jsonify({"error": "setup_required"}), 409
        return redirect("/setup")
    return None


# --- pages -----------------------------------------------------------------

@app.route("/")
def index():
    """Default landing → /today. The week grid moved to /week as part of
    the daily-ritual redesign — `/` should answer the user's #1 question
    ("what am I cooking tonight?") in 1 second, not show a 7×4 grid."""
    if not check_auth():
        return redirect("/login")
    return redirect("/today")


@app.route("/today")
def today_page():
    if not check_auth():
        return redirect("/login")
    return render_template("today.html")


@app.route("/week")
def week_page():
    if not check_auth():
        return redirect("/login")
    return render_template("week.html")


@app.route("/login")
def login_page():
    if check_auth():
        return redirect("/")
    return send_from_directory("templates", "login.html")


@app.route("/setup")
def setup_page():
    if not check_auth():
        return redirect("/login")
    return render_template("setup.html")


@app.route("/settings")
def settings_page():
    if not check_auth():
        return redirect("/login")
    return render_template("settings.html")


@app.route("/recipes")
def recipes_page():
    if not check_auth():
        return redirect("/login")
    return render_template("recipes.html")


@app.route("/recipes/<int:rid>")
def recipe_page(rid: int):
    if not check_auth():
        return redirect("/login")
    return render_template("recipe_detail.html", rid=rid)


@app.route("/recipes/<int:rid>/cook")
def guided_cook_page(rid: int):
    if not check_auth():
        return redirect("/login")
    return render_template("guided_cook.html", rid=rid)


@app.route("/pantry")
def pantry_page():
    if not check_auth():
        return redirect("/login")
    return render_template("pantry.html")


@app.route("/log")
def log_page():
    if not check_auth():
        return redirect("/login")
    return render_template("log.html")


@app.route("/shopping")
def shopping_page():
    if not check_auth():
        return redirect("/login")
    return render_template("shopping.html")


@app.route("/scan")
def scan_page():
    if not check_auth():
        return redirect("/login")
    return render_template("scan.html")


@app.route("/health")
def health():
    checks = {"database": False, "nutrition": False}
    try:
        db._conn().execute("SELECT 1").fetchone()
        checks["database"] = True
        uri = f"file:{config.NUTRITION_DB}?mode=ro&immutable=1"
        nutrition = sqlite3.connect(uri, uri=True, timeout=2)
        try:
            nutrition.execute("SELECT 1 FROM ingredients LIMIT 1").fetchone()
            checks["nutrition"] = True
        finally:
            nutrition.close()
    except (OSError, sqlite3.Error):
        log.exception("readiness check failed")
    ready = all(checks.values())
    return jsonify({
        "status": "ok" if ready else "degraded",
        "checks": checks,
        "schema_version": db.SCHEMA_VERSION,
    }), 200 if ready else 503


@app.route("/favicon.svg")
def favicon():
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        '<rect width="32" height="32" rx="4" fill="#0a0a0a"/>'
        '<text x="16" y="22" text-anchor="middle" font-size="20" '
        'font-family="Helvetica" font-weight="700" fill="#f5d90a">K</text>'
        "</svg>",
        200,
        {"Content-Type": "image/svg+xml"},
    )


# --- API: settings ---------------------------------------------------------

@app.route("/api/settings", methods=["GET"])
@require_auth
def get_settings():
    """Full settings view: profile + preferences + KV + secrets-redacted env."""
    return jsonify({
        "profile": db.get_user_profile(),
        "preferences": db.get_preferences(),
        "kv": app_settings.kv_all(),
        "env": app_settings.public_view(),
        "setup_completed": db.setup_completed(),
    })


@app.route("/api/settings/profile", methods=["PATCH"])
@require_auth
def patch_profile():
    body = object_body(request)
    allowed = {"weight_kg", "height_cm", "age_years", "sex",
               "activity_level", "goal",
               "training_kcal_delta", "training_protein_delta"}
    reject_unknown(body, allowed)
    payload = {}
    number_fields = {
        "weight_kg": (20, 500),
        "height_cm": (80, 260),
        "age_years": (14, 120),
        "training_kcal_delta": (-1000, 3000),
        "training_protein_delta": (-200, 500),
    }
    for key, (minimum, maximum) in number_fields.items():
        if key in body:
            parser = integer if key in {
                "age_years", "training_kcal_delta", "training_protein_delta"
            } else finite_number
            payload[key] = parser(
                body[key], key, required=True, minimum=minimum, maximum=maximum
            )
    choices = {
        "sex": {"m", "f"},
        "activity_level": {"sedentary", "light", "moderate", "active", "very_active"},
        "goal": {"cut", "maintain", "bulk"},
    }
    for key, values in choices.items():
        if key in body:
            payload[key] = enum(body[key], key, values, required=True)
    if not payload:
        raise ValidationError("no fields")
    db.update_user_profile(**payload)
    # Recompute targets if all stats are present.
    profile = db.get_user_profile()
    required = ("weight_kg", "height_cm", "age_years", "sex",
                "activity_level", "goal")
    if all(profile.get(k) is not None for k in required):
        t = targets_mod.compute(profile)
        db.update_user_profile(
            rest_kcal_target=t["rest_kcal"],
            rest_protein_g=t["rest_protein_g"],
            rest_carbs_g=t["rest_carbs_g"],
            rest_fat_g=t["rest_fat_g"],
        )
    return jsonify({"ok": True, "profile": db.get_user_profile()})


@app.route("/api/settings/preferences", methods=["PATCH"])
@require_auth
def patch_preferences():
    body = object_body(request)
    allowed = {"equipment", "dislikes", "allergies", "favorites", "supermarkets"}
    reject_unknown(body, allowed)
    payload = {}
    for key in ("equipment", "dislikes", "allergies", "supermarkets"):
        if key in body:
            payload[key] = string_list(
                body[key], key, max_items=50, item_length=100
            )
    if "favorites" in body:
        if not isinstance(body["favorites"], list) or len(body["favorites"]) > 500:
            raise ValidationError("must be a list of recipe IDs", field="favorites")
        favorites = [
            integer(value, f"favorites[{index}]", required=True, minimum=1)
            for index, value in enumerate(body["favorites"])
        ]
        existing = {
            row["id"] for row in db._conn().execute(
                "SELECT id FROM recipes WHERE archived_at IS NULL"
            ).fetchall()
        }
        if any(value not in existing for value in favorites):
            raise ValidationError("contains an unknown recipe ID", field="favorites")
        payload["favorites"] = list(dict.fromkeys(favorites))
    if not payload:
        raise ValidationError("no fields")
    db.update_preferences(**payload)
    return jsonify({"ok": True, "preferences": db.get_preferences()})


_EDITABLE_KV = {
    "slot_kcal_split", "cook_time_budget_min", "rotation_window_days",
    "favorites_bypass_mode", "default_servings", "leftover_behavior",
    "prepared_shelf_life_days", "frozen_shelf_life_days",
    "aisle_order", "shopping_include_optional", "barcode_online_lookup",
    "translation_mode",
    "timezone", "planner_preserve_manual", "expiry_days_by_category",
    "macro_split_override", "difficulty_labels", "training_delta_per_slot",
    "weekday_set", "public_base_url",
}


def _validated_kv_value(key: str, value):
    if key == "slot_kcal_split":
        if not isinstance(value, dict) or set(value) != set(meal_service.SLOTS):
            raise ValidationError("must define all four meal slots", field="value")
        parsed = {
            slot: finite_number(
                value[slot], f"value.{slot}", required=True, minimum=0, maximum=1
            )
            for slot in meal_service.SLOTS
        }
        if abs(sum(parsed.values()) - 1.0) > 0.001:
            raise ValidationError("slot split must total 1.0", field="value")
        return parsed
    if key == "cook_time_budget_min":
        days = {"mon", "tue", "wed", "thu", "fri", "sat", "sun"}
        if not isinstance(value, dict) or set(value) != days:
            raise ValidationError("must define all seven days", field="value")
        return {
            day: integer(
                value[day], f"value.{day}", required=True, minimum=0, maximum=480
            )
            for day in days
        }
    if key == "rotation_window_days":
        return integer(value, "value", required=True, minimum=0, maximum=365)
    if key == "favorites_bypass_mode":
        return enum(
            value, "value", {"always", "max_once_per_week", "off"}, required=True
        )
    if key == "default_servings":
        return finite_number(value, "value", required=True, minimum=0.1, maximum=50)
    if key == "leftover_behavior":
        return enum(
            value,
            "value",
            {"next_day_lunch", "same_day", "manual"},
            required=True,
        )
    if key in {"prepared_shelf_life_days", "frozen_shelf_life_days"}:
        return integer(value, "value", required=True, minimum=1, maximum=730)
    if key == "aisle_order":
        result = string_list(value, "value", max_items=20, item_length=30)
        if len(set(result)) != len(result) or "other" not in result:
            raise ValidationError(
                "aisle order must be unique and include other", field="value"
            )
        return result
    if key in {
        "shopping_include_optional",
        "barcode_online_lookup",
        "planner_preserve_manual",
    }:
        if not isinstance(value, bool):
            raise ValidationError("must be true or false", field="value")
        return value
    if key == "translation_mode":
        return enum(
            value, "value", {"hover", "side_by_side", "italian_only"}, required=True
        )
    if key == "timezone":
        zone = text(value, "value", required=True, max_length=100)
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
        try:
            ZoneInfo(zone)
        except ZoneInfoNotFoundError:
            raise ValidationError("unknown IANA timezone", field="value")
        return zone
    if key == "public_base_url":
        if value == "":
            return ""
        return http_url(value, "value", required=True, https_only=True)
    if key in {"difficulty_labels", "weekday_set"}:
        return string_list(value, "value", max_items=10, item_length=30)
    if key in {
        "expiry_days_by_category", "macro_split_override", "training_delta_per_slot"
    }:
        if value is not None and not isinstance(value, dict):
            raise ValidationError("must be an object or null", field="value")
        return value
    raise ValidationError("setting is not editable")


@app.route("/api/settings/kv/<key>", methods=["PATCH"])
@require_auth
def patch_kv(key: str):
    if key not in _EDITABLE_KV:
        return jsonify({"error": "unknown key"}), 404
    body = object_body(request)
    reject_unknown(body, {"value"})
    if "value" not in body:
        raise ValidationError("missing value", field="value")
    value = _validated_kv_value(key, body["value"])
    app_settings.kv_set(key, value, is_default=False)
    return jsonify({"ok": True, "value": value})


@app.route("/api/settings/kv/<key>/reset", methods=["POST"])
@require_auth
def reset_kv(key: str):
    if key not in _EDITABLE_KV:
        return jsonify({"error": "unknown key"}), 404
    ok = app_settings.kv_reset(key)
    if not ok:
        return jsonify({"error": "unknown key"}), 404
    return jsonify({"ok": True, "value": db.KV_DEFAULTS[key]})


@app.route("/api/settings/secrets", methods=["PATCH"])
@require_auth
def patch_secrets():
    """Write to app.env. Values that come back as empty strings on secret keys
    are treated as 'leave alone' — same convention as recsbot-ui."""
    body = object_body(request)
    reject_unknown(body, set(app_settings.MANAGED_ENV_KEYS))
    to_save: dict[str, str] = {}
    for k in app_settings.MANAGED_ENV_KEYS:
        if k not in body:
            continue
        v = body[k]
        if k in app_settings.SECRET_ENV_KEYS and v == "":
            continue
        to_save[k] = text(v, k, max_length=4096) or ""
    if "SMTP_PORT" in to_save:
        to_save["SMTP_PORT"] = str(integer(
            to_save["SMTP_PORT"], "SMTP_PORT", required=True,
            minimum=1, maximum=65535,
        ))
    if not to_save:
        return jsonify(app_settings.public_view())
    persisted = app_settings.persist_env(to_save)
    if not persisted:
        return jsonify({"error": "could not persist settings"}), 500
    app_settings.set_env(to_save)
    return jsonify({"ok": True, "persisted": persisted, "env": app_settings.public_view()})


@app.route("/api/settings/test/gemini", methods=["POST"])
@require_auth
def test_gemini():
    """Probe Gemini's models endpoint with the supplied key (or the persisted
    one if body is empty). One round-trip, no model invoke, no token spend.
    Returns {ok, message, models_count?}."""
    import httpx

    body = object_body(request)
    reject_unknown(body, {"key"})
    key = (
        text(body.get("key"), "key", max_length=4096) or
        app_settings.get("GEMINI_API_KEY")
    )
    if not key:
        return jsonify({"ok": False, "message": "No key set."}), 200

    try:
        with httpx.Client(timeout=8.0, trust_env=False) as client:
            r = client.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                headers={"x-goog-api-key": key},
            )
    except httpx.HTTPError as e:
        return jsonify({"ok": False, "message": f"network error: {e}"}), 200

    if r.status_code == 200:
        try:
            n = len(r.json().get("models") or [])
        except Exception:
            n = 0
        return jsonify({"ok": True, "message": f"Key works ({n} models accessible)."})

    # Friendly mapping of the common Google API error shapes
    try:
        err = r.json().get("error") or {}
        reason = err.get("status") or err.get("message") or f"HTTP {r.status_code}"
    except Exception:
        reason = f"HTTP {r.status_code}"
    if r.status_code == 400 and "API_KEY_INVALID" in (reason or "").upper():
        msg = "Key invalid. Generate a new one at aistudio.google.com/apikey."
    elif r.status_code == 403:
        msg = "Key rejected (403). Check that 'Generative Language API' is enabled on the project."
    elif r.status_code == 429:
        msg = "Rate-limited. Try again in a minute."
    else:
        msg = f"Failed: {reason}"
    return jsonify({"ok": False, "message": msg}), 200


@app.route("/api/settings/recompute-targets", methods=["POST"])
@require_auth
def recompute_targets():
    profile = db.get_user_profile()
    required = ("weight_kg", "height_cm", "age_years", "sex",
                "activity_level", "goal")
    missing = [k for k in required if profile.get(k) is None]
    if missing:
        return jsonify({"error": "missing fields", "fields": missing}), 400
    t = targets_mod.compute(profile)
    db.update_user_profile(
        rest_kcal_target=t["rest_kcal"],
        rest_protein_g=t["rest_protein_g"],
        rest_carbs_g=t["rest_carbs_g"],
        rest_fat_g=t["rest_fat_g"],
    )
    return jsonify({"ok": True, "targets": t})


# --- API: setup wizard -----------------------------------------------------

# --- API: recipes ----------------------------------------------------------

def _resolve_ingredient_for_save(line_or_obj) -> dict:
    """Take either a string ('400 g chicken thigh') or a dict (already-parsed
    fields possibly with manual macros) and produce the dict shape recipes_dao
    expects. Manual values from the form take precedence over the resolver.

    Translation is NOT done here (was a per-ingredient sync Gemini call,
    which made saving a 15-ingredient recipe block a worker thread for ~3s).
    Caller should kick off `_translate_recipe_in_background(rid)` after save.
    """
    if isinstance(line_or_obj, str):
        parsed = nutrition_resolve.parse_line(
            text(line_or_obj, "ingredient", required=True, max_length=500)
        )
        if parsed.quantity is not None and parsed.quantity <= 0:
            raise ValidationError(
                "must be greater than zero", field="ingredient.quantity"
            )
        r = nutrition_resolve.resolve(parsed)
        r["ingredient_key"] = canonical_key(
            r["display_name"], r.get("ingredient_key")
        )
        r["display_name_it"] = None
        return r
    if not isinstance(line_or_obj, dict):
        raise ValidationError("ingredient must be text or an object")
    reject_unknown(line_or_obj, {
        "display_name", "raw", "quantity", "unit", "ingredient_key",
        "kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "optional",
        "nutrition_source", "nutrition_confidence", "nutrition_basis",
        "display_name_it", "grams", "source", "resolved",
        "nutrition_status",
    })
    if (
        not line_or_obj.get("display_name")
        and line_or_obj.get("raw")
        and line_or_obj.get("quantity") in (None, "")
        and line_or_obj.get("unit") in (None, "")
    ):
        return _resolve_ingredient_for_save(line_or_obj["raw"])
    # Keep structured imports structured. Rebuilding "qty unit name" text here
    # used to discard the selected dataset identity and leave stale macros when
    # a user changed an amount or match.
    raw_value = line_or_obj.get("display_name") or line_or_obj.get("raw") or ""
    display_name = text(
        raw_value, "ingredient.display_name",
        required=True, max_length=_LIMITS["ingredient_name"],
    )
    quantity = None
    if line_or_obj.get("quantity") not in (None, ""):
        quantity = finite_number(
            line_or_obj["quantity"], "ingredient.quantity",
            minimum=0.000001, maximum=1_000_000,
        )
    unit = None
    if line_or_obj.get("unit") not in (None, ""):
        unit = normalize_unit(
            text(line_or_obj["unit"], "ingredient.unit", max_length=30)
        )
    proposed_key = text(
        line_or_obj.get("ingredient_key"),
        "ingredient.ingredient_key",
        max_length=160,
    )
    base = nutrition_resolve.resolve_fields(
        display_name=display_name,
        quantity=quantity,
        unit=unit,
        ingredient_key=proposed_key,
    )
    base["optional"] = line_or_obj.get("optional", False)

    # Explicit macro values are a deliberate override. Preserve trusted
    # provenance returned by our own import preview; otherwise label them as
    # user-entered rather than implying a dataset-backed calculation.
    supplied_macros = False
    for key in ("kcal", "protein_g", "carbs_g", "fat_g", "fiber_g"):
        if key in line_or_obj and line_or_obj.get(key) not in (None, ""):
            base[key] = finite_number(
                line_or_obj[key], f"ingredient.{key}",
                minimum=0, maximum=1_000_000,
            )
            supplied_macros = True
    if supplied_macros:
        provenance_source = line_or_obj.get("nutrition_source")
        if provenance_source in {"usda", "off"}:
            base["nutrition_source"] = provenance_source
            proposed_confidence = line_or_obj.get("nutrition_confidence")
            base["nutrition_confidence"] = (
                proposed_confidence
                if proposed_confidence in {"high", "medium", "low", "unknown"}
                else "low"
            )
            base["nutrition_basis"] = (
                text(
                    line_or_obj.get("nutrition_basis"),
                    "ingredient.nutrition_basis",
                    max_length=120,
                )
                or "imported_profile"
            )
        else:
            base["nutrition_source"] = "user"
            base["nutrition_confidence"] = "low"
            base["nutrition_basis"] = "user_entered"
        base["nutrition_status"] = "counted"
        base["resolved"] = True
    if "optional" in line_or_obj and not isinstance(line_or_obj["optional"], bool):
        raise ValidationError("must be true or false", field="ingredient.optional")
    base["ingredient_key"] = canonical_key(
        base["display_name"], base.get("ingredient_key")
    )
    base.setdefault("display_name_it", None)
    return base


def _ingredient_nutrition_status(item: dict) -> str:
    explicit = item.get("nutrition_status")
    if explicit in {
        "counted", "missing_amount", "unknown_unit", "no_match", "no_nutrition",
    }:
        return explicit
    if any(
        item.get(key) is not None
        for key in ("kcal", "protein_g", "carbs_g", "fat_g", "fiber_g")
    ):
        return "counted"
    if item.get("quantity") is None:
        return "missing_amount"
    if item.get("nutrition_basis") in {
        "amount_not_convertible", "unit_not_convertible",
    }:
        return "unknown_unit"
    if (
        item.get("nutrition_source") in {None, "unknown", "manual"}
        or item.get("nutrition_basis") == "no_dataset_match"
    ):
        return "no_match"
    return "no_nutrition"


def _nutrition_summary(items: list[dict]) -> dict:
    statuses = {
        "counted": 0,
        "missing_amount": 0,
        "unknown_unit": 0,
        "no_match": 0,
        "no_nutrition": 0,
    }
    confidence_rank = {"unknown": 0, "low": 1, "medium": 2, "high": 3}
    counted_confidences = []
    for item in items:
        status = _ingredient_nutrition_status(item)
        item["nutrition_status"] = status
        statuses[status] += 1
        if status == "counted":
            counted_confidences.append(
                item.get("nutrition_confidence") or "unknown"
            )
    counted = statuses["counted"]
    total = len(items)
    return {
        "confidence": (
            min(counted_confidences, key=confidence_rank.get)
            if counted_confidences else "unknown"
        ),
        "sourced": counted,
        "counted": counted,
        "total": total,
        "incomplete": total - counted,
        "complete": total > 0 and counted == total,
        "empty": total == 0,
        **statuses,
    }


def _translate_recipe_in_background(rid: int) -> None:
    """Kick off a background thread that batches all of recipe `rid`'s
    ingredient names through translate_many() and writes display_name_it
    back to recipe_ingredients. Idempotent — re-runs are safe."""
    import threading

    def _worker():
        try:
            recipe = db._conn().execute(
                "SELECT name, name_it FROM recipes WHERE id = ?", (rid,)
            ).fetchone()
            ings = db._conn().execute(
                "SELECT id, display_name FROM recipe_ingredients "
                "WHERE recipe_id = ? AND (display_name_it IS NULL OR display_name_it = '')",
                (rid,),
            ).fetchall()
            names = [r["display_name"] for r in ings if r["display_name"]]
            if recipe and recipe["name"] and not recipe["name_it"]:
                names.insert(0, recipe["name"])
            if not names:
                return
            translated = i18n_translate.translate_many(names)
            with db.tx() as c:
                if recipe and recipe["name"] and not recipe["name_it"]:
                    translated_title = translated.get(recipe["name"])
                    if translated_title:
                        c.execute(
                            "UPDATE recipes SET name_it = ? WHERE id = ?",
                            (translated_title, rid),
                        )
                for r in ings:
                    it = translated.get(r["display_name"])
                    if it:
                        c.execute(
                            "UPDATE recipe_ingredients SET display_name_it = ? WHERE id = ?",
                            (it, r["id"]),
                        )
        except Exception as e:
            log.warning("background translate failed for recipe %s: %s", rid, e)
        finally:
            db.close_thread_conn()

    threading.Thread(target=_worker, daemon=True).start()


def _coerce_int(value, default=None, min_val=None, max_val=None):
    """Lenient int coercion for JSON bodies. None on bad input."""
    if value is None or value == "":
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    if min_val is not None and n < min_val:
        n = min_val
    if max_val is not None and n > max_val:
        n = max_val
    return n


def _coerce_float(value, default=None, min_val=None, max_val=None):
    if value is None or value == "":
        return default
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    import math
    if not math.isfinite(n):
        return default
    if min_val is not None and n < min_val:
        n = min_val
    if max_val is not None and n > max_val:
        n = max_val
    return n


# Hard caps on user-supplied strings. Refuse anything beyond — pages don't
# render multi-megabyte recipe names anyway, and unbounded LIKE queries are
# a DoS surface (#10).
_LIMITS = {
    "name": 200, "name_it": 200, "notes": 5000, "step": 2000,
    "ingredient_name": 200, "cuisine": 60, "search": 200,
    "free_text": 500,
}


def _cap(value: str, kind: str) -> str:
    if not isinstance(value, str):
        return value
    return value[:_LIMITS.get(kind, 1000)]


@app.route("/api/recipes", methods=["GET"])
@require_auth
def list_recipes():
    args = request.args
    items = recipes_dao.list_(
        search=_cap(args.get("search", "").strip(), "search"),
        cuisine=_cap(args.get("cuisine", "").strip(), "cuisine"),
        meal_slot=args.get("meal_slot", "").strip()[:20],
        favorites_only=(args.get("favorites") == "1"),
        limit=_coerce_int(args.get("limit"), default=200, min_val=1, max_val=500),
    )
    favs = set(db.get_preferences().get("favorites") or [])
    for it in items:
        it["favorite"] = it["id"] in favs
        it["prepared_portions"] = prepared.available(int(it["id"]))
    return jsonify({"items": items})


@app.route("/api/recipes/<int:rid>", methods=["GET"])
@require_auth
def get_recipe(rid: int):
    r = recipes_dao.get(rid)
    if not r:
        return jsonify({"error": "not found"}), 404
    r["favorite"] = recipes_dao.is_favorite(rid)
    r["prepared_portions"] = prepared.available(rid)
    r["nutrition_summary"] = _nutrition_summary(r.get("ingredients") or [])
    return jsonify(r)


@app.route("/api/nutrition/ingredients/preview", methods=["POST"])
@require_auth
def preview_ingredient_nutrition():
    body = object_body(request)
    reject_unknown(body, {"ingredients"})
    ingredients = body.get("ingredients")
    if not isinstance(ingredients, list):
        raise ValidationError("must be a list", field="ingredients")
    if len(ingredients) > 200:
        raise ValidationError(
            "must have at most 200 items", field="ingredients"
        )
    resolved = [_resolve_ingredient_for_save(item) for item in ingredients]
    return jsonify({
        "items": resolved,
        "summary": _nutrition_summary(resolved),
    })


@app.route("/api/nutrition/search", methods=["GET"])
@require_auth
def search_nutrition():
    query = text(
        request.args.get("q"), "q", required=True,
        max_length=_LIMITS["ingredient_name"],
    )
    return jsonify({
        "items": nutrition_resolve.search(query, limit=8),
    })


def _recipe_payload_to_create_args(body: dict) -> dict:
    """Coerce a JSON body from POST /api/recipes (manual create) into the
    keyword args recipes_dao.create expects. Resolves ingredient lines.

    All user-supplied strings are capped per _LIMITS (#10). Lists are bounded
    so a hostile body can't try to write a million-element ingredients list.
    """
    allowed = {
        "name", "name_it", "source", "source_url", "servings",
        "total_time_min", "active_time_min", "difficulty", "cuisine",
        "meal_slot", "equipment", "steps", "notes", "ingredients",
        "accept_incomplete_nutrition",
    }
    reject_unknown(body, allowed)
    ings_raw = body.get("ingredients") or []
    if not isinstance(ings_raw, list):
        raise ValidationError("must be a list", field="ingredients")
    if len(ings_raw) > 200:
        raise ValidationError("must have at most 200 items", field="ingredients")
    ingredients = [_resolve_ingredient_for_save(x) for x in ings_raw]
    accept_incomplete = body.get("accept_incomplete_nutrition", False)
    if not isinstance(accept_incomplete, bool):
        raise ValidationError(
            "must be true or false",
            field="accept_incomplete_nutrition",
        )
    summary = _nutrition_summary(ingredients)
    if not summary["complete"] and not accept_incomplete:
        raise ValidationError(
            (
                "nutrition review required: "
                f"{summary['incomplete']} of {summary['total']} ingredients "
                "are not counted"
                if summary["total"]
                else "nutrition review required: add ingredients or confirm "
                "saving an empty recipe"
            ),
            field="ingredients",
        )
    # Final cap pass on resolved ingredients (the user could type a 10k-char
    # display name into the form; nutrition.resolve doesn't truncate).
    for ing in ingredients:
        ing["display_name"] = _cap(ing.get("display_name") or "", "ingredient_name")
        if ing.get("display_name_it"):
            ing["display_name_it"] = _cap(ing["display_name_it"], "ingredient_name")
    steps = string_list(
        body.get("steps") or [], "steps", max_items=100,
        item_length=_LIMITS["step"],
    )
    equipment = string_list(
        body.get("equipment") or [], "equipment", max_items=30, item_length=100
    )
    name = text(
        body.get("name"), "name", required=True, max_length=_LIMITS["name"]
    )
    source = enum(
        body.get("source") or "manual", "source",
        {"manual", "url", "ocr", "llm"}, required=True,
    )
    source_url = None
    if body.get("source_url"):
        source_url = http_url(body["source_url"], "source_url")
    return dict(
        name=name,
        name_it=text(
            body.get("name_it"), "name_it", max_length=_LIMITS["name_it"]
        ) or None,
        source=source,
        source_url=source_url,
        servings=integer(
            body.get("servings", 1), "servings",
            required=True, minimum=1, maximum=99,
        ),
        total_time_min=integer(
            body.get("total_time_min"), "total_time_min", minimum=0, maximum=999
        ),
        active_time_min=integer(
            body.get("active_time_min"), "active_time_min", minimum=0, maximum=999
        ),
        difficulty=integer(
            body.get("difficulty"), "difficulty", minimum=1, maximum=5
        ),
        cuisine=text(
            body.get("cuisine"), "cuisine", max_length=_LIMITS["cuisine"]
        ) or None,
        meal_slot=enum(
            body.get("meal_slot"), "meal_slot", meal_service.SLOTS
        ),
        equipment=equipment,
        steps=steps,
        notes=text(body.get("notes"), "notes", max_length=_LIMITS["notes"]) or None,
        ingredients=ingredients,
    )


@app.route("/api/recipes", methods=["POST"])
@require_auth
def create_recipe():
    body = object_body(request)
    args = _recipe_payload_to_create_args(body)
    rid = recipes_dao.create(**args)
    _translate_recipe_in_background(rid)
    return jsonify({"ok": True, "id": rid}), 201


@app.route("/api/recipes/<int:rid>", methods=["PATCH"])
@require_auth
def update_recipe(rid: int):
    body = object_body(request)
    if not recipes_dao.get(rid):
        return jsonify({"error": "not found"}), 404
    allowed = {
        "name", "name_it", "servings", "total_time_min", "active_time_min",
        "difficulty", "cuisine", "meal_slot", "notes", "source_url",
        "equipment", "steps", "ingredients",
        "accept_incomplete_nutrition",
    }
    reject_unknown(body, allowed)
    fields = {}
    text_fields = {
        "name": ("name", True),
        "name_it": ("name_it", False),
        "cuisine": ("cuisine", False),
        "notes": ("notes", False),
    }
    for key, (kind, required) in text_fields.items():
        if key in body:
            fields[key] = text(
                body[key], key, required=required, max_length=_LIMITS[kind]
            ) or None
    for key, minimum, maximum in (
        ("servings", 1, 99),
        ("total_time_min", 0, 999),
        ("active_time_min", 0, 999),
        ("difficulty", 1, 5),
    ):
        if key in body:
            fields[key] = integer(
                body[key], key, minimum=minimum, maximum=maximum
            )
    if "meal_slot" in body:
        fields["meal_slot"] = enum(
            body["meal_slot"], "meal_slot", meal_service.SLOTS
        )
    if "source_url" in body:
        fields["source_url"] = (
            http_url(body["source_url"], "source_url")
            if body["source_url"] else None
        )
    if "equipment" in body:
        fields["equipment"] = string_list(
            body["equipment"], "equipment", max_items=30, item_length=100
        )
    if "steps" in body:
        fields["steps"] = string_list(
            body["steps"], "steps", max_items=100, item_length=_LIMITS["step"]
        )
    ingredients = None
    if "ingredients" in body:
        if not isinstance(body["ingredients"], list) or len(body["ingredients"]) > 200:
            raise ValidationError(
                "must be a list with at most 200 items", field="ingredients"
            )
        ingredients = [_resolve_ingredient_for_save(x) for x in body["ingredients"]]
        accept_incomplete = body.get("accept_incomplete_nutrition", False)
        if not isinstance(accept_incomplete, bool):
            raise ValidationError(
                "must be true or false",
                field="accept_incomplete_nutrition",
            )
        summary = _nutrition_summary(ingredients)
        if not summary["complete"] and not accept_incomplete:
            raise ValidationError(
                (
                    "nutrition review required: "
                    f"{summary['incomplete']} of {summary['total']} "
                    "ingredients are not counted"
                    if summary["total"]
                    else "nutrition review required: add ingredients or "
                    "confirm saving an empty recipe"
                ),
                field="ingredients",
            )
    recipes_dao.update(rid, fields=fields, ingredients=ingredients)
    if ingredients is not None:
        _translate_recipe_in_background(rid)
    return jsonify({"ok": True, "recipe": recipes_dao.get(rid)})


@app.route("/api/recipes/<int:rid>", methods=["DELETE"])
@require_auth
def delete_recipe(rid: int):
    ok = recipes_dao.delete(rid)
    if not ok:
        return jsonify({"error": "not found"}), 404
    # Clean up favorites referencing the deleted recipe
    favs = db.get_preferences().get("favorites") or []
    if rid in favs:
        db.update_preferences(favorites=[x for x in favs if x != rid])
    return jsonify({"ok": True})


@app.route("/api/recipes/<int:rid>/favorite", methods=["POST"])
@require_auth
def favorite_recipe(rid: int):
    if not recipes_dao.get(rid):
        return jsonify({"error": "not found"}), 404
    new_state = recipes_dao.toggle_favorite(rid)
    return jsonify({"ok": True, "favorite": new_state})


@app.route("/api/recipes/<int:rid>/feedback", methods=["PATCH"])
@require_auth
def update_recipe_feedback(rid: int):
    body = object_body(request)
    reject_unknown(body, {"rating", "preference"})
    if not body:
        raise ValidationError("no feedback fields")
    if not recipes_dao.get(rid):
        return jsonify({"error": "not found"}), 404
    current = recipes_dao.get_feedback(rid)
    rating = current["rating"]
    preference = current["preference"]
    if "rating" in body:
        rating = integer(
            body["rating"],
            "rating",
            minimum=1,
            maximum=5,
        )
    if "preference" in body:
        preference = enum(
            body["preference"],
            "preference",
            recipes_dao.FEEDBACK_PREFERENCES,
            required=True,
        )
    try:
        feedback = recipes_dao.set_feedback(
            rid,
            rating=rating,
            preference=preference,
        )
    except KeyError:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True, "feedback": feedback})


@app.route("/api/recipes/from-url", methods=["POST"])
@require_auth
def import_recipe_from_url():
    """Try recipe-scrapers, fall back to Gemini Flash. Returns a *proposed*
    recipe (the caller's UI lets the user review + edit before POSTing to
    /api/recipes to actually save). No DB write happens here."""
    body = object_body(request)
    reject_unknown(body, {"url"})
    url = http_url(body.get("url"), "url", required=True)

    proposal = recipes_scrape.scrape(url)
    used_llm = False
    if not proposal:
        proposal = recipes_llm.parse_url(url)
        used_llm = bool(proposal)
    if not proposal:
        return jsonify({"error": "could not parse this URL", "tried": ["recipe-scrapers", "gemini"]}), 422

    # Resolve each ingredient so the form can prefill macros + IT translations.
    resolved = [_resolve_ingredient_for_save(line) for line in (proposal.get("ingredients") or [])]
    return jsonify({
        "ok": True,
        "used_llm": used_llm,
        "proposal": {
            "name": proposal.get("name"),
            "source_url": url,
            "servings": proposal.get("servings", 1),
            "total_time_min": proposal.get("total_time_min"),
            "active_time_min": proposal.get("active_time_min"),
            "cuisine": proposal.get("cuisine"),
            "notes": proposal.get("notes"),
            "ingredients": resolved,
            "steps": proposal.get("steps") or [],
        },
    })


@app.route("/api/recipes/from-text", methods=["POST"])
@require_auth
def import_recipe_from_text():
    body = object_body(request)
    reject_unknown(body, {"text"})
    raw = text(body.get("text"), "text", required=True, max_length=30_000)
    try:
        proposal = recipes_text_import.parse(raw)
    except ValueError as exc:
        raise ValidationError(str(exc), field="text") from exc
    proposal["ingredients"] = [
        _resolve_ingredient_for_save(line)
        for line in proposal.get("ingredients") or []
    ]
    return jsonify({"ok": True, "proposal": proposal})


# --- API: pantry -----------------------------------------------------------

@app.route("/api/pantry", methods=["GET"])
@require_auth
def list_pantry():
    """Returns items grouped into expiry buckets."""
    from datetime import date, timedelta
    today = date.today()
    items = pantry_dao.list_active()
    buckets = {"urgent": [], "this_week": [], "stocked": [], "frozen_dry": []}
    for it in items:
        cat = pantry_dao.categorize(it["display_name"])
        it["category"] = cat
        exp = it.get("expires_on")
        if cat in ("frozen", "dry"):
            buckets["frozen_dry"].append(it)
            continue
        if exp:
            try:
                d = date.fromisoformat(exp)
                days = (d - today).days
                if days <= 1:
                    buckets["urgent"].append(it)
                elif days <= 7:
                    buckets["this_week"].append(it)
                else:
                    buckets["stocked"].append(it)
                continue
            except ValueError:
                pass
        buckets["stocked"].append(it)
    return jsonify({"buckets": buckets, "total": len(items)})


@app.route("/api/pantry", methods=["POST"])
@require_auth
def add_pantry():
    body = object_body(request)
    reject_unknown(body, {
        "raw", "ingredient_key", "display_name", "quantity", "unit",
        "expires_on", "source", "ean", "portions",
    })
    raw = text(body.get("raw"), "raw", max_length=500) or ""
    proposed_key = text(
        body.get("ingredient_key"),
        "ingredient_key",
        max_length=160,
    )
    if raw and not body.get("display_name"):
        # Parse "400 g chicken thigh" so the user can type free-form
        r = nutrition_resolve.resolve(raw)
        body["ingredient_key"] = r["ingredient_key"]
        body["display_name"]   = r["display_name"]
        body["quantity"]       = r["quantity"]
        body["unit"]           = r["unit"]
    name = text(
        body.get("display_name"), "display_name", required=True,
        max_length=_LIMITS["ingredient_name"],
    )
    qty = finite_number(
        body.get("quantity"), "quantity", required=True,
        minimum=0.000001, maximum=1_000_000,
    )
    portions = finite_number(
        body.get("portions"), "portions",
        minimum=0.01, maximum=1_000,
    )
    unit = normalize_unit(text(body.get("unit") or "g", "unit", max_length=30))
    expires_on = iso_date(body.get("expires_on"), "expires_on")
    source = enum(
        body.get("source") or "manual", "source",
        {"manual", "receipt_ocr", "barcode"}, required=True,
    )
    resolved = nutrition_resolve.resolve(name)
    ingredient_key = canonical_key(
        name,
        proposed_key or resolved.get("ingredient_key"),
    )
    ean = text(body.get("ean"), "ean", max_length=48) or ""
    barcode = None
    barcode_profile = None
    if ean:
        try:
            barcode = parse_barcode(ean)
        except BarcodeError as exc:
            raise ValidationError(str(exc), field="ean") from exc
        barcode_profile = barcode_service.local_nutrition_profile(barcode)
        if barcode_profile and not proposed_key:
            ingredient_key = canonical_key(
                name,
                barcode_profile.get("ingredient_key"),
            )
    with db.tx() as c:
        pid = pantry_dao.add(
            ingredient_key=ingredient_key,
            display_name=name,
            quantity=qty,
            unit=unit,
            expires_on=expires_on,
            source=source,
            portions=portions,
            ean=barcode.canonical if barcode else None,
            nutrition_profile=barcode_profile,
        )
        # Remember a locally named barcode in the same transaction.
        if barcode:
            barcode_service.remember_user_barcode(
                c,
                barcode,
                display_name=name,
                quantity=qty,
                unit=unit,
            )
            recognition.resolve_barcode_queue(
                c,
                barcode,
                display_name=name,
                ingredient_key=ingredient_key,
                quantity=qty,
                unit=unit,
            )
    return jsonify({"ok": True, "id": pid}), 201


@app.route("/api/pantry/<int:pid>", methods=["PATCH"])
@require_auth
def patch_pantry(pid: int):
    body = object_body(request)
    allowed = {"quantity", "unit", "expires_on", "display_name", "portions"}
    reject_unknown(body, allowed)
    if not body:
        raise ValidationError("no fields")
    kwargs = {}
    if "quantity" in body:
        kwargs["quantity"] = finite_number(
            body["quantity"], "quantity", required=True,
            minimum=0.000001, maximum=1_000_000,
        )
    if "unit" in body:
        kwargs["unit"] = normalize_unit(
            text(body["unit"], "unit", required=True, max_length=30)
        )
    if "expires_on" in body:
        kwargs["expires_on"] = iso_date(body["expires_on"], "expires_on")
    if "portions" in body:
        kwargs["portions"] = finite_number(
            body["portions"], "portions",
            minimum=0.01, maximum=1_000,
        )
    if "display_name" in body:
        name = text(
            body["display_name"], "display_name", required=True,
            max_length=_LIMITS["ingredient_name"],
        )
        kwargs["display_name"] = name
        kwargs["ingredient_key"] = nutrition_resolve.resolve(name).get(
            "ingredient_key"
        )
    if not pantry_dao.update(pid, **kwargs):
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


@app.route("/api/pantry/<int:pid>/consume-portion", methods=["POST"])
@require_auth
def consume_pantry_portion(pid: int):
    try:
        item = pantry_dao.consume_portion(pid)
    except pantry_dao.PortionError as exc:
        return jsonify({"error": str(exc)}), 409
    if item is None:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True, "item": item})


@app.route("/api/pantry/<int:pid>", methods=["DELETE"])
@require_auth
def del_pantry(pid: int):
    if pantry_dao.remove(pid):
        return jsonify({"ok": True})
    return jsonify({"error": "not found"}), 404


@app.route("/api/pantry/from-barcode", methods=["POST"])
@require_auth
@limiter.limit("30 per minute")
def pantry_from_barcode():
    body = object_body(request)
    reject_unknown(body, {"ean"})
    ean = text(body.get("ean"), "ean", required=True, max_length=48)
    online_lookup = bool(
        app_settings.kv_get("barcode_online_lookup")
    )
    try:
        barcode = parse_barcode(ean)
        result = barcode_service.lookup(
            ean,
            online=online_lookup,
        )
    except BarcodeError as exc:
        raise ValidationError(str(exc), field="ean") from exc
    except barcode_service.OnlineLookupError:
        log.warning("Open Food Facts barcode lookup unavailable")
        inbox_item = recognition.record_barcode_miss(
            barcode,
            reason="Open Food Facts lookup unavailable",
        )
        return jsonify({
            "ok": False,
            "error": "Open Food Facts is temporarily unavailable",
            "retryable": True,
            "inbox_item": inbox_item,
        }), 503
    if result:
        return jsonify(result)
    inbox_item = recognition.record_barcode_miss(
        barcode,
        reason=(
            "Not found by Open Food Facts"
            if online_lookup
            else "Not found locally; online lookup disabled"
        ),
    )
    return jsonify({
        "ok": False,
        "error": (
            "barcode not found"
            if online_lookup
            else "barcode not found locally; online lookup is disabled"
        ),
        "online_checked": online_lookup,
        "inbox_item": inbox_item,
    }), 404


# --- API: prepared portions ----------------------------------------------

@app.route("/api/prepared", methods=["GET"])
@require_auth
def list_prepared():
    recipe_id = (
        integer(request.args.get("recipe_id"), "recipe_id", minimum=1)
        if request.args.get("recipe_id")
        else None
    )
    items = prepared.list_active(recipe_id)
    usable = [item for item in items if not item["expired"]]
    expired = [item for item in items if item["expired"]]
    return jsonify({
        "items": items,
        "total_batches": len(items),
        "total_portions": round(
            sum(float(item["portions_remaining"]) for item in usable), 2
        ),
        "expired_batches": len(expired),
        "expired_portions": round(
            sum(float(item["portions_remaining"]) for item in expired), 2
        ),
    })


@app.route("/api/prepared/<int:batch_id>", methods=["PATCH"])
@require_auth
def patch_prepared(batch_id: int):
    body = object_body(request)
    reject_unknown(body, {
        "portions_remaining", "expires_on", "frozen", "discard",
    })
    if not body:
        raise ValidationError("no fields")
    kwargs = {}
    if "portions_remaining" in body:
        kwargs["portions_remaining"] = finite_number(
            body["portions_remaining"],
            "portions_remaining",
            required=True,
            minimum=0,
            maximum=1000,
        )
    if "expires_on" in body:
        kwargs["expires_on"] = iso_date(
            body["expires_on"], "expires_on", required=True
        )
    for key in ("frozen", "discard"):
        if key in body:
            if not isinstance(body[key], bool):
                raise ValidationError("must be true or false", field=key)
            kwargs[key] = body[key]
    try:
        item = prepared.update_batch(batch_id, **kwargs)
    except ValueError as exc:
        raise ValidationError(str(exc), field="portions_remaining") from exc
    if not item:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True, "item": item})


# --- API: planner ---------------------------------------------------------

def _monday_of(d_iso: str) -> str:
    from datetime import date, timedelta
    d = date.fromisoformat(iso_date(d_iso, "start", required=True)) if d_iso else date.today()
    return (d - timedelta(days=d.weekday())).isoformat()


@app.route("/api/plan/week", methods=["GET"])
@require_auth
def get_plan_week():
    start = _monday_of(request.args.get("start") or "")
    return jsonify({
        "start": start,
        "plan": planner_solver.list_week(start),
        "version": planner_solver.plan_version(start),
    })


@app.route("/api/plan/week/proposal", methods=["POST"])
@require_auth
def propose_plan_week():
    body = object_body(request)
    reject_unknown(body, {"start", "preserve_manual"})
    start = _monday_of(body.get("start") or "")
    preserve_manual = body.get("preserve_manual")
    if preserve_manual is not None and not isinstance(preserve_manual, bool):
        raise ValidationError("must be true or false", field="preserve_manual")
    try:
        proposal = planner_solver.create_proposal(
            start, preserve_manual=preserve_manual
        )
    except RuntimeError as exc:
        return jsonify({"error": str(exc), "conflict": True}), 409
    return jsonify(proposal)


@app.route("/api/plan/week/commit", methods=["POST"])
@require_auth
def commit_plan_week():
    body = object_body(request)
    reject_unknown(body, {"proposal_id", "expected_version"})
    proposal_id = text(
        body.get("proposal_id"), "proposal_id", required=True, max_length=100
    )
    expected_version = integer(
        body.get("expected_version"), "expected_version",
        required=True, minimum=0,
    )
    try:
        result = planner_solver.commit_proposal(
            proposal_id, expected_version
        )
    except RuntimeError as exc:
        return jsonify({"error": str(exc), "conflict": True}), 409
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 422
    return jsonify({
        "ok": True,
        **result,
        "plan": planner_solver.list_week(result["start"]),
    })


@app.route("/api/plan/<date_str>/<slot>", methods=["PATCH"])
@require_auth
def patch_plan_cell(date_str: str, slot: str):
    date_str = iso_date(date_str, "date", required=True)
    if slot not in meal_service.SLOTS:
        raise ValidationError("invalid slot", field="slot")
    body = object_body(request)
    allowed = {
        "recipe_id", "status", "is_training_day", "servings", "locked",
        "expected_version", "idempotency_key", "cook_mode",
        "prepared_servings", "expires_on", "frozen",
    }
    reject_unknown(body, allowed)
    mutable_fields = {
        "recipe_id", "status", "is_training_day", "servings", "locked",
    }
    if not (set(body) & mutable_fields):
        raise ValidationError("no mutable fields")
    if body.get("status") == "cooked" and not body.get("idempotency_key"):
        raise ValidationError(
            "field is required when status is cooked",
            field="idempotency_key",
        )
    if "idempotency_key" in body and body.get("status") != "cooked":
        raise ValidationError(
            "is only valid when status is cooked",
            field="idempotency_key",
        )
    cooking_fields = {"cook_mode", "prepared_servings", "expires_on", "frozen"}
    if set(body) & cooking_fields and body.get("status") != "cooked":
        raise ValidationError(
            "cooking options require status=cooked", field="status"
        )
    kwargs = {}
    if "recipe_id" in body:
        kwargs["recipe_id"] = integer(
            body["recipe_id"], "recipe_id", required=True, minimum=1
        )
    if "status" in body:
        kwargs["status"] = enum(
            body["status"], "status", meal_service.STATUSES, required=True
        )
    if "servings" in body:
        kwargs["servings"] = finite_number(
            body["servings"], "servings", required=True, minimum=0.1, maximum=99
        )
    for key in ("is_training_day", "locked"):
        if key in body:
            if not isinstance(body[key], bool):
                raise ValidationError("must be true or false", field=key)
            kwargs[key] = body[key]
    if "expected_version" in body:
        kwargs["expected_version"] = integer(
            body["expected_version"], "expected_version",
            required=True, minimum=1,
        )
    if "idempotency_key" in body:
        kwargs["event_key"] = text(
            body["idempotency_key"], "idempotency_key",
            required=True, max_length=100,
        )
    if "cook_mode" in body:
        kwargs["cook_mode"] = enum(
            body["cook_mode"],
            "cook_mode",
            {"auto", "fresh", "prepared"},
            required=True,
        )
    if "prepared_servings" in body:
        kwargs["prepared_servings"] = finite_number(
            body["prepared_servings"],
            "prepared_servings",
            required=True,
            minimum=0.1,
            maximum=200,
        )
    if "expires_on" in body:
        kwargs["expires_on"] = (
            iso_date(body["expires_on"], "expires_on", required=True)
            if body["expires_on"]
            else None
        )
    if "frozen" in body:
        if not isinstance(body["frozen"], bool):
            raise ValidationError("must be true or false", field="frozen")
        kwargs["frozen"] = body["frozen"]
    try:
        result = meal_service.patch_slot(date_str, slot, **kwargs)
    except meal_service.NotFoundError as exc:
        return jsonify({"error": str(exc)}), 404
    except meal_service.ConflictError as exc:
        return jsonify({"error": str(exc), "conflict": True}), 409
    return jsonify({"ok": True, **result})


# --- API: log -------------------------------------------------------------

def _pantry_log_nutrition(
    pantry_item_id,
    quantity,
    unit,
) -> tuple[dict, float, str, dict]:
    pid = integer(
        pantry_item_id, "pantry_item_id",
        required=True, minimum=1,
    )
    item = pantry_dao.get(pid)
    if not item:
        raise ValidationError(
            "pantry item not found", field="pantry_item_id"
        )
    amount = finite_number(
        quantity, "quantity", required=True,
        minimum=0.000001, maximum=1_000_000,
    )
    amount_unit = normalize_unit(
        text(unit or item["unit"], "unit", required=True, max_length=30)
    )
    nutrition = pantry_dao.nutrition_for_amount(
        item,
        quantity=amount,
        unit=amount_unit,
    )
    return item, amount, amount_unit, nutrition


@app.route("/api/log/pantry-preview", methods=["POST"])
@require_auth
def preview_pantry_log():
    body = object_body(request)
    reject_unknown(body, {"pantry_item_id", "quantity", "unit"})
    item, amount, amount_unit, nutrition = _pantry_log_nutrition(
        body.get("pantry_item_id"),
        body.get("quantity"),
        body.get("unit"),
    )
    return jsonify({
        "item": {"id": item["id"], "display_name": item["display_name"]},
        "quantity": amount,
        "unit": amount_unit,
        "nutrition": nutrition,
    })


@app.route("/api/log/<date_str>", methods=["GET"])
@require_auth
def get_log(date_str: str):
    """Today's planned + ad-hoc meals + macro totals vs target."""
    date_str = iso_date(date_str, "date", required=True)
    rows = db._conn().execute(
        "SELECT mp.*, r.name AS rname, r.kcal, r.protein_g, r.carbs_g, r.fat_g, "
        "r.servings AS recipe_servings, ce.cook_mode, ce.prepared_servings "
        "FROM meal_plan mp LEFT JOIN recipes r ON r.id = mp.recipe_id "
        "LEFT JOIN cook_events ce ON ce.date = mp.date AND ce.slot = mp.slot "
        "AND ce.undone_at IS NULL "
        "WHERE mp.date = ?",
        (date_str,),
    ).fetchall()
    planned = []
    totals = {"kcal": 0.0, "protein_g": 0.0, "carbs_g": 0.0, "fat_g": 0.0}
    training_today = False
    for r in rows:
        d = dict(r)
        if d["is_training_day"]:
            training_today = True
        planned.append({
            "slot": d["slot"], "recipe_id": d["recipe_id"], "name": d["rname"],
            "kcal": d["kcal"], "protein_g": d["protein_g"],
            "carbs_g": d["carbs_g"], "fat_g": d["fat_g"],
            "status": d["status"], "servings": d["servings"] or 1,
            "version": int(d["version"]),
            "recipe_servings": d["recipe_servings"] or 1,
            "prepared_portions": (
                prepared.available(int(d["recipe_id"]))
                if d["recipe_id"] else 0
            ),
            "cook_mode": d["cook_mode"],
            "prepared_servings": d["prepared_servings"],
        })
        if d["status"] == "cooked":
            mult = float(d["servings"] or 1)
            for k in totals:
                if d[k] is not None:
                    totals[k] += float(d[k]) * mult
    ah = db._conn().execute(
        "SELECT * FROM ad_hoc_meals WHERE date = ? ORDER BY logged_at",
        (date_str,),
    ).fetchall()
    ad_hoc = []
    for r in ah:
        d = dict(r)
        ad_hoc.append(d)
        for k in ("est_kcal", "est_protein_g", "est_carbs_g", "est_fat_g"):
            if d[k]:
                key = k.replace("est_", "")
                totals[key] += float(d[k])
    profile = db.get_user_profile()
    target = {
        "kcal": profile.get("rest_kcal_target") or 0,
        "protein_g": profile.get("rest_protein_g") or 0,
        "carbs_g": profile.get("rest_carbs_g") or 0,
        "fat_g": profile.get("rest_fat_g") or 0,
    }
    if training_today:
        target["kcal"] += int(profile.get("training_kcal_delta") or 0)
        target["protein_g"] += int(profile.get("training_protein_delta") or 0)
    return jsonify({"date": date_str, "planned": planned, "ad_hoc": ad_hoc,
                    "totals": totals, "target": target,
                    "is_training_day": training_today})


@app.route("/api/log/ad-hoc", methods=["POST"])
@require_auth
def add_ad_hoc():
    from datetime import datetime as _dt, date as _date
    body = object_body(request)
    reject_unknown(body, {
        "recipe_id", "free_text", "servings", "kcal", "protein_g",
        "carbs_g", "fat_g", "date", "slot", "pantry_item_id",
        "quantity", "unit",
    })
    rid = (
        integer(body.get("recipe_id"), "recipe_id", minimum=1)
        if body.get("recipe_id") is not None else None
    )
    free_text = text(
        body.get("free_text"), "free_text", max_length=_LIMITS["free_text"]
    ) or ""
    servings = finite_number(
        body.get("servings", 1), "servings", required=True,
        minimum=0.1, maximum=99,
    )
    macros = {"est_kcal": None, "est_protein_g": None, "est_carbs_g": None, "est_fat_g": None}
    pantry_item_id = None
    food_quantity = None
    food_unit = None
    nutrition_source = "unknown"
    nutrition_confidence = "unknown"
    nutrition_basis = None
    if rid and body.get("pantry_item_id") is not None:
        raise ValidationError(
            "choose either a recipe or a pantry food",
            field="pantry_item_id",
        )
    if rid:
        r = recipes_dao.get(rid)
        if r:
            free_text = free_text or r["name"]
            for k in ("kcal", "protein_g", "carbs_g", "fat_g"):
                v = r.get(k)
                if v is not None:
                    macros[f"est_{k}"] = float(v) * servings
    elif body.get("pantry_item_id") is not None:
        item, food_quantity, food_unit, nutrition = _pantry_log_nutrition(
            body.get("pantry_item_id"),
            body.get("quantity"),
            body.get("unit"),
        )
        if nutrition["nutrition_status"] != "counted":
            raise ValidationError(
                "this pantry food has no usable nutrition for that amount",
                field="pantry_item_id",
            )
        pantry_item_id = int(item["id"])
        free_text = free_text or item["display_name"]
        for key in ("kcal", "protein_g", "carbs_g", "fat_g"):
            macros[f"est_{key}"] = nutrition.get(key)
        nutrition_source = nutrition["nutrition_source"]
        nutrition_confidence = nutrition["nutrition_confidence"]
        nutrition_basis = nutrition["nutrition_basis"]
    elif any(
        body.get(key) is not None
        for key in ("kcal", "protein_g", "carbs_g", "fat_g")
    ):
        for k in ("kcal", "protein_g", "carbs_g", "fat_g"):
            v = body.get(k)
            if v is not None:
                macros[f"est_{k}"] = finite_number(
                    v, k, minimum=0, maximum=1_000_000
                )
        nutrition_source = "user"
        nutrition_confidence = "low"
        nutrition_basis = "user_entered"
    if not free_text and not rid:
        return jsonify({"error": "free_text or recipe_id required"}), 400
    meal_date = iso_date(
        body.get("date") or _date.today().isoformat(), "date", required=True
    )
    slot = enum(
        body.get("slot") or "snack", "slot", meal_service.SLOTS, required=True
    )
    db._conn().execute(
        "INSERT INTO ad_hoc_meals "
        "(date, slot, recipe_id, free_text, servings, est_kcal, "
        "est_protein_g, est_carbs_g, est_fat_g, pantry_item_id, "
        "food_quantity, food_unit, nutrition_source, nutrition_confidence, "
        "nutrition_basis, logged_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (meal_date, slot, rid, free_text, servings,
         macros["est_kcal"], macros["est_protein_g"],
         macros["est_carbs_g"], macros["est_fat_g"],
         pantry_item_id, food_quantity, food_unit,
         nutrition_source, nutrition_confidence, nutrition_basis,
         _dt.now(timezone.utc).isoformat()),
    )
    return jsonify({"ok": True})


@app.route("/api/log/ad-hoc/<int:aid>", methods=["DELETE"])
@require_auth
def del_ad_hoc(aid: int):
    cur = db._conn().execute("DELETE FROM ad_hoc_meals WHERE id = ?", (aid,))
    if not cur.rowcount:
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True})


# --- API: shopping --------------------------------------------------------

@app.route("/api/shopping", methods=["GET"])
@require_auth
def get_shopping():
    """Diff whole recipe batches against prepared portions and pantry stock."""
    start = _monday_of(request.args.get("start") or "")
    import math
    from datetime import date, datetime, timedelta
    from zoneinfo import ZoneInfo
    end = (date.fromisoformat(start) + timedelta(days=7)).isoformat()
    timezone_name = app_settings.kv_get("timezone") or "Europe/Dublin"
    today = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
    demand_start = max(start, today)
    include_optional = bool(app_settings.kv_get("shopping_include_optional"))
    recipe_demands = db._conn().execute(
        "SELECT mp.recipe_id, SUM(mp.servings) AS planned_servings, "
        "r.servings AS recipe_yield "
        "FROM meal_plan mp "
        "JOIN recipes r ON r.id = mp.recipe_id "
        "WHERE mp.date >= ? AND mp.date < ? "
        "AND mp.status IN ('planned','substituted') "
        "GROUP BY mp.recipe_id, r.servings",
        (demand_start, end),
    ).fetchall()
    needs: dict[tuple, dict] = {}
    for demand in recipe_demands:
        planned_servings = float(demand["planned_servings"] or 0)
        prepared_servings = prepared.available(int(demand["recipe_id"]))
        raw_servings = max(0.0, planned_servings - prepared_servings)
        recipe_yield = max(float(demand["recipe_yield"] or 1), 0.1)
        batches = math.ceil(max(0.0, raw_servings - 0.000001) / recipe_yield)
        if batches <= 0:
            continue
        ingredient_rows = db._conn().execute(
            "SELECT ingredient_key, display_name, quantity, unit, optional "
            "FROM recipe_ingredients WHERE recipe_id = ? "
            "AND (? = 1 OR optional = 0)",
            (demand["recipe_id"], 1 if include_optional else 0),
        ).fetchall()
        for row in ingredient_rows:
            ingredient_key = canonical_key(
                row["display_name"], row["ingredient_key"]
            )
            display_unit = normalize_unit(row["unit"])
            canonical_qty, canonical_unit, dimension = to_canonical(
                float(row["quantity"] or 0) * batches,
                display_unit,
            )
            key = (ingredient_key, dimension)
            slot = needs.setdefault(key, {
                "ingredient_key": ingredient_key,
                "display_name": row["display_name"],
                "unit": display_unit,
                "canonical_unit": canonical_unit,
                "canonical_quantity": 0.0,
                "aisle": pantry_dao.categorize(row["display_name"]),
            })
            slot["canonical_quantity"] += canonical_qty
    on_hand: dict[tuple, float] = {}
    for r in pantry_dao.list_active():
        key = (r["ingredient_key"], r["dimension"])
        on_hand[key] = on_hand.get(key, 0) + float(
            r["canonical_quantity"] or 0
        )
    aisle_order = app_settings.kv_get("aisle_order") or [
        "produce", "meat", "fish", "dairy", "dry", "frozen", "other"
    ]
    checks = {
        row["item_key"]: bool(row["checked"])
        for row in db._conn().execute(
            "SELECT item_key, checked FROM shopping_checks WHERE week_start = ?",
            (start,),
        ).fetchall()
    }
    out: dict = {a: [] for a in aisle_order}
    for key, need in needs.items():
        have = on_hand.get(key, 0)
        missing = need.pop("canonical_quantity") - have
        if missing <= 0.000001:
            continue
        item_key = f"{key[0]}|{key[1]}"
        need["item_key"] = item_key
        need["checked"] = checks.get(item_key, False)
        need["quantity"] = round(from_canonical(missing + have, need["unit"]), 2)
        need["have"] = round(from_canonical(have, need["unit"]), 2)
        need["missing"] = round(from_canonical(missing, need["unit"]), 2)
        bucket = need["aisle"] if need["aisle"] in out else "other"
        out.setdefault(bucket, []).append(need)
    for items in out.values():
        items.sort(key=lambda item: item["display_name"].casefold())
    return jsonify({
        "start": start,
        "demand_start": demand_start,
        "aisles": out,
        "aisle_order": aisle_order,
        "supermarkets": db.get_preferences().get("supermarkets") or [],
        "optional_included": include_optional,
    })


@app.route("/api/shopping/check", methods=["PATCH"])
@require_auth
def patch_shopping_check():
    body = object_body(request)
    reject_unknown(body, {"start", "item_key", "checked"})
    start = _monday_of(body.get("start") or "")
    item_key = text(
        body.get("item_key"), "item_key", required=True, max_length=260
    )
    if not isinstance(body.get("checked"), bool):
        raise ValidationError("must be true or false", field="checked")
    from datetime import datetime, timezone
    db._conn().execute(
        "INSERT INTO shopping_checks (week_start, item_key, checked, updated_at) "
        "VALUES (?, ?, ?, ?) ON CONFLICT(week_start, item_key) DO UPDATE SET "
        "checked = excluded.checked, updated_at = excluded.updated_at",
        (
            start,
            item_key,
            1 if body["checked"] else 0,
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    return jsonify({"ok": True, "checked": body["checked"]})


@app.route("/api/shopping/done", methods=["POST"])
@require_auth
def shopping_done():
    """Mark items as added to pantry. Body: items: [{display_name, quantity, unit}]."""
    body = object_body(request)
    reject_unknown(body, {"items", "start"})
    start = _monday_of(body.get("start") or "")
    items = body.get("items")
    if not isinstance(items, list) or not items or len(items) > 200:
        raise ValidationError(
            "items must be a non-empty list with at most 200 entries",
            field="items",
        )
    validated = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise ValidationError("must be an object", field=f"items[{index}]")
        reject_unknown(item, {
            "item_key", "ingredient_key", "display_name", "quantity", "unit",
        })
        name = text(
            item.get("display_name"), f"items[{index}].display_name",
            required=True, max_length=_LIMITS["ingredient_name"],
        )
        quantity = finite_number(
            item.get("quantity"), f"items[{index}].quantity",
            required=True, minimum=0.000001, maximum=1_000_000,
        )
        unit = normalize_unit(text(
            item.get("unit") or "g", f"items[{index}].unit",
            required=True, max_length=30,
        ))
        proposed_key = text(
            item.get("ingredient_key"),
            f"items[{index}].ingredient_key",
            max_length=160,
        )
        item_key = text(
            item.get("item_key"),
            f"items[{index}].item_key",
            max_length=260,
        )
        validated.append((name, quantity, unit, canonical_key(
            name, proposed_key
        ), item_key))
    with db.tx() as c:
        for name, quantity, unit, key, item_key in validated:
            pantry_dao.add(
                ingredient_key=key,
                display_name=name,
                quantity=quantity,
                unit=unit,
                source="manual",
            )
            if item_key:
                c.execute(
                    "DELETE FROM shopping_checks "
                    "WHERE week_start = ? AND item_key = ?",
                    (start, item_key),
                )
    return jsonify({"ok": True, "added": len(validated)})


# --- API: scan / OCR -----------------------------------------------------

@app.route("/api/llm/budget", methods=["GET"])
@require_auth
def llm_budget():
    """Token + call counts: today, this week, this month, by model + status."""
    from datetime import datetime, timedelta
    today = datetime.utcnow().strftime("%Y-%m-%d")
    week_ago = (datetime.utcnow() - timedelta(days=7)).isoformat()
    month_ago = (datetime.utcnow() - timedelta(days=30)).isoformat()

    def query(where_clause, params):
        rows = db._conn().execute(
            f"SELECT model, status, COUNT(*) AS n, "
            f"  COALESCE(SUM(input_tokens),0) AS in_t, "
            f"  COALESCE(SUM(output_tokens),0) AS out_t "
            f"FROM llm_calls WHERE {where_clause} "
            f"GROUP BY model, status",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    return jsonify({
        "today": query("ts LIKE ?", (f"{today}%",)),
        "week":  query("ts >= ?", (week_ago,)),
        "month": query("ts >= ?", (month_ago,)),
    })


@app.route("/api/recipes/generate", methods=["POST"])
@require_auth
def recipe_generate():
    """Gemini Pro recipe generator. Daily cap of 5 calls."""
    from datetime import datetime
    body = object_body(request)
    reject_unknown(body, {"prompt"})
    prompt_input = text(
        body.get("prompt"), "prompt", required=True, max_length=2000
    )

    # Hard daily cap (separate from the model-class caps in llm.py)
    today = datetime.utcnow().strftime("%Y-%m-%d")
    n_today = db._conn().execute(
        "SELECT COUNT(*) AS n FROM llm_calls WHERE purpose='recipe_generate' "
        "AND ts LIKE ? AND status='ok'",
        (f"{today}%",),
    ).fetchone()["n"]
    if n_today >= 5:
        return jsonify({"error": "daily cap reached (5/day)"}), 429

    from llm import call_json, LLMUnavailableError, RateLimitedError
    schema_prompt = (
        "Generate a single recipe matching this user request. Return ONLY valid "
        "JSON, no prose, no markdown fences. Schema:\n"
        "{\n"
        '  "name": "string",\n'
        '  "servings": <integer, default 2>,\n'
        '  "total_time_min": <integer>,\n'
        '  "active_time_min": <integer>,\n'
        '  "cuisine": "<string>",\n'
        '  "meal_slot": "breakfast|lunch|dinner|snack",\n'
        '  "ingredients": ["<qty unit name>", ...],\n'
        '  "steps": ["<step text>", ...],\n'
        '  "notes": "<short string or null>"\n'
        "}\n"
        "Rules: ingredients in 'qty unit name' format (e.g. '400 g chicken thigh'); "
        "no nutrition data; cooking-realistic times; concise steps.\n\n"
        f"User request: {prompt_input}"
    )
    try:
        d = call_json("recipe_generate", "pro", schema_prompt)
    except LLMUnavailableError as e:
        return jsonify({"error": str(e)}), 503
    except RateLimitedError as e:
        return jsonify({"error": str(e)}), 429
    return jsonify({"ok": True, "proposal": d})


def _uploaded_image(field: str = "file"):
    upload = request.files.get(field)
    if upload is None:
        raise ValidationError("file is required", field=field)
    raw = upload.stream.read(image_processing.MAX_INPUT_BYTES + 1)
    if len(raw) > image_processing.MAX_INPUT_BYTES:
        raise RequestEntityTooLarge()
    try:
        return image_processing.decode(raw)
    except image_processing.ImageValidationError as exc:
        raise ValidationError(str(exc), field=field) from exc


def _review_jpeg(image, field: str = "file") -> tuple[bytes, str]:
    try:
        return image_processing.review_jpeg(image)
    except image_processing.ImageValidationError as exc:
        raise ValidationError(str(exc), field=field) from exc


def _receipt_ocr(image) -> str:
    """Run tesseract against a decoded image with bounded time and storage."""
    path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as temporary:
            path = temporary.name
            image.save(temporary, format="PNG", optimize=True)
        result = subprocess.run(
            ["tesseract", path, "stdout", "-l", "eng+ita", "--psm", "6"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            log.warning(
                "tesseract failed returncode=%s stderr=%s",
                result.returncode,
                (result.stderr or "")[:300],
            )
            raise RuntimeError("OCR processing failed")
        return (result.stdout or "")[:20_000]
    finally:
        if path:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass


# --- API: recognition review ------------------------------------------------

@app.route("/api/recognition-inbox", methods=["GET"])
@require_auth
def recognition_inbox_list():
    status = enum(
        request.args.get("status") or "open",
        "status",
        {"open", "resolved", "dismissed"},
        required=True,
    )
    limit = integer(
        request.args.get("limit") or 100,
        "limit",
        minimum=1,
        maximum=200,
    )
    return jsonify({
        "items": recognition.list_items(status=status, limit=limit),
    })


@app.route("/api/recognition-inbox/photo", methods=["POST"])
@require_auth
@limiter.limit("60 per hour")
def recognition_photo_create():
    image = _uploaded_image()
    try:
        image_jpeg, image_sha256 = _review_jpeg(image)
    finally:
        image.close()
    item = recognition.create_product_photo(
        image_jpeg=image_jpeg,
        image_sha256=image_sha256,
        note=text(request.form.get("note"), "note", max_length=500),
        suggested_name=text(
            request.form.get("suggested_name"),
            "suggested_name",
            max_length=200,
        ),
    )
    return jsonify({"ok": True, "item": item}), 201


@app.route(
    "/api/recognition-inbox/<int:item_id>/photo",
    methods=["POST"],
)
@require_auth
@limiter.limit("60 per hour")
def recognition_photo_attach(item_id: int):
    image = _uploaded_image()
    try:
        image_jpeg, image_sha256 = _review_jpeg(image)
    finally:
        image.close()
    item = recognition.attach_photo(
        item_id,
        image_jpeg=image_jpeg,
        image_sha256=image_sha256,
    )
    if not item:
        return jsonify({"error": "open review item not found"}), 404
    return jsonify({"ok": True, "item": item})


@app.route("/api/recognition-inbox/<int:item_id>/image", methods=["GET"])
@require_auth
def recognition_image(item_id: int):
    value = recognition.image(item_id)
    if value is None:
        return jsonify({"error": "image not found"}), 404
    return send_file(
        BytesIO(value),
        mimetype="image/jpeg",
        download_name=f"recognition-{item_id}.jpg",
        max_age=0,
    )


@app.route(
    "/api/recognition-inbox/<int:item_id>/suggestions",
    methods=["GET"],
)
@require_auth
def recognition_suggestions(item_id: int):
    query = text(
        request.args.get("q"),
        "q",
        required=True,
        max_length=120,
    )
    return jsonify({
        "suggestions": recognition.suggestions(item_id, query),
    })


@app.route(
    "/api/recognition-inbox/<int:item_id>/resolve",
    methods=["POST"],
)
@require_auth
def recognition_resolve(item_id: int):
    current = recognition.get(item_id)
    if not current:
        return jsonify({"error": "not found"}), 404
    if current["status"] != "open":
        return jsonify({"error": "review item is already closed"}), 409
    body = object_body(request)
    reject_unknown(body, {
        "display_name",
        "ingredient_key",
        "quantity",
        "unit",
        "expires_on",
        "add_to_pantry",
    })
    add_to_pantry = body.get(
        "add_to_pantry",
        current["kind"] != "receipt_line",
    )
    if not isinstance(add_to_pantry, bool):
        raise ValidationError(
            "must be true or false",
            field="add_to_pantry",
        )
    result = recognition.resolve_item(
        item_id,
        display_name=text(
            body.get("display_name"),
            "display_name",
            required=True,
            max_length=_LIMITS["ingredient_name"],
        ),
        ingredient_key=text(
            body.get("ingredient_key"),
            "ingredient_key",
            max_length=160,
        ),
        quantity=finite_number(
            body.get("quantity"),
            "quantity",
            required=True,
            minimum=0.000001,
            maximum=1_000_000,
        ),
        unit=normalize_unit(text(
            body.get("unit") or "piece",
            "unit",
            required=True,
            max_length=30,
        )),
        expires_on=iso_date(body.get("expires_on"), "expires_on"),
        add_to_pantry=add_to_pantry,
    )
    if not result:
        return jsonify({"error": "open review item not found"}), 404
    return jsonify({"ok": True, **result})


@app.route(
    "/api/recognition-inbox/<int:item_id>/dismiss",
    methods=["POST"],
)
@require_auth
def recognition_dismiss(item_id: int):
    if not recognition.dismiss(item_id):
        return jsonify({"error": "open review item not found"}), 404
    return jsonify({"ok": True})


# --- API: receipt reconciliation --------------------------------------------

@app.route("/api/scan/receipt", methods=["POST"])
@app.route("/api/receipts", methods=["POST"])
@require_auth
@limiter.limit("30 per hour")
def scan_receipt():
    image = _uploaded_image()
    try:
        image_jpeg, image_sha256 = _review_jpeg(image)
        try:
            raw_text = _receipt_ocr(image)
        except FileNotFoundError:
            return jsonify({"error": "tesseract not installed"}), 500
        except subprocess.TimeoutExpired:
            return jsonify({"error": "OCR timeout"}), 504
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), 502
    finally:
        image.close()

    merchant = text(
        request.form.get("merchant"),
        "merchant",
        max_length=200,
    )
    purchased_on = iso_date(
        request.form.get("purchased_on"),
        "purchased_on",
    ) or None
    currency = (
        text(
            request.form.get("currency") or "EUR",
            "currency",
            required=True,
            max_length=3,
        )
        or "EUR"
    ).upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise ValidationError(
            "must be a three-letter currency code",
            field="currency",
        )
    receipt = receipts.create(
        raw_text=raw_text,
        image_jpeg=image_jpeg,
        image_sha256=image_sha256,
        merchant=merchant,
        purchased_on=purchased_on,
        currency=currency,
    )
    legacy_lines = [
        {
            "quantity": item["quantity"],
            "name": item["display_name"],
        }
        for item in receipt["items"]
    ]
    return jsonify({
        "ok": True,
        "receipt": receipt,
        "raw_text": raw_text[:5000],
        "lines": legacy_lines,
    }), 201


@app.route("/api/receipts", methods=["GET"])
@require_auth
def receipt_list():
    limit = integer(
        request.args.get("limit") or 20,
        "limit",
        minimum=1,
        maximum=100,
    )
    return jsonify({"receipts": receipts.list_receipts(limit=limit)})


@app.route("/api/receipts/<int:receipt_id>", methods=["GET"])
@require_auth
def receipt_get(receipt_id: int):
    receipt = receipts.get(receipt_id)
    if not receipt:
        return jsonify({"error": "not found"}), 404
    return jsonify(receipt)


@app.route("/api/receipts/<int:receipt_id>/image", methods=["GET"])
@require_auth
def receipt_image(receipt_id: int):
    value = receipts.image(receipt_id)
    if value is None:
        return jsonify({"error": "image not found"}), 404
    return send_file(
        BytesIO(value),
        mimetype="image/jpeg",
        download_name=f"receipt-{receipt_id}.jpg",
        max_age=0,
    )


@app.route("/api/receipts/<int:receipt_id>/commit", methods=["POST"])
@require_auth
def receipt_commit(receipt_id: int):
    body = object_body(request)
    reject_unknown(body, {"items"})
    values = body.get("items")
    if not isinstance(values, list):
        raise ValidationError("must be a list", field="items")
    if len(values) > 60:
        raise ValidationError("must have at most 60 items", field="items")
    items = []
    for index, value in enumerate(values):
        field = f"items[{index}]"
        if not isinstance(value, dict):
            raise ValidationError("must be an object", field=field)
        reject_unknown(value, {
            "id",
            "action",
            "display_name",
            "quantity",
            "unit",
            "line_total",
            "ingredient_key",
        })
        item = {
            "id": integer(
                value.get("id"),
                f"{field}.id",
                required=True,
                minimum=1,
            ),
            "action": enum(
                value.get("action"),
                f"{field}.action",
                {"add", "merge", "skip"},
                required=True,
            ),
        }
        if item["action"] != "skip":
            item.update({
                "display_name": text(
                    value.get("display_name"),
                    f"{field}.display_name",
                    required=True,
                    max_length=_LIMITS["ingredient_name"],
                ),
                "quantity": finite_number(
                    value.get("quantity"),
                    f"{field}.quantity",
                    required=True,
                    minimum=0.000001,
                    maximum=1_000_000,
                ),
                "unit": normalize_unit(text(
                    value.get("unit") or "piece",
                    f"{field}.unit",
                    required=True,
                    max_length=30,
                )),
                "line_total": finite_number(
                    value.get("line_total"),
                    f"{field}.line_total",
                    minimum=0,
                    maximum=1_000_000,
                ),
                "ingredient_key": text(
                    value.get("ingredient_key"),
                    f"{field}.ingredient_key",
                    max_length=160,
                ),
            })
        items.append(item)
    try:
        result = receipts.commit(receipt_id, items=items)
    except receipts.ReceiptConflictError as exc:
        return jsonify({"error": str(exc)}), 409
    if result.get("not_found"):
        return jsonify({"error": "not found"}), 404
    return jsonify({"ok": True, **result})


@app.route("/api/receipts/<int:receipt_id>/discard", methods=["POST"])
@require_auth
def receipt_discard(receipt_id: int):
    current = receipts.get(receipt_id)
    if not current:
        return jsonify({"error": "not found"}), 404
    if not receipts.discard(receipt_id):
        return jsonify({"error": "receipt is no longer editable"}), 409
    return jsonify({"ok": True})


# --- API: exports and encrypted backups -------------------------------------

def _download_name(label: str, extension: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"king-of-meal-prep-{label}-{stamp}.{extension}"


@app.route("/api/data/export.json", methods=["GET"])
@require_auth
@limiter.limit("30 per hour")
def data_export_json():
    return send_file(
        BytesIO(data_portability.portable_json_bytes()),
        mimetype="application/json",
        as_attachment=True,
        download_name=_download_name("export", "json"),
        max_age=0,
    )


@app.route("/api/data/export.csv.zip", methods=["GET"])
@require_auth
@limiter.limit("30 per hour")
def data_export_csv():
    return send_file(
        BytesIO(data_portability.portable_csv_zip_bytes()),
        mimetype="application/zip",
        as_attachment=True,
        download_name=_download_name("export-csv", "zip"),
        max_age=0,
    )


def _backup_temp_dir() -> Path:
    value = os.environ.get(
        "DB_BACKUP_DIR",
        str(Path(config.DB_PATH).parent / "backups"),
    )
    path = Path(value)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


@app.route("/api/data/backup", methods=["POST"])
@require_auth
@limiter.limit("5 per hour")
def data_backup():
    body = object_body(request)
    reject_unknown(body, {"passphrase"})
    passphrase = text(
        body.get("passphrase"),
        "passphrase",
        required=True,
        max_length=256,
        strip=False,
    )
    if len(passphrase) < 12:
        raise ValidationError(
            "must be at least 12 characters",
            field="passphrase",
        )
    with tempfile.NamedTemporaryFile(
        prefix=".king-download-",
        suffix=".kingbackup",
        dir=_backup_temp_dir(),
        delete=False,
    ) as temporary:
        path = Path(temporary.name)
    handle = None
    try:
        data_portability.create_encrypted_backup(path, passphrase)
        handle = open(path, "rb")
        response = send_file(
            handle,
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=_download_name("full-backup", "kingbackup"),
            max_age=0,
        )

        def cleanup() -> None:
            handle.close()
            try:
                path.unlink()
            except FileNotFoundError:
                pass

        response.call_on_close(cleanup)
        return response
    except (data_portability.BackupError, OSError) as exc:
        if handle is not None:
            handle.close()
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return jsonify({"error": str(exc)}), 422


@app.route("/api/data/backup/validate", methods=["POST"])
@require_auth
@limiter.limit("10 per hour")
def data_backup_validate():
    upload = request.files.get("file")
    if upload is None:
        raise ValidationError("file is required", field="file")
    passphrase = text(
        request.form.get("passphrase"),
        "passphrase",
        required=True,
        max_length=256,
        strip=False,
    )
    path = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=".king-validate-",
            suffix=".kingbackup",
            dir=_backup_temp_dir(),
            delete=False,
        ) as temporary:
            path = Path(temporary.name)
            total = 0
            while True:
                chunk = upload.stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > BACKUP_UPLOAD_MAX_BYTES:
                    raise RequestEntityTooLarge()
                temporary.write(chunk)
        report = data_portability.validate_encrypted_backup(path, passphrase)
    except (data_portability.BackupError, OSError) as exc:
        return jsonify({"error": str(exc)}), 422
    finally:
        if path is not None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
    return jsonify({
        "ok": True,
        "schema_version": report["schema_version"],
        "quick_check": report["quick_check"],
        "foreign_key_errors": report["foreign_key_errors"],
        "contains_app_env": report["contains_app_env"],
        "database_bytes": report["database_bytes"],
        "manifest": report["manifest"],
    })


@app.route("/api/setup/finish", methods=["POST"])
@require_auth
def finish_setup():
    """Validates required fields then marks setup complete."""
    profile = db.get_user_profile()
    required = ("weight_kg", "height_cm", "age_years", "sex",
                "activity_level", "goal")
    missing = [k for k in required if profile.get(k) is None]
    if missing:
        return jsonify({"error": "incomplete", "missing": missing}), 400
    prefs = db.get_preferences()
    if not prefs.get("equipment"):
        return jsonify({"error": "incomplete", "missing": ["equipment"]}), 400
    db.mark_setup_completed()
    return jsonify({"ok": True})
