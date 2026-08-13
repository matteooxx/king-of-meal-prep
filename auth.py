"""Auth: bcrypt + Flask session, forgot-password email, env-marker write-back.

Cloned from recsbot-ui. Strings translated to English (this app is English-first
per design doc), and the persisted-hash path goes through settings.persist_env()
not settings.persist().
"""
from __future__ import annotations

import logging
import hashlib
import secrets
import threading
from datetime import datetime, timedelta
from functools import wraps

import bcrypt
from flask import Blueprint, jsonify, request, send_from_directory, session

import config
import db
import settings as app_settings
from extensions import limiter
from notifications import send_password_reset_email
from validation import ValidationError, http_url, object_body, reject_unknown, text

log = logging.getLogger("king-of-meal-prep")
auth_bp = Blueprint("auth", __name__)

current_pass_hash = [config.ADMIN_PASS_HASH]
RESET_TOKEN_TTL_MIN = 30
reset_tokens_lock = threading.Lock()


def _persist_admin_hash(new_hash: str) -> bool:
    return app_settings.persist_env({"ADMIN_PASS_HASH": new_hash})


def _purge_expired_tokens() -> None:
    now = datetime.utcnow()
    with reset_tokens_lock:
        db._conn().execute(
            "DELETE FROM reset_tokens WHERE expires_at < ? "
            "OR (used_at IS NOT NULL AND used_at < ?)",
            (
                now.isoformat(),
                (now - timedelta(days=1)).isoformat(),
            ),
        )


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def check_password(password: str, hashed: str) -> bool:
    if not hashed:
        return False
    if hashed.startswith(("$2b$", "$2a$")):
        try:
            return bcrypt.checkpw(password.encode(), hashed.encode())
        except Exception:
            return False
    return False


def check_auth() -> bool:
    if not session.get("authenticated"):
        return False
    # Cookie revocation: reject sessions issued before the current epoch.
    # Bumped on logout / password change / password reset (see _bump_auth_epoch).
    sess_epoch = int(session.get("auth_epoch") or 0)
    if sess_epoch < _current_auth_epoch():
        session.clear()
        return False
    last = session.get("last_activity")
    now = datetime.utcnow()
    if last:
        elapsed = (now - datetime.fromisoformat(last)).total_seconds()
        if elapsed > config.SESSION_INACTIVITY_HOURS * 3600:
            session.clear()
            return False
        if elapsed < 60:
            return True
    session["last_activity"] = now.isoformat()
    return True


def _bump_auth_epoch() -> int:
    """Advance the global auth epoch. Every session cookie carries the epoch
    at issuance; check_auth() rejects ones that don't match.
    Effectively a server-side revocation list of size 1."""
    cur = int(app_settings.kv_get("auth_epoch") or 1)
    new = cur + 1
    app_settings.kv_set("auth_epoch", new, is_default=False)
    return new


def _current_auth_epoch() -> int:
    return int(app_settings.kv_get("auth_epoch") or 1)


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not check_auth():
            return jsonify({"error": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


@auth_bp.route("/api/login", methods=["POST"])
@limiter.limit("10 per minute")
def login():
    data = object_body(request)
    reject_unknown(data, {"username", "password"})
    username = text(
        data.get("username"), "username", required=True, max_length=128
    )
    password = text(
        data.get("password"), "password", required=True,
        max_length=1024, strip=False,
    )
    if username != config.ADMIN_USER or not check_password(password, current_pass_hash[0]):
        log.info("login_failed user=%s ip=%s", username, request.remote_addr)
        return jsonify({"error": "Invalid credentials"}), 401
    session.clear()
    session.permanent = True
    session["authenticated"] = True
    session["username"] = username
    session["last_activity"] = datetime.utcnow().isoformat()
    session["auth_epoch"] = _current_auth_epoch()
    # New per-session CSRF token. JS reads it via /api/me and echoes
    # it back in X-CSRF-Token on every state-changing request.
    session["csrf_token"] = secrets.token_urlsafe(32)
    log.info("login_ok user=%s ip=%s", username, request.remote_addr)
    return jsonify({"ok": True})


@auth_bp.route("/api/logout", methods=["POST"])
@require_auth
def logout():
    session.clear()
    return jsonify({"ok": True})


@auth_bp.route("/api/logout-all", methods=["POST"])
@require_auth
def logout_all():
    _bump_auth_epoch()
    session.clear()
    return jsonify({"ok": True})


@auth_bp.route("/api/me")
def me():
    if check_auth():
        # Issue (or echo) a CSRF token if missing. Older sessions from before
        # the CSRF deploy won't have one until they re-login; we add one here
        # so the UX doesn't break for an already-authenticated tab.
        if not session.get("csrf_token"):
            session["csrf_token"] = secrets.token_urlsafe(32)
        return jsonify({
            "authenticated": True,
            "username": session.get("username", ""),
            "csrf_token": session["csrf_token"],
        })
    return jsonify({"authenticated": False}), 401


@auth_bp.route("/api/change-password", methods=["POST"])
@require_auth
@limiter.limit("5 per minute")
def change_password():
    data = object_body(request)
    reject_unknown(data, {"current", "new"})
    cur = text(
        data.get("current"), "current", required=True,
        max_length=1024, strip=False,
    )
    new_pass = text(
        data.get("new"), "new", required=True,
        max_length=1024, strip=False,
    )
    if len(new_pass) < 8:
        return jsonify({"error": "New password must be at least 8 characters"}), 400
    if not check_password(cur, current_pass_hash[0]):
        return jsonify({"error": "Current password is incorrect"}), 401
    new_hash = bcrypt.hashpw(new_pass.encode(), bcrypt.gensalt()).decode()
    persisted = _persist_admin_hash(new_hash)
    if not persisted:
        return jsonify({"error": "Password update could not be persisted"}), 503
    current_pass_hash[0] = new_hash
    # Invalidate every session except this one. We then re-stamp THIS session
    # with the new epoch so the user isn't kicked out immediately.
    new_epoch = _bump_auth_epoch()
    session["auth_epoch"] = new_epoch
    return jsonify({"ok": True, "persisted": persisted})


@auth_bp.route("/api/forgot-password", methods=["POST"])
@limiter.limit("3 per hour")
def forgot_password():
    data = object_body(request)
    reject_unknown(data, set())
    ip = request.remote_addr or ""
    _purge_expired_tokens()
    owner_email = app_settings.get("OWNER_EMAIL")
    if (
        not owner_email
        or not app_settings.get("SMTP_USER")
        or not app_settings.get("SMTP_PASS")
    ):
        return jsonify({"ok": True})  # mute reason — don't reveal config
    token = secrets.token_urlsafe(32)
    expires = datetime.utcnow() + timedelta(minutes=RESET_TOKEN_TTL_MIN)
    base_value = app_settings.kv_get("public_base_url") or ""
    if not isinstance(base_value, str):
        log.error("password reset not sent: public_base_url has invalid type")
        return jsonify({"ok": True})
    base = base_value.rstrip("/")
    if not base:
        log.error("password reset not sent: public_base_url is not configured")
        return jsonify({"ok": True})
    try:
        base = http_url(base, "public_base_url", required=True, https_only=True)
    except ValidationError:
        log.error("password reset not sent: public_base_url is invalid")
        return jsonify({"ok": True})
    with reset_tokens_lock:
        db._conn().execute(
            "INSERT INTO reset_tokens "
            "(token_hash, created_at, expires_at, request_ip) VALUES (?, ?, ?, ?)",
            (
                _token_hash(token),
                datetime.utcnow().isoformat(),
                expires.isoformat(),
                ip[:128],
            ),
        )
    reset_link = f"{base}/reset-password?token={token}"
    threading.Thread(
        target=send_password_reset_email,
        args=(owner_email, reset_link, RESET_TOKEN_TTL_MIN, ip),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@auth_bp.route("/reset-password")
def reset_password_page():
    return send_from_directory("templates", "reset.html")


@auth_bp.route("/api/reset-password", methods=["POST"])
@limiter.limit("10 per hour")
def reset_password():
    data = object_body(request)
    reject_unknown(data, {"token", "new"})
    token = text(
        data.get("token"), "token", required=True, max_length=256
    )
    new_pass = text(
        data.get("new"), "new", required=True,
        max_length=1024, strip=False,
    )
    if len(new_pass) < 8:
        return jsonify({"error": "New password must be at least 8 characters"}), 400
    _purge_expired_tokens()
    token_hash = _token_hash(token)
    claimed_at = datetime.utcnow().isoformat()
    with reset_tokens_lock:
        with db.tx() as c:
            row = c.execute(
                "SELECT expires_at, used_at FROM reset_tokens WHERE token_hash = ?",
                (token_hash,),
            ).fetchone()
            if (
                not row
                or row["used_at"] is not None
                or row["expires_at"] < claimed_at
            ):
                return jsonify({"error": "Token invalid or expired"}), 401
            c.execute(
                "UPDATE reset_tokens SET used_at = ? "
                "WHERE token_hash = ? AND used_at IS NULL",
                (claimed_at, token_hash),
            )
    new_hash = bcrypt.hashpw(new_pass.encode(), bcrypt.gensalt()).decode()
    persisted = _persist_admin_hash(new_hash)
    if not persisted:
        db._conn().execute(
            "UPDATE reset_tokens SET used_at = NULL "
            "WHERE token_hash = ? AND used_at = ?",
            (token_hash, claimed_at),
        )
        return jsonify({"error": "Password update could not be persisted"}), 503
    current_pass_hash[0] = new_hash
    # Reset means "every old session is dead". Don't re-stamp the requester:
    # the requester is unauthenticated at this point (followed an email link).
    _bump_auth_epoch()
    return jsonify({"ok": True, "persisted": persisted})
