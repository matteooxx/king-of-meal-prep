"""URL → structured Recipe via the recipe-scrapers library.

recipe-scrapers covers ~500 cooking sites natively. When it returns nothing
useful (unsupported site, paywall, 404, layout shift) we return None and the
caller falls back to recipes/llm.py (Gemini Flash parser).

We never raise: any exception becomes None + a log line so the caller's
error path is uniform.
"""
from __future__ import annotations

import logging
from typing import Optional, TypedDict

import httpx

log = logging.getLogger("king-of-meal-prep.scrape")


class ScrapedRecipe(TypedDict, total=False):
    name: str
    source_url: str
    image: Optional[str]
    servings: int
    total_time_min: Optional[int]
    active_time_min: Optional[int]
    ingredients: list[str]
    steps: list[str]
    cuisine: Optional[str]
    notes: Optional[str]


_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) "
                  "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17 Safari/605.1.15",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en;q=0.9, it;q=0.8",
}


def _fetch(url: str, timeout: float = 12.0) -> Optional[str]:
    """Fetch via the SSRF-hardened safe_fetch layer.

    The `timeout` arg is kept for back-compat but ignored — safe_fetch has its
    own per-hop + per-chain timeouts. Returns None on any rejection so the
    caller's "scrape failed → fall back to LLM" path stays uniform.
    """
    from recipes.safe_fetch import fetch as safe_fetch, SafeFetchError
    try:
        text, _ = safe_fetch(url)
        return text
    except SafeFetchError as e:
        log.info("scrape fetch %s rejected: %s", url, e)
        return None


def scrape(url: str) -> Optional[ScrapedRecipe]:
    """Fetch + parse. Returns None if anything goes wrong."""
    html = _fetch(url)
    if not html:
        return None
    try:
        from recipe_scrapers import scrape_html
    except ImportError:
        log.error("recipe-scrapers not installed")
        return None
    try:
        s = scrape_html(html, org_url=url)
    except Exception as e:
        log.info("recipe-scrapers parse failed for %s: %s", url, e)
        return None

    out: ScrapedRecipe = {"source_url": url}

    # Each accessor can throw ElementNotFoundInHtml / NotImplementedError.
    # We catch around each so a single missing field doesn't void the whole
    # parse — we'd rather have a name + ingredients than nothing.
    def _safe(fn, default=None):
        try:
            v = fn()
            return v if v not in (None, "", [], {}) else default
        except Exception:
            return default

    out["name"] = _safe(s.title) or "Untitled recipe"
    out["image"] = _safe(s.image)
    out["servings"] = _try_int(_safe(s.yields)) or 1
    out["total_time_min"] = _try_int(_safe(s.total_time))
    # active_time isn't always available; recipe-scrapers exposes prep_time + cook_time
    pt = _try_int(_safe(getattr(s, "prep_time", lambda: None)))
    ct = _try_int(_safe(getattr(s, "cook_time", lambda: None)))
    if pt or ct:
        out["active_time_min"] = (pt or 0) + (ct or 0)

    out["ingredients"] = _safe(s.ingredients) or []
    instr = _safe(s.instructions)
    if isinstance(instr, str):
        out["steps"] = [s.strip() for s in instr.split("\n") if s.strip()]
    elif isinstance(instr, list):
        out["steps"] = [s.strip() for s in instr if isinstance(s, str) and s.strip()]
    else:
        out["steps"] = []

    out["cuisine"] = _safe(getattr(s, "cuisine", lambda: None))
    out["notes"] = _safe(getattr(s, "description", lambda: None))

    # If we got neither ingredients nor steps, treat as failure
    if not out["ingredients"] and not out["steps"]:
        log.info("recipe-scrapers got nothing useful for %s", url)
        return None

    return out


def _try_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
