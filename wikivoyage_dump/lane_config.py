"""Lane configuration + API key management.

Two files in the app support directory, stored in the persistent
Docker volume at ``$BACKPACKER_SUPPORT_DIR`` (default
``/var/lib/backpacker-index-manager``):

  ``lanes.json``        lane config (provider, model, size ranges, workers, ...)
                        — contains NO secrets, can be safely reset
  ``api_keys.json``     ONLY the API keys, one per lane name
                        — NEVER touched by any reset or migration
                        — auto-backed up to ``api_keys.json.bak.<ts>`` on
                          every save (rolling last 5)

Why two files? Because the user explicitly asked to never have to
re-enter keys again after a config bug. The keys live in a file that
``/api/lanes/config/reset`` literally cannot reach. Even if lanes.json
is destroyed, the keys survive. The dashboard UI is unchanged — one
password field per lane card — but the storage is split.

The ``migrate_keys_into_separate_file()`` helper below is the
one-time path used by ``fill_server._on_startup`` to extract any
``api_key`` fields already in lanes.json into api_keys.json. After
that runs, lanes.json is rewritten without any secrets.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SUPPORT_DIR = Path(os.environ.get(
    "BACKPACKER_SUPPORT_DIR",
    str(Path.home() / "Library/Application Support/Backpacker Index Manager"),
))
LANES_PATH = SUPPORT_DIR / "lanes.json"        # config only, no secrets
KEYS_PATH = SUPPORT_DIR / "api_keys.json"     # only secrets, never reset

_write_lock = threading.Lock()
_key_backup_lock = threading.Lock()


# --- API keys (separate, never-touched file) --------------------------------

def _rotate_key_backups() -> None:
    """Keep the last 5 timestamped backups of api_keys.json.
    Called automatically on every key save.
    """
    if not KEYS_PATH.exists():
        return
    with _key_backup_lock:
        ts = time.strftime("%Y%m%d-%H%M%S")
        new_backup = SUPPORT_DIR / f"api_keys.json.bak.{ts}"
        try:
            new_backup.write_bytes(KEYS_PATH.read_bytes())
            os.chmod(new_backup, 0o600)
        except OSError:
            return
        # Trim to last 5 backups. The list is sorted by filename
        # (which starts with the timestamp), so the oldest is first.
        existing = sorted(
            (p for p in SUPPORT_DIR.glob("api_keys.json.bak.*") if p.is_file()),
            key=lambda p: p.name,
        )
        # The new backup is already in the list as a glob match; dedup.
        seen: set[Path] = set()
        deduped: list[Path] = []
        for p in existing:
            if p not in seen:
                seen.add(p)
                deduped.append(p)
        for stale in deduped[:-5]:
            try:
                stale.unlink()
            except FileNotFoundError:
                pass


def load_keys() -> dict[str, str]:
    """Return ``{lane_name: api_key_value}``.

    The on-disk format is ``{"keys": {lane_name: api_key}}`` so it's
    obvious what the file is when you cat it. The wrapper gives the
    caller a flat dict.
    """
    if not KEYS_PATH.exists():
        return {}
    try:
        data = json.loads(KEYS_PATH.read_text(encoding="utf-8") or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    raw_keys = data.get("keys", data)
    if not isinstance(raw_keys, dict):
        return {}
    return {k: v for k, v in raw_keys.items() if isinstance(v, str) and v}


def save_keys(keys: dict[str, str]) -> None:
    """Write keys to api_keys.json and create a timestamped backup.

    The file is mode 0600. Callers should pass a complete dict (the
    full key store, not a partial update) — the on-disk file is
    fully rewritten each time.
    """
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"keys": keys, "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    with _write_lock:
        KEYS_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.chmod(KEYS_PATH, 0o600)
    _rotate_key_backups()


def migrate_keys_into_separate_file() -> int:
    """One-time: extract any ``api_key`` fields currently in lanes.json
    into api_keys.json, then rewrite lanes.json without them.

    Idempotent: runs only if lanes.json contains api_key fields that
    are not yet in api_keys.json. After it runs, subsequent startups
    are no-ops. Returns the number of keys migrated.
    """
    if not LANES_PATH.exists():
        return 0
    try:
        data = json.loads(LANES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    if not isinstance(data, dict):
        return 0
    raw_lanes = data.get("lanes", [])
    if not isinstance(raw_lanes, list):
        return 0
    # Extract any api_key fields.
    existing_keys = load_keys()
    migrated = 0
    lanes_changed = False
    new_lanes: list[dict[str, Any]] = []
    for lane in raw_lanes:
        if not isinstance(lane, dict):
            new_lanes.append(lane)
            continue
        key = lane.pop("api_key", None)
        # Only migrate non-empty, non-None keys. Empty/missing keys
        # stay missing — don't overwrite an existing key in the store.
        if key and lane.get("name") and lane["name"] not in existing_keys:
            existing_keys[lane["name"]] = key
            migrated += 1
            lanes_changed = True
        elif key is not None:
            # api_key was in the lane record but the keys file already
            # has a value. Just remove the redundant field from lanes.json.
            lanes_changed = True
        new_lanes.append(lane)
    if migrated > 0:
        save_keys(existing_keys)
    if lanes_changed:
        with _write_lock:
            LANES_PATH.write_text(
                json.dumps({"lanes": new_lanes}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.chmod(LANES_PATH, 0o600)
    return migrated


# --- Lanes -------------------------------------------------------------------

@dataclass
class Lane:
    name: str
    provider: str
    model: str
    api_key: str | None = None  # in-memory only; persisted in api_keys.json
    min_chars: int = 0
    max_chars: int | None = None  # None = unbounded
    workers: int = 1
    priority: int = 100
    enabled: bool = True
    base_url: str | None = None  # override the provider's default API endpoint

    def matches_size(self, size: int) -> bool:
        if size < self.min_chars:
            return False
        if self.max_chars is not None and size > self.max_chars:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None or k in ("max_chars", "api_key")}

    def api_key_fingerprint(self) -> str:
        if not self.api_key:
            return ""
        if len(self.api_key) <= 12:
            return "****"
        return f"{self.api_key[:6]}****{self.api_key[-4:]}"


def default_lanes() -> list[Lane]:
    """Ship a sane default that covers the size ranges with as many
    free / cheap lanes as the user has keys for.

    Size range strategy:
      * 0-5000      local-small  (oMLX, on the user's Mac via ZeroTier)
      * 0-∞         opencode-zen-bigpickle  (free, every size, runs in parallel)
      * 0-∞         opencode-zen-dsv4free  (free, every size, runs in parallel)
      * 0-∞         nvidia-nemotron  (free NIM, every size, runs in parallel)
      * 0-∞         minimax-coding-m2.7  (paid plan, every size, runs in parallel)
      * 5K-15K      openrouter-mid  (openrouter/free fallback)
      * 15K-∞       deepseek-big  (direct deepseek, workhorse)

    Multiple lanes covering the same size range is intentional. The
    dispatch lock prevents them from claiming the same article, so they
    effectively share the work. If one lane is failing (e.g. its
    provider is rate-limited), the lane backs off on its own and the
    others keep draining the queue. There are no explicit fallbacks;
    any working lane can pick up any unclaimed article in its size range.

    Keys are NOT included in defaults — the user pastes them in the
    Configure tab. The lane worker reads ``api_key`` directly.
    """
    return [
        # Smallest articles, Mac-local
        Lane(name="local-small", provider="local", model="Qwen3.6-27B-MLX-6bit",
             api_key=None, min_chars=0, max_chars=5000, workers=1, priority=100,
             base_url="http://localhost:8000/v1"),
        # Free OpenCode Zen: Big Pickle (stealth free model)
        Lane(name="opencode-zen-bigpickle", provider="opencode-zen", model="big-pickle",
             api_key=None, min_chars=0, max_chars=None, workers=1, priority=95,
             base_url="https://opencode.ai/zen/v1"),
        # Free OpenCode Zen: DeepSeek V4 Flash Free
        Lane(name="opencode-zen-dsv4free", provider="opencode-zen", model="deepseek-v4-flash-free",
             api_key=None, min_chars=0, max_chars=None, workers=1, priority=95,
             base_url="https://opencode.ai/zen/v1"),
        # Free NVIDIA NIM: Nemotron 3 Super (120B MoE, free)
        # 120B params but only 12B active per token — fast, modern, and
        # well within the free tier's capacity budget. Stronger at
        # structured output than the older 253B Ultra, which is
        # capacity-constrained on the free tier.
        Lane(name="nvidia-nemotron", provider="nvidia", model="nvidia/nemotron-3-super-120b-a12b",
             api_key=None, min_chars=0, max_chars=None, workers=1, priority=95,
             base_url="https://integrate.api.nvidia.com/v1"),
        # MiniMax Coding Plan (global endpoint), MiniMax-M2.7 (cheaper than M3)
        Lane(name="minimax-coding-m2.7", provider="minimax", model="MiniMax-M2.7",
             api_key=None, min_chars=0, max_chars=None, workers=1, priority=90,
             base_url="https://api.minimax.io/v1"),
        # Mid-size fallback
        Lane(name="openrouter-mid", provider="openrouter", model="openrouter/free",
             api_key=None, min_chars=5000, max_chars=15000, workers=1, priority=100),
        # Large article workhorse
        Lane(name="deepseek-big", provider="deepseek-direct", model="deepseek-v4-flash",
             api_key=None, min_chars=15000, max_chars=None, workers=1, priority=100),
    ]


def load_lanes() -> list[Lane]:
    """Load lane config from lanes.json and overlay api_keys.json.

    The keys file is the source of truth for api_key values. lanes.json
    is the source of truth for everything else (provider, model,
    size ranges, etc). If lanes.json is missing or corrupt, defaults
    are used; keys are still overlaid from the keys file.
    """
    if LANES_PATH.exists():
        try:
            data = json.loads(LANES_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            data = None
    else:
        data = None
    if not isinstance(data, dict):
        lanes = default_lanes()
    else:
        out: list[Lane] = []
        for raw in data.get("lanes", []):
            try:
                out.append(Lane(
                    name=raw["name"],
                    provider=raw["provider"],
                    model=raw["model"],
                    api_key=None,  # never read from lanes.json — use keys file
                    min_chars=int(raw.get("min_chars", 0)),
                    max_chars=raw.get("max_chars"),
                    workers=int(raw.get("workers", 1)),
                    priority=int(raw.get("priority", 100)),
                    enabled=bool(raw.get("enabled", True)),
                    base_url=raw.get("base_url"),
                ))
            except (KeyError, TypeError, ValueError):
                continue
        lanes = out or default_lanes()
    # Overlay the keys file. This is what makes reset safe — even if
    # the lane list is wiped, the keys survive in api_keys.json.
    keys = load_keys()
    if keys:
        for lane in lanes:
            if lane.name in keys:
                lane.api_key = keys[lane.name]
    return lanes


def save_lanes(lanes: list[Lane]) -> None:
    """Write lane config to lanes.json. The api_key fields are NOT
    persisted here — they live in api_keys.json. Use ``save_keys()``
    separately to persist keys.
    """
    SUPPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {"lanes": []}
    for lane in lanes:
        d = lane.to_dict()
        d.pop("api_key", None)
        payload["lanes"].append(d)
    with _write_lock:
        LANES_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.chmod(LANES_PATH, 0o600)


def save_lanes_and_keys(lanes: list[Lane], keys: dict[str, str]) -> None:
    """Atomic-ish: write both lanes.json and api_keys.json.

    Order: keys first (with backup), then lanes. If lanes.json write
    fails, the keys file is still on disk and safe.
    """
    save_keys(keys)
    save_lanes(lanes)


def validate_lanes(lanes: list[Lane]) -> list[str]:
    """Return a list of human-readable errors. Empty list = valid.

    Hard errors (block startup):
      - duplicate lane names
      - empty lane name
      - min_chars < 0
      - max_chars <= min_chars
      - workers < 1

    Soft warnings (logged, do not block):
      - enabled lane has no API key (user can paste via UI)
    """
    errors: list[str] = []
    name_counts: dict[str, int] = {}
    for lane in lanes:
        name_counts[lane.name] = name_counts.get(lane.name, 0) + 1
    duplicates = sorted(n for n, c in name_counts.items() if c > 1)
    if duplicates:
        errors.append(f"Duplicate lane names: {', '.join(duplicates)}")
    for lane in lanes:
        if not lane.name.strip():
            errors.append("A lane has an empty name")
        if lane.min_chars < 0:
            errors.append(f"{lane.name}: min_chars must be >= 0")
        if lane.max_chars is not None and lane.max_chars <= lane.min_chars:
            errors.append(f"{lane.name}: max_chars must be > min_chars")
        if lane.workers < 1:
            errors.append(f"{lane.name}: workers must be >= 1")
    return errors


def warn_missing_keys(lanes: list[Lane]) -> list[str]:
    """Return human-readable warnings for enabled lanes that have no API key."""
    warnings: list[str] = []
    for lane in lanes:
        if lane.enabled and lane.api_key is None and lane.provider != "local":
            warnings.append(f"{lane.name}: no API key configured (paste it in the lane card)")
    return warnings
