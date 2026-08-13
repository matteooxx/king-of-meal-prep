"""EN→IT translation cache.

Three-tier lookup:
  1. translations table (DB)        — instant
  2. static dict ~250 cooking terms — instant
  3. Gemini Flash (one term/call)   — ~200 ms, then cached forever

Translations don't go stale, so we never expire entries.
"""
from __future__ import annotations

import logging

from i18n.static_dict import lookup as static_lookup
from llm import call, LLMUnavailableError, RateLimitedError

import db

log = logging.getLogger("king-of-meal-prep.i18n")


def translate(en: str) -> str | None:
    """English → Italian for an ingredient name. Returns None if we can't get
    a translation (LLM disabled and not in dict). Caller (the recipe save
    endpoint, the recipe page renderer) decides what to display on miss."""
    en = (en or "").strip()
    if not en:
        return None

    # 1. DB cache
    row = db._conn().execute(
        "SELECT italian FROM translations WHERE english = ?", (en.lower(),)
    ).fetchone()
    if row and row["italian"]:
        return row["italian"]

    # 2. Static dict
    s = static_lookup(en)
    if s:
        # Cache for next time so we don't pay the lookup again
        _cache(en, s, source="static")
        return s

    # 3. Gemini Flash
    try:
        text = call(
            purpose="translate_ingredient",
            model="flash",
            prompt=(
                "Translate this single cooking ingredient name from English to "
                "Italian. Reply with ONLY the Italian translation, no quotes, "
                "no prose, no punctuation other than what's in the word.\n\n"
                f"English: {en}\nItalian:"
            ),
        )
        it = text.strip().split("\n")[0].strip().strip('"').strip("'")
        if it:
            _cache(en, it, source="gemini")
            return it
    except (LLMUnavailableError, RateLimitedError) as e:
        log.info("translate %r: LLM unavailable: %s", en, e)
    return None


def _cache(en: str, it: str, source: str) -> None:
    try:
        db._conn().execute(
            "INSERT OR REPLACE INTO translations (english, italian, source) "
            "VALUES (?, ?, ?)",
            (en.lower(), it, source),
        )
    except Exception as e:
        log.warning("translate cache write failed for %r: %s", en, e)


def translate_many(items: list[str]) -> dict[str, str]:
    """Translate many at once.

    Tier-0/1 (DB cache + static dict) hits are checked individually and
    short-circuit. Anything that misses is batched into a SINGLE Gemini Flash
    call with a JSON-array prompt, so a 15-ingredient recipe costs one HTTP
    round-trip instead of fifteen. Falls back to per-item translate() on
    JSON-shape failure.
    """
    out: dict[str, str] = {}
    misses: list[str] = []
    for en in items:
        en = (en or "").strip()
        if not en:
            continue
        if en in out:
            continue
        # Cache check
        row = db._conn().execute(
            "SELECT italian FROM translations WHERE english = ?", (en.lower(),)
        ).fetchone()
        if row and row["italian"]:
            out[en] = row["italian"]
            continue
        s = static_lookup(en)
        if s:
            _cache(en, s, source="static")
            out[en] = s
            continue
        misses.append(en)
    if not misses:
        return out

    # Batch the misses through Gemini Flash in a single call.
    try:
        import json
        prompt = (
            "Translate these cooking ingredient names from English to Italian. "
            "Reply with ONLY a JSON array of strings, same length and order as "
            "the input. Each string is the Italian translation, no quotes, no "
            "punctuation other than what's in the word.\n\n"
            "Input (JSON):\n" + json.dumps(misses, ensure_ascii=False)
        )
        text = call(purpose="translate_ingredients_batch", model="flash", prompt=prompt)
        # Tolerate ```json fences
        import re as _re
        m = _re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, _re.DOTALL)
        if m: text = m.group(1)
        try:
            arr = json.loads(text.strip())
        except json.JSONDecodeError:
            arr = []
        if isinstance(arr, list) and len(arr) == len(misses):
            for en, it in zip(misses, arr):
                if isinstance(it, str) and it.strip():
                    cleaned = it.strip().split("\n")[0].strip().strip('"').strip("'")
                    _cache(en, cleaned, source="gemini")
                    out[en] = cleaned
        else:
            # Shape mismatch — fall back to one-by-one for whatever we can salvage.
            log.info("translate_many: gemini batch shape mismatch, falling back per-item")
            for en in misses:
                t = translate(en)
                if t:
                    out[en] = t
    except (LLMUnavailableError, RateLimitedError) as e:
        log.info("translate_many: LLM unavailable: %s", e)
    return out
