"""Gemini API client. Single entry point for every LLM call.

Three guarantees enforced here, not in callers:
  1. NO PERSONAL DATA leaves the NAS. The `prompt` arg must not contain
     pantry items, meal log, body stats, or training tags. Caller is on the
     honor system, but a defensive contains-check on a few obvious markers
     fails-loudly if it sees a leak.
  2. Every call is logged in `llm_calls` (id, ts, model, purpose, tokens, status).
  3. Free-tier safety: a soft daily cap (configurable in settings_kv) returns
     RateLimitedError before we hit Google's hard limit.

Public surface:
    LLMUnavailableError   — no key set / app misconfigured
    RateLimitedError      — daily cap reached or upstream 429
    call(purpose, model, prompt) -> str  (Gemini's text response)
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Literal

import httpx

import db
import settings as app_settings

log = logging.getLogger("king-of-meal-prep.llm")

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

ModelClass = Literal["flash", "pro"]
MODEL_IDS = {
    "flash": "gemini-2.5-flash",
    "pro":   "gemini-2.5-pro",
}

# Daily caps (per model class). 80% of public free-tier limits as of 2026.
# Settings_kv.gemini_daily_caps overrides at runtime.
DEFAULT_DAILY_CAPS = {"flash": 1200, "pro": 40}


# A handful of obvious markers that should never appear in an LLM prompt.
# Belt-and-suspenders against accidental leakage; the caller is supposed to
# only ever pass *generic* data (URL HTML to parse, an English ingredient
# string to translate, etc.).
_LEAK_MARKERS = (
    "ADMIN_PASS_HASH=", "GEMINI_API_KEY=", "SMTP_PASS=",
    "training_kcal_delta", "rest_kcal_target",
    "pantry_item", "meal_log",
)


class LLMUnavailableError(RuntimeError):
    """No key set, or some other config issue. UI should fall back to manual."""


class RateLimitedError(RuntimeError):
    """Daily cap hit or upstream 429."""


def _check_no_leak(prompt: str) -> None:
    for m in _LEAK_MARKERS:
        if m in prompt:
            raise LLMUnavailableError(
                f"refusing to send prompt: contains marker {m!r} "
                "(personal data must not reach Gemini)"
            )


# In-process counter cache. Keyed by (UTC date, model_id). Avoids issuing a
# COUNT(*) before every LLM call (was once-per-ingredient on translation
# batches → could be 100+ queries for an active session). Reset implicitly
# when the date key changes; no stale state across days.
_today_cache: dict[tuple[str, str], int] = {}


def _today_count(model_id: str) -> int:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    key = (today, model_id)
    if key in _today_cache:
        return _today_cache[key]
    row = db._conn().execute(
        "SELECT COUNT(*) AS n FROM llm_calls WHERE model = ? AND ts LIKE ? AND status = 'ok'",
        (model_id, f"{today}%"),
    ).fetchone()
    n = row["n"] if row else 0
    _today_cache[key] = n
    return n


def _record_call(model_id: str, purpose: str, in_t: int, out_t: int, status: str) -> None:
    db._conn().execute(
        "INSERT INTO llm_calls (ts, model, purpose, input_tokens, output_tokens, status) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.utcnow().isoformat(), model_id, purpose, in_t, out_t, status),
    )
    # Bump in-process counter on success so subsequent _today_count calls
    # don't requery DB. We only count 'ok' calls against the cap.
    if status == "ok":
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = (today, model_id)
        if key in _today_cache:
            _today_cache[key] += 1


def call(purpose: str, model: ModelClass, prompt: str, *, timeout: float = 30.0) -> str:
    """Generic Gemini call. Returns the text response.

    Raises:
        LLMUnavailableError if no key is configured (UI should disable feature)
        RateLimitedError if daily cap or 429 hit (UI should suggest "try later")
    """
    key = app_settings.get("GEMINI_API_KEY")
    if not key:
        raise LLMUnavailableError("no GEMINI_API_KEY set")
    _check_no_leak(prompt)

    model_id = MODEL_IDS.get(model, MODEL_IDS["flash"])

    # Daily cap check (soft, per model)
    caps = app_settings.kv_get("gemini_daily_caps") or DEFAULT_DAILY_CAPS
    cap = int(caps.get(model, DEFAULT_DAILY_CAPS[model]))
    today = _today_count(model_id)
    if today >= cap:
        _record_call(model_id, purpose, 0, 0, "rate_limited")
        raise RateLimitedError(f"daily cap {cap} reached for {model_id}; try tomorrow")

    url = f"{GEMINI_API_BASE}/models/{model_id}:generateContent"
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.4,
            "maxOutputTokens": 2048,
        },
    }
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            r = client.post(
                url,
                headers={
                    "x-goog-api-key": key,
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except httpx.HTTPError as e:
        _record_call(model_id, purpose, 0, 0, "error")
        raise LLMUnavailableError(f"network: {e}")

    if r.status_code == 429:
        _record_call(model_id, purpose, 0, 0, "rate_limited")
        raise RateLimitedError("upstream 429 (Gemini)")
    if r.status_code != 200:
        _record_call(model_id, purpose, 0, 0, "error")
        try:
            err = r.json().get("error", {}).get("message") or r.text[:200]
        except Exception:
            err = r.text[:200]
        raise LLMUnavailableError(f"gemini {r.status_code}: {err}")

    data = r.json()
    candidates = data.get("candidates") or []
    if not candidates:
        _record_call(model_id, purpose, 0, 0, "error")
        raise LLMUnavailableError("gemini returned no candidates")

    text = ""
    for part in (candidates[0].get("content", {}).get("parts") or []):
        if "text" in part:
            text += part["text"]
    if not text.strip():
        _record_call(model_id, purpose, 0, 0, "error")
        raise LLMUnavailableError("gemini returned empty text")

    usage = data.get("usageMetadata") or {}
    _record_call(
        model_id, purpose,
        int(usage.get("promptTokenCount") or 0),
        int(usage.get("candidatesTokenCount") or 0),
        "ok",
    )
    return text


def call_json(purpose: str, model: ModelClass, prompt: str) -> dict:
    """call() that expects a JSON body. Strips ```json fences if present."""
    text = call(purpose, model, prompt)
    # Models often wrap JSON in markdown fences; tolerate both
    m = re.search(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMUnavailableError(f"gemini returned non-JSON: {e}; raw={text[:200]!r}")
