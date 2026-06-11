"""State file for the multi-lane initial fill.

Stores per-article processing records in JSONL. Each row records the
article slug, page_id, size, which lane handled it, and the status.
The file is append-only and human-readable.

Backward compatible: rows without a ``lane`` field (from the legacy
``deepseek_import_state.jsonl`` format) are read and treated as the
``default`` lane. Global done/failed sets include both legacy and new
rows.

File location: ``~/Library/Application Support/Backpacker Index Manager/fill_state.jsonl``
(can be overridden via the ``FILL_STATE_PATH`` env var).
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Iterable

# Reuse the legacy path as the default so existing resume works.
DEFAULT_LEGACY = Path.home() / "Library/Application Support/Backpacker Index Manager/deepseek_import_state.jsonl"
DEFAULT_NEW = Path.home() / "Library/Application Support/Backpacker Index Manager/fill_state.jsonl"


def state_path() -> Path:
    p = os.environ.get("FILL_STATE_PATH")
    if p:
        return Path(p)
    # Prefer the new file if it has rows; fall back to legacy for resume.
    if DEFAULT_NEW.exists() and DEFAULT_NEW.stat().st_size > 0:
        return DEFAULT_NEW
    if DEFAULT_LEGACY.exists():
        return DEFAULT_LEGACY
    return DEFAULT_NEW


@dataclass
class StateRow:
    page_id: int
    slug: str
    size: int
    lane: str
    status: str  # in_progress | done | failed_attempt | failed_permanent
    at: str
    run_id: str | None = None
    error: str | None = None
    input_chars: int | None = None
    output_chars: int | None = None
    elapsed_s: float | None = None
    title: str | None = None
    db_written: bool | None = None
    db_error: str | None = None
    task: str | None = None  # task name (e.g. "guide_fill"). Older rows: None.

    def to_json(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}


_write_lock = threading.Lock()
_read_cache_lock = threading.Lock()
_read_cache_sig: tuple[int, int] | None = None
_read_cache_rows: list[dict[str, Any]] | None = None


def _invalidate_read_cache() -> None:
    global _read_cache_sig, _read_cache_rows
    with _read_cache_lock:
        _read_cache_sig = None
        _read_cache_rows = None


def append(row: StateRow) -> None:
    """Append a row to the state file (thread-safe, line-atomic)."""
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = row.to_json()
    line = json.dumps(payload, ensure_ascii=False)
    with _write_lock:
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    _invalidate_read_cache()


def append_raw(payload: dict[str, Any]) -> None:
    """Append a row that may have come from the legacy format (no lane key)."""
    p = state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with _write_lock:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    _invalidate_read_cache()


def load_all() -> list[dict[str, Any]]:
    """Read state rows, cached by file mtime/size.

    The manager dashboard, SSE pusher, watchdog, and lane status all
    ask for state frequently. Parsing the whole JSONL file for every
    request makes idle CPU climb as the file grows. The state file is
    append/rewrite only, so mtime+size is a cheap freshness check.
    """
    global _read_cache_sig, _read_cache_rows
    p = state_path()
    if not p.exists():
        return []
    try:
        st = p.stat()
    except FileNotFoundError:
        return []
    sig = (st.st_mtime_ns, st.st_size)
    with _read_cache_lock:
        if _read_cache_sig == sig and _read_cache_rows is not None:
            return _read_cache_rows
    rows: list[dict[str, Any]] = []
    with p.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    with _read_cache_lock:
        _read_cache_sig = sig
        _read_cache_rows = rows
    return rows


def global_done_slugs(rows: list[dict[str, Any]] | None = None, task: str | None = None) -> set[str]:
    """Set of slugs whose LATEST row is ``done``.

    Used by the lane worker to decide which slugs to skip. A slug
    that was once done but later downgraded (e.g. manually
    requeued, or auto-repaired by ``repair_false_dones``) is NOT
    in this set, because its latest status is no longer ``done``
    and the worker should reclaim it.

    Pass a ``task`` name to scope the count to one task; ``None``
    (default) means "all tasks".
    """
    rows = rows if rows is not None else load_all()
    if task is not None:
        rows = _filter_by_task(rows, task)
    latest = _latest_by_slug(rows)
    return {slug for slug, r in latest.items() if r.get("status") == "done"}


def lane_done_slugs(lane: str, rows: list[dict[str, Any]] | None = None) -> set[str]:
    rows = rows if rows is not None else load_all()
    return {r["slug"] for r in rows if r.get("status") == "done" and r.get("lane") == lane and "slug" in r}


def lane_claimed_slugs(lane: str, rows: list[dict[str, Any]] | None = None) -> set[str]:
    """Set of slugs currently in-flight for this lane (latest status only)."""
    rows = rows if rows is not None else load_all()
    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        slug = r.get("slug")
        if not slug or r.get("lane") != lane:
            continue
        if slug not in latest or r.get("at", "") > latest[slug].get("at", ""):
            latest[slug] = r
    now = time.time()
    return {
        slug for slug, r in latest.items()
        if r.get("status") == "in_progress"
        and now - _parse_iso(r.get("at", "")) <= STALE_IN_PROGRESS_AFTER_S
    }


def recent_activity(limit: int = 50, rows: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    rows = rows if rows is not None else load_all()
    # Last N rows, reversed (newest first)
    return list(reversed(rows[-limit:]))


def lane_stats(lane: str | None = None) -> dict[str, Any]:
    """Per-lane counts and last-article info. Used by the web dashboard.

    Counts UNIQUE slugs per status, not raw rows. An article that was
    retried three times would otherwise triple-count toward
    failed_attempt/in_progress.
    """
    rows = load_all()
    latest_by_lane_slug: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        lane_name = r.get("lane") or "default"
        slug = r.get("slug")
        if not slug:
            continue
        at = r.get("at", "")
        lane_dict = latest_by_lane_slug.setdefault(lane_name, {})
        existing = lane_dict.get(slug)
        if existing is None or at > existing.get("at", ""):
            lane_dict[slug] = r
    empty = {"done": 0, "in_progress": 0, "failed_attempt": 0, "failed_permanent": 0,
             "last_article": None, "last_at": None}
    out: dict[str, dict[str, Any]] = {}
    for lane_name, slug_map in latest_by_lane_slug.items():
        bucket = dict(empty)
        for r in slug_map.values():
            status = r.get("status")
            if status in bucket:
                bucket[status] += 1
            at = r.get("at")
            if at and (not bucket["last_at"] or at > bucket["last_at"]):
                bucket["last_article"] = r.get("slug")
                bucket["last_at"] = at
        out[lane_name] = bucket
    if lane is not None:
        return out.get(lane, dict(empty))
    return out


# In-progress rows older than this are treated as abandoned (worker died,
# container restarted, etc.) and released so another lane can re-claim
# the article. The fill loop itself takes 30-120s per article, so 10
# minutes is a generous safety margin.
STALE_IN_PROGRESS_AFTER_S = 600


def _parse_iso(at: str) -> float:
    """Return a unix epoch for an ISO-8601 'at' string, or 0 on failure."""
    if not at:
        return 0.0
    try:
        # Tolerate "...Z" and fractional seconds.
        s = at.replace("Z", "+00:00")
        from datetime import datetime
        return datetime.fromisoformat(s).timestamp()
    except Exception:
        return 0.0


def compact_state() -> tuple[int, int]:
    """Rewrite the state file keeping only the latest row per slug.

    The state file is append-only by design (cheap, durable, audit-friendly),
    but retries cause each slug to accumulate many rows. This function:

      1. Collapses the file to one row per slug (last-write-wins)
      2. Releases ``in_progress`` rows older than
         ``STALE_IN_PROGRESS_AFTER_S`` so another lane can re-claim
         the article (a worker that died or a container restart that
         interrupted the loop should not block the article forever)

    Returns ``(duplicates_removed, stale_in_progress_released)``.
    """
    import json as _json
    p = state_path()
    if not p.exists():
        return (0, 0)
    rows = load_all()
    by_slug: dict[str, dict[str, Any]] = {}
    for r in rows:
        slug = r.get("slug")
        if not slug:
            continue
        at = r.get("at", "")
        if slug not in by_slug or at > by_slug[slug].get("at", ""):
            by_slug[slug] = r

    now = time.time()
    kept: list[dict[str, Any]] = []
    stale_released = 0
    for r in by_slug.values():
        if r.get("status") == "in_progress":
            age = now - _parse_iso(r.get("at", ""))
            if age > STALE_IN_PROGRESS_AFTER_S:
                stale_released += 1
                # Drop entirely — the article is fair game again.
                continue
        kept.append(r)
    kept.sort(key=lambda r: r.get("at", ""))

    with _write_lock:
        p.write_text(
            "".join(_json.dumps(r, ensure_ascii=False) + "\n" for r in kept),
            encoding="utf-8",
        )
    _invalidate_read_cache()
    duplicates_removed = len(rows) - len(by_slug)
    return (duplicates_removed, stale_released)


def repair_false_dones(force: bool = False) -> dict[str, int]:
    """Downgrade 'done' rows whose DB has 0 v2 content.

    The v2 loader bug (in ``deepseek_importer.insert_dynamic_sql``)
    used to silently drop every ``destination_content_sections`` row
    because the ``setdefault("body", ...)`` ran AFTER the column
    filtering had already removed the model's ``summary`` field. The
    LLM call paid for, the response was non-empty, the lane marked
    the article as ``done`` — and the DB sat empty.

    This function scans the state file, asks the staging DB for the
    actual content counts, and rewrites any ``done`` row whose
    destination has 0 prose_sections AND 0 practical_notes (and 0
    payment_methods and 0 safety_items) as ``failed_attempt``. After
    the rewrite, the dispatch loop will re-claim these articles
    and re-process them — using the LLM response cache so the
    re-run costs nothing.

    Returns ``{"repaired": N, "scanned": M}``. Idempotent: a
    repaired row already at status ``failed_attempt`` is not
    touched. Safe to call from startup.
    """
    import os as _os
    import subprocess
    p = state_path()
    if not p.exists():
        return {"repaired": 0, "scanned": 0}
    rows = load_all()
    by_slug: dict[str, dict[str, Any]] = {}
    for r in rows:
        slug = r.get("slug")
        if not slug:
            continue
        at = r.get("at", "")
        if slug not in by_slug or at > by_slug[slug].get("at", ""):
            by_slug[slug] = r
    # Candidates: latest row per slug is 'done' AND db_written was True
    # (so we know the lane thought it was a real success).
    candidates = [
        (slug, r) for slug, r in by_slug.items()
        if r.get("status") == "done" and (r.get("db_written") or r.get("db_error") == "empty")
    ]
    if not candidates:
        return {"repaired": 0, "scanned": 0}
    # Build one big VALUES list and one query
    env = _os.environ.copy()
    env.setdefault("PGPASSWORD", "backpacker")
    val_rows = []
    for slug, r in candidates:
        safe = slug.replace("'", "''")
        val_rows.append(f"('{safe}')")
    # Postgres VALUES limit is around 16635/2 = 8000 rows per query.
    # Batch into chunks of 500 to be safe.
    empty_slugs: set[str] = set()
    BATCH = 500
    for i in range(0, len(val_rows), BATCH):
        batch = val_rows[i:i + BATCH]
        in_list = ", ".join(batch)
        # An article is "really empty" only when ALL of these are
        # zero: prose_sections, content_sections, practical_notes,
        # payment_methods, safety_items, AND featured_listings. The
        # last one is critical: a thin destination like Iconha has
        # 3 waterfall listings but no prose or notes. Without this
        # the repair incorrectly downgrades a valid done row.
        q = f"""
        WITH slugs(slug) AS (VALUES {in_list})
        SELECT s.slug FROM slugs s
        WHERE EXISTS (SELECT 1 FROM destinations d WHERE d.slug = s.slug)
          AND NOT EXISTS (SELECT 1 FROM destination_prose_sections p
                            JOIN destinations d ON d.id = p.destination_id
                            WHERE d.slug = s.slug)
          AND NOT EXISTS (SELECT 1 FROM destination_content_sections c
                            JOIN destinations d ON d.id = c.destination_id
                            WHERE d.slug = s.slug)
          AND NOT EXISTS (SELECT 1 FROM destination_practical_notes n
                            JOIN destinations d ON d.id = n.destination_id
                            WHERE d.slug = s.slug)
          AND NOT EXISTS (SELECT 1 FROM destination_payment_methods m
                            JOIN destinations d ON d.id = m.destination_id
                            WHERE d.slug = s.slug)
          AND NOT EXISTS (SELECT 1 FROM destination_safety_items si
                            JOIN destinations d ON d.id = si.destination_id
                            WHERE d.slug = s.slug)
          AND NOT EXISTS (SELECT 1 FROM destination_featured_listings fl
                            JOIN destinations d ON d.id = fl.destination_id
                            WHERE d.slug = s.slug);
        """
        try:
            r = subprocess.run(
                ["psql", "-h", "backpacker-index-db", "-U", "backpacker",
                 "-d", "backpacker_index", "-tA", "-F", "|", "-c", q],
                env=env, capture_output=True, text=True, timeout=60,
            )
            if r.returncode != 0:
                # DB unreachable; skip this batch
                continue
            for line in r.stdout.strip().split("\n"):
                if line:
                    empty_slugs.add(line)
        except Exception:
            continue

    # Also catch old-format articles: have content_sections (v2) but
    # were processed before the unified prompt added prose_sections
    # and featured_listings. These are thin — they have some content
    # but miss the rich guide data the unified prompt produces.
    # Only flag if NEITHER prose_sections NOR featured_listings
    # has rows (a destination with either is OK for the public page).
    old_format_slugs: set[str] = set()
    for i in range(0, len(val_rows), BATCH):
        batch = val_rows[i:i + BATCH]
        in_list = ", ".join(batch)
        q = f"""
        WITH slugs(slug) AS (VALUES {in_list})
        SELECT s.slug
        FROM slugs s
        WHERE EXISTS (SELECT 1 FROM destinations d WHERE d.slug = s.slug)
          AND EXISTS (SELECT 1 FROM destination_content_sections c
                        JOIN destinations d ON d.id = c.destination_id
                        WHERE d.slug = s.slug)
          AND NOT EXISTS (SELECT 1 FROM destination_prose_sections p
                            JOIN destinations d ON d.id = p.destination_id
                            WHERE d.slug = s.slug)
          AND NOT EXISTS (SELECT 1 FROM destination_featured_listings fl
                            JOIN destinations d ON d.id = fl.destination_id
                            WHERE d.slug = s.slug);
        """
        try:
            r = subprocess.run(
                ["psql", "-h", "backpacker-index-db", "-U", "backpacker",
                 "-d", "backpacker_index", "-tA", "-c", q],
                env=env, capture_output=True, text=True, timeout=60,
            )
            if r.returncode != 0:
                continue
            for line in r.stdout.strip().split("\n"):
                if line:
                    old_format_slugs.add(line)
        except Exception:
            continue
    if old_format_slugs:
        empty_slugs.update(old_format_slugs)

    if not empty_slugs:
        return {"repaired": 0, "scanned": len(candidates)}

    # Rewrite the state file: for each empty slug, downgrade the
    # LATEST 'done' row to 'failed_attempt' with a clear error.
    # Older rows are left alone (they're just history).
    repaired = 0
    seen: set[str] = set()
    new_rows: list[dict[str, Any]] = []
    # Walk rows in order; for the FIRST row we see per slug that
    # is in empty_slugs, downgrade. But we want the LATEST row
    # to be the one we downgrade, so walk in reverse.
    for r in reversed(rows):
        slug = r.get("slug")
        if slug in empty_slugs and slug not in seen and r.get("status") == "done":
            r["status"] = "failed_attempt"
            r["error"] = (
                "repaired by repair_false_dones: original 'done' row had "
                "db_written=True but 0 v2 content rows (loader silently "
                "dropped them). Retry will re-process from LLM cache."
            )
            seen.add(slug)
            repaired += 1
        new_rows.append(r)
    new_rows.reverse()
    if repaired:
        with _write_lock:
            p.write_text(
                "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in new_rows),
                encoding="utf-8",
            )
        _invalidate_read_cache()
    return {"repaired": repaired, "scanned": len(candidates)}


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + f".{int((time.time()%1)*1000):03d}Z"


# --- Task abstraction -------------------------------------------------------
#
# A "task" is a unit of work that processes slugs from a candidate
# set. State rows are tagged with a task name so multiple tasks can
# coexist in the same state file without cross-contamination. The
# unified dashboard progress sums across all tasks. New tasks can be
# added by:
#   1. Adding an entry to TASK_DEFINITIONS below.
#   2. Writing a worker that appends StateRow(task=<name>, ...) rows.
#   3. (Optional) Wiring up a per-task lane in lane_config that sets
#      the task name on every state row it writes.
#
# For now only "guide_fill" is wired up. The data model and the
# progress functions support more without further changes.

# The default task name. Older rows (with no task field) are read
# as this default so existing state files keep working.
DEFAULT_TASK = "guide_fill"


# Task metadata for the dashboard. The candidate_source key names
# where the candidate slugs for this task come from. ``llm_ready_places``
# is the JSONL file produced by the upstream filter pipeline.
TASK_DEFINITIONS: dict[str, dict[str, Any]] = {
    "guide_fill": {
        "label": "Guide fill",
        "description": "LLM-extracted guide content for each candidate destination.",
        "candidate_source": "llm_ready_places",
        "color": "#58a6ff",
    },
}


def _task_of(row: dict[str, Any]) -> str:
    """Return the task name for a state row. Missing/None means default."""
    t = row.get("task")
    return t if isinstance(t, str) and t else DEFAULT_TASK


def _filter_by_task(rows: list[dict[str, Any]], task: str | None) -> list[dict[str, Any]]:
    """Filter rows to a specific task, or pass through if task is None."""
    if task is None:
        return rows
    return [r for r in rows if _task_of(r) == task]


def _latest_by_slug(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Reduce a list of state rows to the latest row per slug."""
    latest: dict[str, dict[str, Any]] = {}
    for r in rows:
        slug = r.get("slug")
        if not slug:
            continue
        at = r.get("at", "")
        if slug not in latest or at > latest[slug].get("at", ""):
            latest[slug] = r
    return latest


def task_progress(task: str) -> dict[str, Any]:
    """Compute progress for one task against its candidate set.

    Returns a dict with these keys, all integers, in the same shape as
    the API contract the dashboard uses:

      total         candidates in this task's scope
      done          slugs whose latest row in this task is 'done'
      in_progress   slugs whose latest row in this task is 'in_progress'
                    and not stale (worker still alive)
      failed_perm   slugs whose latest row in this task is
                    'failed_permanent'
      waiting       slugs whose latest row is 'failed_attempt' AND
                    still inside the retry cooldown window
      pending       claimable work = total - done - in_progress -
                    failed_perm - waiting (anything still left to do
                    that is not currently blocked or in flight)
      pct           (done / total) * 100, rounded to 0.1

    ``pending`` here is the work that is genuinely free to claim. A
    worker that grabs a 'waiting' slug mid-cooldown would just race
    the cooldown, so we surface them separately so the user can
    see "5 slugs in cooldown, 3 ready to go".
    """
    candidates = _load_candidates()
    if task not in TASK_DEFINITIONS:
        # Unknown task: no candidates, no work. Caller should
        # already have validated the task name.
        return {
            "total": 0, "done": 0, "in_progress": 0,
            "failed_perm": 0, "waiting": 0, "pending": 0, "pct": 0.0,
        }
    candidate_slugs: set[str] = set()
    for c in candidates:
        if c.get("slug"):
            candidate_slugs.add(c["slug"])
    total = len(candidate_slugs)

    rows = _filter_by_task(load_all(), task)
    latest = _latest_by_slug(rows)

    now = time.time()
    try:
        cooldown_s = max(0.0, float(os.environ.get("FILL_RETRY_BLOCK_COOLDOWN_S", "600")))
    except ValueError:
        cooldown_s = 600.0

    done = 0
    in_progress = 0
    failed_perm = 0
    waiting = 0
    for slug, r in latest.items():
        # If the latest row is for a slug that's no longer in the
        # candidate set (e.g. the upstream filter dropped it), skip
        # it. Without this, total_done could exceed total_articles
        # and the progress bar would render > 100%.
        if candidate_slugs and slug not in candidate_slugs:
            continue
        status = r.get("status")
        if status == "done":
            done += 1
        elif status == "in_progress":
            if now - _parse_iso(r.get("at", "")) <= STALE_IN_PROGRESS_AFTER_S:
                in_progress += 1
        elif status == "failed_permanent":
            failed_perm += 1
        elif status == "failed_attempt":
            if now - _parse_iso(r.get("at", "")) <= cooldown_s:
                waiting += 1
    pending = max(0, total - done - in_progress - failed_perm - waiting)
    pct = round((done / total) * 100, 1) if total else 0.0
    return {
        "total": total,
        "done": done,
        "in_progress": in_progress,
        "failed_perm": failed_perm,
        "waiting": waiting,
        "pending": pending,
        "pct": pct,
    }


def overall_progress() -> dict[str, Any]:
    """Aggregate progress across all known tasks.

    Returns the same shape as ``task_progress`` plus a ``per_task``
    sub-dict so the dashboard can show per-task cards under a single
    unified bar. ``total`` here is the count of unique slugs in
    scope across all tasks (not the sum of per-task totals; tasks
    share a candidate set so summing double-counts).
    """
    candidates = _load_candidates()
    all_slugs: set[str] = set()
    for c in candidates:
        if c.get("slug"):
            all_slugs.add(c["slug"])
    total = len(all_slugs)

    rows = load_all()
    latest = _latest_by_slug(rows)
    now = time.time()
    try:
        cooldown_s = max(0.0, float(os.environ.get("FILL_RETRY_BLOCK_COOLDOWN_S", "600")))
    except ValueError:
        cooldown_s = 600.0

    done = 0
    in_progress = 0
    failed_perm = 0
    waiting = 0
    for slug, r in latest.items():
        if all_slugs and slug not in all_slugs:
            continue
        status = r.get("status")
        if status == "done":
            done += 1
        elif status == "in_progress":
            if now - _parse_iso(r.get("at", "")) <= STALE_IN_PROGRESS_AFTER_S:
                in_progress += 1
        elif status == "failed_permanent":
            failed_perm += 1
        elif status == "failed_attempt":
            if now - _parse_iso(r.get("at", "")) <= cooldown_s:
                waiting += 1
    pending = max(0, total - done - in_progress - failed_perm - waiting)
    pct = round((done / total) * 100, 1) if total else 0.0

    per_task: dict[str, Any] = {}
    for task_name in TASK_DEFINITIONS.keys():
        per_task[task_name] = {
            "label": TASK_DEFINITIONS[task_name].get("label", task_name),
            "description": TASK_DEFINITIONS[task_name].get("description", ""),
            "color": TASK_DEFINITIONS[task_name].get("color", "#58a6ff"),
            **task_progress(task_name),
        }
    return {
        "total": total,
        "done": done,
        "in_progress": in_progress,
        "failed_perm": failed_perm,
        "waiting": waiting,
        "pending": pending,
        "pct": pct,
        "per_task": per_task,
        "tasks_known": list(TASK_DEFINITIONS.keys()),
    }


def list_tasks() -> list[dict[str, Any]]:
    """Return the task definitions as a list (for the dashboard)."""
    return [
        {
            "name": name,
            "label": meta.get("label", name),
            "description": meta.get("description", ""),
            "color": meta.get("color", "#58a6ff"),
        }
        for name, meta in TASK_DEFINITIONS.items()
    ]


# --- Candidate set & progress computation -----------------------------------

# The destination candidate set produced by the upstream filter stages
# (filter_top_level_subtrees.py, filter_layer2_place_only.py). Cached
# after first load.
_CANDIDATE_PATH = Path(__file__).resolve().parent / "llm_ready_places.jsonl"
_candidate_cache: list[dict[str, Any]] | None = None
_candidate_size_by_slug: dict[str, int] | None = None


def _load_candidates() -> list[dict[str, Any]]:
    """Load the candidate set. Cached after the first call."""
    global _candidate_cache, _candidate_size_by_slug
    if _candidate_cache is not None:
        return _candidate_cache
    rows: list[dict[str, Any]] = []
    sizes: dict[str, int] = {}
    if _CANDIDATE_PATH.exists():
        with _CANDIDATE_PATH.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rows.append(r)
                slug = r.get("slug")
                if slug:
                    size = r.get("page_len") or r.get("size") or 0
                    try:
                        sizes[slug] = int(size)
                    except (TypeError, ValueError):
                        sizes[slug] = 0
    _candidate_cache = rows
    _candidate_size_by_slug = sizes
    return rows


def candidate_count() -> int:
    """Total number of unique destination candidates the pipeline can fill."""
    return len(_load_candidates())


def lane_size_total(min_chars: int, max_chars: int | None) -> int:
    """Count of candidates whose size falls in the given range."""
    if max_chars is None:
        max_chars = 10**9
    return sum(
        1 for s in _load_candidates()
        if min_chars <= (s.get("page_len") or s.get("size") or 0) <= max_chars
    )


def lane_progress(lane_name: str, min_chars: int, max_chars: int | None) -> dict[str, Any]:
    """Per-lane progress counts.

    Semantics:
      * total      = unique candidates in this lane's size range
      * done       = terminal-done by this lane
      * in_progress = currently claimed by this lane
      * failed     = terminal-failed (won't be retried) by this lane
      * remaining  = total minus anything globally terminal in this range
                     (done OR failed-permanent by ANY lane)
      * pct        = this lane's done / this lane's range total
    """
    if max_chars is None:
        max_chars = 10**9
    candidates = _load_candidates()
    in_range_slugs = {
        c["slug"] for c in candidates
        if c.get("slug") and min_chars <= (c.get("page_len") or c.get("size") or 0) <= max_chars
    }
    total = len(in_range_slugs)
    if total == 0:
        return {"total": 0, "done": 0, "in_progress": 0, "failed": 0, "remaining": 0, "pct": 0.0}

    state_rows = load_all()
    done_this: set[str] = set()
    in_progress_this: set[str] = set()
    failed_this: set[str] = set()
    done_any_in_range: set[str] = set()
    failed_any_in_range: set[str] = set()
    for r in state_rows:
        slug = r.get("slug")
        if not slug or slug not in in_range_slugs:
            continue
        status = r.get("status")
        lane = r.get("lane")
        if status == "done":
            done_any_in_range.add(slug)
            if lane == lane_name:
                done_this.add(slug)
        elif status == "in_progress":
            if lane == lane_name:
                in_progress_this.add(slug)
        elif status == "failed_permanent":
            failed_any_in_range.add(slug)
            if lane == lane_name:
                failed_this.add(slug)

    remaining = max(0, total - len(done_any_in_range) - len(failed_any_in_range))
    return {
        "total": total,
        "done": len(done_this),
        "in_progress": len(in_progress_this),
        "failed": len(failed_this),
        "remaining": remaining,
        "pct": round(len(done_this) / total * 100, 1),
    }


def global_progress() -> dict[str, Any]:
    """Backward-compatible alias for ``overall_progress()``.

    Kept so older callers (and the public API contract) keep working.
    The new function returns the same keys plus ``per_task`` and
    ``failed_perm``/``waiting``/``pending`` (in addition to the
    legacy ``failed_permanent``/``remaining`` aliases).
    """
    p = overall_progress()
    # Preserve the legacy key names so old API consumers don't break.
    p.setdefault("failed_permanent", p.get("failed_perm", 0))
    p.setdefault("remaining", p.get("pending", 0))
    return p


# --- Dispatch helpers --------------------------------------------------------

_global_claim_lock_path = os.environ.get(
    "FILL_DISPATCH_LOCK",
    str(Path.home() / "Library/Application Support/Backpacker Index Manager/fill_dispatch.lock"),
)


def _acquire_dispatch_lock():
    """Acquire an exclusive file lock for dispatch decisions.

    Multiple lane workers may want to claim the same article at the
    same time when their size ranges overlap. We serialise the
    read-modify-write of the state file with a global lock so the
    first worker to acquire it wins the article.
    """
    import fcntl
    os.makedirs(os.path.dirname(_global_claim_lock_path), exist_ok=True)
    fd = os.open(_global_claim_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _release_dispatch_lock(fd) -> None:
    import fcntl
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)


def global_in_progress_slugs(rows: list[dict[str, Any]] | None = None, task: str | None = None) -> set[str]:
    """Set of slugs whose latest row is currently in-progress under any lane."""
    rows = rows if rows is not None else load_all()
    if task is not None:
        rows = _filter_by_task(rows, task)
    latest = _latest_by_slug(rows)
    now = time.time()
    return {
        slug for slug, r in latest.items()
        if r.get("status") == "in_progress"
        and now - _parse_iso(r.get("at", "")) <= STALE_IN_PROGRESS_AFTER_S
    }


def global_retry_blocked_slugs(rows: list[dict[str, Any]] | None = None, task: str | None = None) -> set[str]:
    """Slugs whose latest row is a retryable failure.

    Retryable means "not done". We block only for a short cooldown so
    30 workers do not immediately dogpile the same just-failed slug, but
    failed_attempt rows must not become permanent skips.
    """
    rows = rows if rows is not None else load_all()
    if task is not None:
        rows = _filter_by_task(rows, task)
    latest = _latest_by_slug(rows)
    try:
        cooldown_s = max(0.0, float(os.environ.get("FILL_RETRY_BLOCK_COOLDOWN_S", "600")))
    except ValueError:
        cooldown_s = 600.0
    now = time.time()
    return {
        slug for slug, r in latest.items()
        if r.get("status") == "failed_attempt"
        and now - _parse_iso(r.get("at", "")) <= cooldown_s
    }
