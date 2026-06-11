"""FastAPI web server for the initial fill dashboard.

Single-page UI served at /. JSON API under /api/. Server-sent events
at /events stream change notifications so the dashboard updates
without polling.

Bound to 127.0.0.1 only — never expose to the LAN. Per Gary's
direction, the API keys in the support directory are not heavily
protected, but we still do not ship a network-exposed service.
"""
from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

import fill_state
import lane_config
from lane_config import Lane
from orchestrator import Orchestrator

# Staging site (the public Backpacker Index) has two endpoints:
#   * internal URL: the manager container uses this to call the staging
#     API. It's reachable as a sibling on the flynn_mesh Docker network.
#   * public URL: what the browser opens when the user clicks an article
#     in the dashboard. It must be a hostname the user's laptop can
#     reach (e.g. http://flynn.local:8495).
STAGING_API_URL = os.environ.get("STAGING_API_URL", "http://backpacker-index-web:8080").rstrip("/")
STAGING_PUBLIC_URL = os.environ.get("STAGING_PUBLIC_URL", "http://flynn.local:8495").rstrip("/")

THIS_DIR = Path(__file__).resolve().parent
STATIC_DIR = THIS_DIR / "static"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8742

# --- Globals -----------------------------------------------------------------

app = FastAPI(title="Backpacker Index Initial Fill", version="0.1.0")
orchestrator = Orchestrator()

# Flynn app manifest. Returned at /flynn-app.json so the Flynn
# launcher can discover the app and show it in the catalog.
MANIFEST = {
    "name": "Backpacker Index Manager",
    "version": "0.1.0",
    "description": "Multi-lane pipeline that fills the Backpacker Index destinations database.",
    "icon": "map",
    "health_endpoint": "/healthz",
    "nav": {
        "label": "Backpacker Index Manager",
        "icon": "map",
        "path": "/",
    },
    "commands": [
        {"label": "Backpacker Index Manager · Run", "path": "/", "keywords": "backpacker fill dashboard"},
    ],
    "scopes_required": [],
}


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    return {"ok": True, "state": orchestrator.state}


@app.get("/flynn-app.json")
def flynn_manifest() -> dict[str, Any]:
    return MANIFEST

# In-process event bus for SSE.
_subscribers: list[asyncio.Queue] = []
_event_loop: asyncio.AbstractEventLoop | None = None

# Cached last state fingerprint so we only broadcast on change.
_last_fingerprint: str | None = None
_api_cache: dict[str, tuple[float, Any]] = {}
_start_jobs: set[str] = set()
_start_jobs_lock = threading.Lock()


def _cache_get(key: str, ttl_s: float, builder):
    """Tiny cache for expensive JSONL-backed dashboard endpoints."""
    now = time.time()
    hit = _api_cache.get(key)
    if hit is not None:
        at, value = hit
        if now - at <= ttl_s:
            return value
    value = builder()
    _api_cache[key] = (now, value)
    return value


# --- Models ------------------------------------------------------------------

class LanesIn(BaseModel):
    lanes: list[dict[str, Any]]
    # Optional map of lane-name -> api_key value. When present, the
    # server writes this to api_keys.json (NEVER lanes.json) and creates
    # a timestamped backup. When absent, the existing keys file is
    # left untouched. The dashboard sends this on every save.
    keys: dict[str, str] | None = None


class LaneToggleIn(BaseModel):
    enabled: bool


# --- Static ------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse((STATIC_DIR / "dashboard.html").read_text(encoding="utf-8"))


if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --- Status / lanes / activity ----------------------------------------------

@app.get("/api/status")
def api_status() -> dict[str, Any]:
    """Unified run status for the dashboard.

    The top-level ``progress`` key is the single source of truth for
    the unified progress bar. It has these integer fields:

        total         total slugs in scope
        done          done (terminal success)
        in_progress   in flight right now
        failed_perm   permanent failures (will not be retried)
        waiting       in retry cooldown (counted separately so the
                      user can see "5 slugs in cooldown, 3 ready")
        pending       total - done - in_progress - failed_perm - waiting
        pct           done / total * 100, rounded to 0.1

    ``per_task`` is the same shape per task; the dashboard uses it to
    render per-task cards. ``tasks_known`` lists the registered task
    names.
    """
    def build() -> dict[str, Any]:
        agg = orchestrator.aggregate_stats()
        status = orchestrator.status()
        progress = fill_state.overall_progress()
        return {
            "orchestrator": status,
            "progress": progress,
            # Backward-compat keys for the old API contract. The
            # dashboard reads the new ``progress`` block; older
            # tooling may still read these.
            "aggregate": {
                **agg,
                "total_articles": progress["total"],
                "total_remaining": progress["pending"],
            },
        }

    return _cache_get("status", 2.0, build)


@app.get("/api/tasks")
def api_tasks() -> dict[str, Any]:
    """List known tasks and their progress.

    New DB-level tasks can be added by extending
    ``fill_state.TASK_DEFINITIONS``. Each task gets a name, a
    human-readable label, an optional color, and shares the same
    candidate-set as the default task (for now). Workers tag their
    state rows with the task name; this endpoint sums up the
    per-task progress.
    """
    def build() -> dict[str, Any]:
        progress = fill_state.overall_progress()
        return {
            "tasks": fill_state.list_tasks(),
            "per_task": progress.get("per_task", {}),
        }
    return _cache_get("tasks", 2.0, build)


@app.get("/api/lanes")
def api_lanes() -> dict[str, Any]:
    """Return the live lane config plus per-lane run stats."""
    def build() -> dict[str, Any]:
        lanes = lane_config.load_lanes()
        stats = fill_state.lane_stats()
        proc_status = orchestrator.status().get("lane_processes", {})
        out = []
        for lane in lanes:
            bucket = stats.get(lane.name, {})
            proc = proc_status.get(lane.name, {})
            progress = fill_state.lane_progress(lane.name, lane.min_chars, lane.max_chars)
            d = lane.to_dict()
            # Redact the api_key: never return the raw value through
            # the public API. The dashboard only needs to know whether
            # the key is set and what its fingerprint is.
            raw = d.pop("api_key", None)
            d["api_key_fingerprint"] = lane.api_key_fingerprint()
            d["api_key_set"] = bool(raw)
            out.append({
                **d,
                "done_count": bucket.get("done", 0),
                "in_progress_count": bucket.get("in_progress", 0),
                "failed_attempt_count": bucket.get("failed_attempt", 0),
                "failed_permanent_count": bucket.get("failed_permanent", 0),
                "total_in_range": progress["total"],
                "remaining_in_range": progress["remaining"],
                "pct": progress["pct"],
                "last_article": bucket.get("last_article"),
                "last_at": bucket.get("last_at"),
                "pid": proc.get("pid"),
                "process_running": proc.get("running", False),
            })
        return {"lanes": out}

    return _cache_get("lanes", 2.0, build)


@app.get("/api/lanes/defaults")
def api_lanes_defaults() -> dict[str, Any]:
    """Return the default base URL for each provider, so the UI can
    show "(default: https://...)" next to the base_url field."""
    defaults: dict[str, str] = {}
    try:
        from providers.local import DEFAULT_BASE as LOCAL_BASE
        defaults["local"] = LOCAL_BASE
    except Exception:
        pass
    try:
        from providers.openrouter import BASE_URL as OR_BASE
        defaults["openrouter"] = OR_BASE
    except Exception:
        pass
    try:
        from providers.opencode_go import OPENAI_BASE as OCG_OPENAI, ANTHROPIC_BASE as OCG_ANTHRO
        defaults["opencode-go"] = OCG_OPENAI
        defaults["opencode-go-anthropic"] = OCG_ANTHRO
    except Exception:
        pass
    try:
        from deepseek_importer import DEFAULT_DEEPSEEK_URL
        defaults["deepseek-direct"] = DEFAULT_DEEPSEEK_URL
    except Exception:
        pass
    # OpenAI-compatible family: each "logical provider" points at its
    # public base URL by default. The lane's base_url field can
    # override these at any time.
    try:
        from providers.openai_compatible import known_logical_providers
        _DEFAULT_BASE_BY_PROVIDER = {
            "opencode-zen": "https://opencode.ai/zen/v1",
            "nvidia": "https://integrate.api.nvidia.com/v1",
            "minimax": "https://api.minimax.io/v1",
        }
        for name in known_logical_providers().keys():
            defaults[name] = _DEFAULT_BASE_BY_PROVIDER.get(name, "")
    except Exception:
        pass
    return {"defaults": defaults}


@app.get("/api/activity")
def api_activity(limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(int(limit), 100))
    return _cache_get(f"activity:{limit}", 2.0, lambda: {"activity": fill_state.recent_activity(limit=limit)})


@app.get("/api/logs")
def api_logs(tail: int = 500) -> PlainTextResponse:
    path = THIS_DIR / "llm_parse_activity.jsonl"
    if not path.exists():
        return PlainTextResponse("")
    try:
        with path.open("rb") as f:
            data = f.read()[-tail * 4000 :]  # rough tail
        return PlainTextResponse(data.decode("utf-8", errors="ignore"))
    except Exception as exc:
        return PlainTextResponse(f"log read error: {exc}", status_code=500)


# --- Lanes config ------------------------------------------------------------

@app.get("/api/lanes/config")
def api_lanes_config() -> dict[str, Any]:
    """Return the lane config. The raw ``api_key`` is replaced with a
    fingerprint so the dashboard can display the key shape without
    seeing the value. To set a new key the client must POST the value
    explicitly via ``/api/lanes/config`` (it is round-tripped once,
    stored, and then reflected only as the fingerprint on subsequent
    reads)."""
    out: list[dict[str, Any]] = []
    for lane in lane_config.load_lanes():
        d = lane.to_dict()
        raw = d.pop("api_key", None)
        d["api_key_fingerprint"] = lane.api_key_fingerprint()
        d["api_key_set"] = bool(raw)
        out.append(d)
    return {"lanes": out}


@app.post("/api/lanes/config/reset")
def api_lanes_config_reset(confirm: int = 0) -> dict[str, Any]:
    """Wipe lanes.json and reload the built-in defaults. Use this when
    the saved config is too broken to edit through the UI.

    SAFETY:
      * Requires ``?confirm=1`` query param so an accidental curl
        cannot trigger it. Without it the endpoint refuses.
      * NEVER touches api_keys.json. Keys survive a reset unchanged.
        After reset, load_lanes() re-attaches the keys from
        api_keys.json to the new default lanes (matched by name).
    """
    if not confirm:
        raise HTTPException(400, {
            "errors": ["reset is destructive — pass ?confirm=1 to proceed"],
            "hint": "API keys in api_keys.json will NOT be affected by reset",
        })
    from pathlib import Path
    support = Path(lane_config.SUPPORT_DIR)
    f = support / "lanes.json"
    if f.exists():
        f.unlink()
    lanes = lane_config.load_lanes()
    await_broadcast({"event": "lanes_changed"})
    return {"ok": True, "count": len(lanes), "keys_preserved": len(lane_config.load_keys())}


@app.get("/api/staging/article/{slug}")
def api_staging_article(slug: str) -> dict[str, Any]:
    """Look up a destination on the staging site and return the URL
    to its detail page.

    The dashboard calls this when a user clicks a row in the recent
    activity table. The server fetches the destination record from the
    staging API, derives the country-slugified URL, and returns it.
    The dashboard then opens that URL in a new tab.
    """
    import urllib.error
    import urllib.request
    import json as _json
    import re

    if not slug or not re.match(r"^[A-Za-z0-9_-]+$", slug):
        raise HTTPException(400, {"errors": ["invalid slug"]})

    url = f"{STAGING_API_URL}/api/destinations/{slug}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {"found": False, "slug": slug, "url": None,
                    "hint": "article not yet in the staging database"}
        raise HTTPException(502, {"errors": [f"staging API HTTP {exc.code}"]})
    except urllib.error.URLError as exc:
        raise HTTPException(502, {"errors": [f"staging API unreachable: {exc}"]})

    data = payload.get("data")
    if not data:
        return {"found": False, "slug": slug, "url": None,
                "hint": "staging returned no data"}
    country = data.get("country") or ""
    # Mirror the public site's slugify for the country segment.
    country_slug = re.sub(r"[^a-z0-9]+", "-", country.lower()).strip("-")
    # The URL the BROWSER will open uses the public hostname so the
    # user's laptop can actually load the page.
    return {
        "found": True,
        "slug": slug,
        "name": data.get("name"),
        "country": country,
        "url": f"{STAGING_PUBLIC_URL}/destinations/{country_slug}/{slug}",
    }


@app.post("/api/lanes/config")
def api_lanes_config_save(body: LanesIn) -> dict[str, Any]:
    # Map lane-name -> existing Lane, so we can preserve the api_key
    # for lanes the user didn't touch in this save. Without this,
    # touching ONE lane's key on the form would wipe all the others.
    existing_by_name: dict[str, Lane] = {
        l.name: l for l in lane_config.load_lanes()
    }
    existing_keys: dict[str, str] = {
        name: lane.api_key
        for name, lane in existing_by_name.items()
        if lane.api_key
    }
    parsed: list[Lane] = []
    for raw in body.lanes:
        try:
            # The form sends everything as strings. Coerce ints; treat
            # empty string as None for optional max_chars.
            min_chars = int(raw.get("min_chars") or 0)
            workers = int(raw.get("workers") or 1)
            priority = int(raw.get("priority") or 100)
            max_chars_raw = raw.get("max_chars")
            if max_chars_raw in (None, ""):
                max_chars = None
            else:
                max_chars = int(max_chars_raw)
            # Resolve the effective api_key for this lane.
            # Three valid request shapes:
            #   "api_key" key absent           -> preserve the existing key
            #   "api_key": null                 -> preserve the existing key
            #   "api_key": ""                   -> clear the key
            #   "api_key": "<non-empty string>" -> set to that value
            # This way the user can change ONE lane's key without
            # wiping the keys on every other lane.
            if "api_key" in raw and raw["api_key"] is not None:
                api_key_value: str | None = raw["api_key"]
            else:
                existing = existing_by_name.get(raw["name"])
                api_key_value = existing.api_key if existing else None
            parsed.append(Lane(
                name=raw["name"],
                provider=raw["provider"],
                model=raw["model"],
                api_key=api_key_value,
                min_chars=min_chars,
                max_chars=max_chars,
                workers=workers,
                priority=priority,
                enabled=bool(raw.get("enabled", True)),
                base_url=raw.get("base_url"),
            ))
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(400, {"errors": [f"bad lane: {exc}"]})
    errors = lane_config.validate_lanes(parsed)
    if errors:
        raise HTTPException(400, {"errors": errors})

    # Build the new keys map. Start from existing keys, then apply
    # any changes the request specified. The dashboard sends a
    # `keys` field that overrides — it has either the new value,
    # empty string (clear), or is omitted (preserve).
    new_keys: dict[str, str] = dict(existing_keys)
    if body.keys is not None:
        for name, val in body.keys.items():
            if val:
                new_keys[name] = val
            elif name in new_keys:
                # explicit empty string: clear this lane's key
                del new_keys[name]
    # Keep the in-memory Lane objects in sync with what we just saved
    for lane in parsed:
        lane.api_key = new_keys.get(lane.name)
    lane_config.save_lanes_and_keys(parsed, new_keys)
    await_broadcast({"event": "lanes_changed"})
    return {"ok": True, "count": len(parsed), "keys_saved": len(new_keys)}


@app.post("/api/lanes/{name}/toggle")
def api_lane_toggle(name: str, body: LaneToggleIn) -> dict[str, Any]:
    lanes = lane_config.load_lanes()
    for lane in lanes:
        if lane.name == name:
            lane.enabled = body.enabled
            lane_config.save_lanes(lanes)
            await_broadcast({"event": "lanes_changed"})
            return {"ok": True, "name": name, "enabled": body.enabled}
    raise HTTPException(404, f"no lane named {name}")


# --- Run control -------------------------------------------------------------

@app.post("/api/run/start")
def api_run_start() -> dict[str, Any]:
    # Pre-flight: any enabled lane that needs a key must have one
    # embedded. The local provider is exempt (oMLX auth is optional).
    missing: list[str] = []
    for lane in orchestrator.lanes:
        if not lane.enabled:
            continue
        if lane.api_key is None and lane.provider != "local":
            missing.append(f"{lane.name} needs an API key — paste one in its lane card")
    if missing:
        raise HTTPException(400, {"errors": missing, "hint": "add keys in the Configure tab"})

    # Run start in a thread so we do not block the request handler
    # while worker subprocesses ramp up (20 workers * 2s = ~40s).
    def run_start() -> None:
        try:
            orchestrator.start()
        finally:
            await_broadcast({"event": "run_state_changed"})

    threading.Thread(target=run_start, daemon=True).start()
    # The actual state transition is performed inside the thread.
    # We return the prior state so the dashboard can show a
    # "starting" indicator immediately; the SSE event will update
    # the pill to the real state once orchestrator.start() finishes.
    await_broadcast({"event": "run_state_changed"})
    return {"ok": True, "state": "starting", "message": "Start requested. Watch the status pill for the transition."}


@app.post("/api/lanes/{name}/start")
def api_lane_start(name: str) -> dict[str, Any]:
    if orchestrator.lane_running(name):
        return {"ok": True, "name": name, "state": orchestrator.state}
    lanes = lane_config.load_lanes()
    lane = next((l for l in lanes if l.name == name), None)
    if lane is None:
        raise HTTPException(404, f"no lane named {name!r}")
    if not lane.enabled:
        raise HTTPException(400, {"errors": [f"lane {name!r} is disabled in config; enable it first"]})
    if lane.api_key is None and lane.provider != "local":
        raise HTTPException(400, {"errors": [f"lane {name!r} has no API key set; paste one in the lane card on the Configure tab"]})
    with _start_jobs_lock:
        if name in _start_jobs:
            return {"ok": True, "name": name, "state": "starting"}
        _start_jobs.add(name)

    def run_start_lane() -> None:
        try:
            ok, err = orchestrator.start_lane(name)
            if not ok:
                print(f"[server] start_lane {name} failed: {err}")
        finally:
            with _start_jobs_lock:
                _start_jobs.discard(name)
            await_broadcast({"event": "run_state_changed"})

    threading.Thread(target=run_start_lane, daemon=True).start()
    await_broadcast({"event": "run_state_changed"})
    return {"ok": True, "name": name, "state": "starting"}


@app.post("/api/lanes/{name}/stop")
def api_lane_stop(name: str) -> dict[str, Any]:
    ok, err = orchestrator.stop_lane(name)
    await_broadcast({"event": "run_state_changed"})
    if not ok:
        raise HTTPException(400, {"errors": [err]})
    return {"ok": True, "name": name, "state": orchestrator.state}


@app.post("/api/admin/repair-false-dones")
def api_admin_repair_false_dones() -> dict[str, Any]:
    """Manually trigger the false-done repair.

    Scans the state file for 'done' rows whose destination has 0
    prose_sections AND 0 practical_notes, downgrades them to
    'failed_attempt' so the dispatch loop re-processes them. The
    re-process uses the LLM response cache so it's free.

    Run on startup automatically. Exposed as a route so the user
    can re-trigger after, e.g., fixing the loader and wanting to
    salvage the runs from the buggy era.
    """
    try:
        result = fill_state.repair_false_dones()
        await_broadcast({"event": "lanes_changed", "kind": "repair_done", **result})
        return {"ok": True, **result}
    except Exception as exc:
        raise HTTPException(500, {"errors": [f"repair failed: {exc}"]})


@app.get("/api/admin/llm-cache/stats")
def api_admin_llm_cache_stats() -> dict[str, Any]:
    """Stats about the LLM response cache (files, total bytes)."""
    import llm_cache
    return llm_cache.stats()


@app.post("/api/admin/llm-cache/clear")
def api_admin_llm_cache_clear(confirm: int = 0) -> dict[str, Any]:
    """Wipe the LLM response cache. Requires ?confirm=1.

    After this, all retries will pay for the LLM call again.
    Useful if the user wants to force a re-extraction with a
    different model.
    """
    if not confirm:
        raise HTTPException(400, {"errors": ["pass ?confirm=1 to clear the LLM cache"]})
    import llm_cache
    n = llm_cache.clear()
    return {"ok": True, "cleared": n}


@app.post("/api/run/stop")
def api_run_stop() -> dict[str, Any]:
    # Run in a thread so we do not block the event loop.
    import threading
    threading.Thread(target=orchestrator.stop, daemon=True).start()
    await_broadcast({"event": "run_state_changed"})
    return {"ok": True, "state": orchestrator.state, "message": "Stop requested. Workers are draining."}


# --- Tasks ------------------------------------------------------------------
#
# Tasks are the unit of work the dashboard shows. Each task is a
# named set of state rows (see fill_state.TASK_DEFINITIONS). Start
# and stop a task by name; the orchestrator translates that to the
# underlying lane workers. Future tasks (reclassify,
# refresh_overview) can be added without changing this endpoint —
# just register them in TASK_DEFINITIONS.

@app.post("/api/tasks/{name}/start")
def api_task_start(name: str) -> dict[str, Any]:
    import fill_state as _fs
    if name not in _fs.TASK_DEFINITIONS:
        raise HTTPException(404, f"unknown task: {name!r}")
    # Reuse the global run/start handler with task filter. The
    # orchestrator currently only knows the guide_fill task; for
    # unknown tasks we return 400 with a clear hint. When new tasks
    # get their own worker types, plumb them through here.
    if name != "guide_fill":
        raise HTTPException(400, {
            "errors": [f"task {name!r} does not have a worker registered yet"],
            "hint": "register the task in fill_state.TASK_DEFINITIONS and add a worker that tags rows with task=" + name,
        })
    # Pre-flight: enabled lanes must have keys. Same gate as
    # /api/run/start.
    missing: list[str] = []
    for lane in orchestrator.lanes:
        if not lane.enabled:
            continue
        if lane.api_key is None and lane.provider != "local":
            missing.append(f"{lane.name} needs an API key")
    if missing:
        raise HTTPException(400, {"errors": missing})
    result_holder: dict[str, Any] = {}
    def run_start() -> None:
        try:
            result_holder.update(orchestrator.start())
        finally:
            await_broadcast({"event": "run_state_changed"})
    threading.Thread(target=run_start, daemon=True).start()
    await_broadcast({"event": "run_state_changed"})
    return {
        "ok": True,
        "task": name,
        "state": "starting",
        "message": f"Start requested for task {name!r}. Watch the status pill.",
    }


@app.post("/api/tasks/{name}/stop")
def api_task_stop(name: str) -> dict[str, Any]:
    import fill_state as _fs
    if name not in _fs.TASK_DEFINITIONS:
        raise HTTPException(404, f"unknown task: {name!r}")
    # For now the orchestrator stops ALL workers (per-task stop
    # would need per-task worker tracking). Documented limitation.
    import threading
    threading.Thread(target=orchestrator.stop, daemon=True).start()
    await_broadcast({"event": "run_state_changed"})
    return {
        "ok": True,
        "task": name,
        "state": orchestrator.state,
        "message": f"Stop requested for task {name!r}. All workers are draining.",
    }


@app.get("/api/run/state")
def api_run_state() -> dict[str, Any]:
    return orchestrator.status()


# --- SSE ---------------------------------------------------------------------

@app.get("/events")
async def events(request: Request):
    q: asyncio.Queue = asyncio.Queue(maxsize=100)
    _subscribers.append(q)
    try:
        async def gen():
            # Send a hello immediately so the client knows it is connected.
            yield {"event": "hello", "data": json.dumps({"ts": time.time()})}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield {"event": payload.get("event", "message"), "data": json.dumps(payload)}
                except asyncio.TimeoutError:
                    # Heartbeat so the connection stays alive.
                    yield {"event": "heartbeat", "data": json.dumps({"ts": time.time()})}
        return EventSourceResponse(gen())
    finally:
        try:
            _subscribers.remove(q)
        except ValueError:
            pass


def await_broadcast(payload: dict[str, Any]) -> None:
    """Schedule a non-blocking broadcast to all SSE subscribers."""
    if _event_loop is None or not _event_loop.is_running():
        return
    asyncio.run_coroutine_threadsafe(_broadcast(payload), _event_loop)


async def _broadcast(payload: dict[str, Any]) -> None:
    for q in list(_subscribers):
        try:
            q.put_nowait(payload)
        except asyncio.QueueFull:
            # Slow client; drop the message. They will get a fresh
            # snapshot on the next status poll.
            pass


# --- Periodic state-change pusher --------------------------------------------

async def _pusher_loop():
    """Every 1.5s, compute a state fingerprint and broadcast on change."""
    global _last_fingerprint
    while True:
        try:
            await asyncio.sleep(1.5)
            fingerprint = _state_fingerprint()
            if fingerprint != _last_fingerprint:
                _last_fingerprint = fingerprint
                await _broadcast({"event": "state_changed", "data": fingerprint})
        except asyncio.CancelledError:
            return
        except Exception:
            # Never let the pusher die; the loop must keep running.
            await asyncio.sleep(1.5)


def _state_fingerprint() -> str:
    s = orchestrator.status()
    a = orchestrator.aggregate_stats()
    p = fill_state.overall_progress()
    return json.dumps({
        "state": s["state"],
        "done": p["done"],
        "in_progress": p["in_progress"],
        "waiting": p["waiting"],
        "failed": p["failed_perm"],
        "pending": p["pending"],
        "lanes": {name: (p2["returncode"], p2["worker_count"]) for name, p2 in s["lane_processes"].items()},
    }, sort_keys=True)


@app.on_event("startup")
async def _on_startup():
    global _event_loop
    _event_loop = asyncio.get_running_loop()
    # Compact the state file on startup so display counts reflect
    # unique articles, not raw rows accumulated across retries.
    # Also releases stale in_progress claims (worker died, container
    # restarted mid-loop) so those articles can be re-claimed.
    try:
        dups, stale = fill_state.compact_state()
        if dups or stale:
            parts = []
            if dups:
                parts.append(f"removed {dups} duplicate rows")
            if stale:
                parts.append(f"released {stale} stale in_progress claims")
            print(f"[startup] compacted state file: " + ", ".join(parts))
    except Exception as exc:
        print(f"[startup] compact_state failed: {exc}")
    # One-time: extract any api_key fields currently in lanes.json
    # into api_keys.json, then rewrite lanes.json without them.
    # After this runs (once), keys are stored in api_keys.json and
    # survive any future reset of lanes.json. Idempotent — no-op
    # once migration has run.
    try:
        migrated = lane_config.migrate_keys_into_separate_file()
        if migrated:
            print(f"[startup] moved {migrated} key(s) from lanes.json into api_keys.json (safe from reset)")
    except Exception as exc:
        print(f"[startup] migrate_keys_into_separate_file failed: {exc}")
    # Repair: scan the state file for 'done' rows whose DB has 0
    # v2 content, downgrade them to 'failed_attempt' so the dispatch
    # loop re-processes them. The v2 loader bug used to mark these
    # 'done' even when the loader silently dropped every row.
    try:
        r = fill_state.repair_false_dones()
        if r["repaired"]:
            print(f"[startup] repair_false_dones: downgraded {r['repaired']} of {r['scanned']} 'done' rows that had 0 v2 content in the DB")
    except Exception as exc:
        print(f"[startup] repair_false_dones failed: {exc}")
    # Merge any new default lanes into the user's existing lanes.json.
    # Existing lanes (matched by name) keep their custom config; only
    # default-named lanes that aren't already configured get added. This
    # way, deploying new code auto-installs new lanes without clobbering
    # the user's local overrides.
    try:
        from lane_config import load_lanes, save_lanes, default_lanes
        existing = load_lanes()
        existing_names = {l.name for l in existing}
        defaults = default_lanes()
        new_lanes = [d for d in defaults if d.name not in existing_names]
        if new_lanes:
            existing.extend(new_lanes)
            save_lanes(existing)
            print(f"[startup] merged {len(new_lanes)} new default lane(s) into lanes.json: "
                  f"{', '.join(d.name for d in new_lanes)}")
    except Exception as exc:
        print(f"[startup] merge default lanes failed: {exc}")
    asyncio.create_task(_pusher_loop())


# --- Entrypoint --------------------------------------------------------------

def run():
    import uvicorn
    host = os.environ.get("FILL_HOST", DEFAULT_HOST)
    port = int(os.environ.get("FILL_PORT", DEFAULT_PORT))
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run()
