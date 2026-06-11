"""LLM response cache.

Persists the raw LLM response (and the parsed JSON) per article slug
and per model. Before calling the provider, the lane worker checks the
cache and reuses a stored response if:

  * the cache file exists for the slug
  * the model name matches (a different model = different output, don't reuse)
  * the prompt hash matches (a different prompt = different output, don't reuse)

The cache is purely a write-through: every successful provider.call()
writes its response to disk *before* the lane attempts the DB write.
That way, if the DB write fails or is silently dropped (as the v2
loader bug did), a retry can reuse the cached response and skip the
LLM call entirely.

Cost rationale: a single OpenCode Zen free call costs the user
nothing, but a single deepseek-v4-flash call costs ~$0.0003 and a
single MiniMax M2.7 Coding Plan call costs ~$0.30/M input + $1.20/M
output. For a 50K-char input article, that's a few cents per call.
The 627 wasted runs cost roughly $5-50 total (depending on which
lanes handled them). With this cache, the re-run is free.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any

from lane_config import SUPPORT_DIR

CACHE_DIR = SUPPORT_DIR / "llm_cache"


def _cache_path(slug: str) -> Path:
    # Sanitize slug to be filesystem-safe. Slugs are already
    # [A-Za-z0-9_-], but a defense-in-depth replace doesn't hurt.
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in slug)
    return CACHE_DIR / f"{safe}.json"


def _prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]


def save(slug: str, model: str, prompt: str, response_text: str,
         data: dict[str, Any] | None = None) -> None:
    """Cache the raw response and (optionally) the parsed JSON.

    Safe to call multiple times: each save overwrites the previous.
    """
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    payload = {
        "model": model,
        "prompt_hash": _prompt_hash(prompt),
        "response_text": response_text,
        "data": data,
        "saved_at": time.time(),
    }
    try:
        _cache_path(slug).write_text(
            json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        # Cache write failure must never block the lane. The lane
        # will still have the parsed data in memory; the next run
        # just has to re-call the LLM.
        pass


def has_guide_content(data: Any) -> bool:
    """True when cached model data contains publishable guide rows.

    A few MiniMax runs returned only ``destination`` metadata. Those
    rows are valid JSON but useless for guide-v2: they write a
    destination shell and then fail verification with 0 content rows.
    Treat them as cache misses so retries ask the model again.
    """
    if not isinstance(data, dict):
        return False
    prose = data.get("prose_sections") or []
    notes = data.get("practical_notes") or []
    facts = data.get("practical_facts") or {}
    listings = data.get("featured_listings") or {}
    content_sections = data.get("content_sections") or []
    if isinstance(prose, list) and prose:
        return True
    if isinstance(notes, list) and notes:
        return True
    if isinstance(content_sections, list) and content_sections:
        return True
    if isinstance(listings, dict) and any(isinstance(v, list) and v for v in listings.values()):
        return True
    if isinstance(facts, dict) and any(v for v in facts.values()):
        return True
    return False


def load(slug: str, model: str, prompt: str) -> dict[str, Any] | None:
    """Return a cached response dict (with keys response_text and
    data) or None if no valid cache entry exists.

    A cache hit requires:
      * file exists
      * model matches
      * prompt hash matches
      * response_text is non-empty
    """
    p = _cache_path(slug)
    if not p.exists():
        return None
    try:
        payload = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("model") != model:
        return None
    if payload.get("prompt_hash") != _prompt_hash(prompt):
        return None
    response_text = payload.get("response_text")
    if not response_text or not isinstance(response_text, str):
        return None
    if not has_guide_content(payload.get("data")):
        return None
    return payload


def stats() -> dict[str, int]:
    """Return a small dict describing the cache state. Used by the
    dashboard to show 'N cached responses' so the user can confirm
    that the cache is working.
    """
    if not CACHE_DIR.exists():
        return {"files": 0, "total_bytes": 0}
    files = list(CACHE_DIR.glob("*.json"))
    total = sum(p.stat().st_size for p in files)
    return {"files": len(files), "total_bytes": total}


def clear() -> int:
    """Delete all cached responses. Returns the count deleted."""
    if not CACHE_DIR.exists():
        return 0
    n = 0
    for p in CACHE_DIR.glob("*.json"):
        try:
            p.unlink()
            n += 1
        except FileNotFoundError:
            pass
    return n
