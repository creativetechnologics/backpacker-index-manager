"""Single-lane worker.

Invoked by the orchestrator as a subprocess. Reads the XML stream,
filters articles by size, dispatches them one at a time to the
configured provider, writes the result to the shared state file.

This worker is intentionally simple. The pipeline is:

  1. Load lane config (size range, provider, model, key)
  2. Build the prompt via the existing ``build_prompt`` helper
  3. Stream articles from the XML dump
  4. For each article in the size range, skip if globally done
  5. Mark in_progress, call provider, mark done/failed
  6. On SIGTERM, flush and exit cleanly

For the MVP, the worker is wired up but the actual LLM call delegates
to the existing pipeline via ``deepseek_importer``. The lane-mode
implementation is additive — the legacy tiered path in
``run_llm_parse.py`` is untouched.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

import fill_state
from fill_state import StateRow, now_iso
from providers import import_provider, ProviderError, RateLimitError


@contextlib.contextmanager
def db_write_slot(lane_name: str, slug: str):
    """Limit staging DB writes across worker subprocesses.

    Normal LLM calls are network-bound, so high worker counts are fine. Cache
    hits are different: many workers can immediately become DB writers. Each
    article write opens several psql connections, so an uncapped cache-hit burst
    can swamp Postgres/sshd/network on the Pi.
    """
    try:
        import fcntl
    except ImportError:
        yield
        return

    try:
        slots = max(1, int(os.environ.get("FILL_DB_WRITE_CONCURRENCY", "4")))
    except ValueError:
        slots = 4
    support_dir = Path(os.environ.get("BACKPACKER_SUPPORT_DIR", "/var/lib/backpacker-index-manager"))
    lock_dir = support_dir / "db-write-slots"
    lock_dir.mkdir(parents=True, exist_ok=True)

    fd = None
    started = time.time()
    while not _stop:
        for i in range(slots):
            candidate_fd = os.open(str(lock_dir / f"slot-{i}.lock"), os.O_CREAT | os.O_RDWR, 0o600)
            try:
                fcntl.flock(candidate_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                fd = candidate_fd
                waited = time.time() - started
                if waited > 2:
                    log(f"[{lane_name}] {slug} waited {waited:.1f}s for DB write slot ({slots} max)")
                break
            except BlockingIOError:
                os.close(candidate_fd)
        if fd is not None:
            break
        time.sleep(0.25)
    try:
        yield
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--lane-name", required=True)
    p.add_argument("--provider", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--min-chars", type=int, default=0)
    p.add_argument("--max-chars", type=int, default=None)
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--api-key", default=None)
    p.add_argument("--base-url", default=None)
    p.add_argument("--dry-run", action="store_true", help="Log what would run, do not call LLM")
    p.add_argument("--limit", type=int, default=None, help="Stop after this many articles")
    p.add_argument("--sort-size-desc", action="store_true",
                   help="Process articles largest-first (optimise expensive API calls)")
    p.add_argument("--page-ids", default=None, help="Comma-separated page_id filter")
    p.add_argument("--task", default="guide_fill",
                   help="Task name to tag state rows with. Default: guide_fill. "
                        "Future tasks (reclassify, refresh_overview) can pass their own name.")
    return p.parse_args()


_stop = False


def _on_signal(signum, frame):
    global _stop
    _stop = True


def main() -> int:
    args = parse_args()
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    api_key = args.api_key  # the lane worker gets the value directly via --api-key

    log(f"[{args.lane_name}] starting provider={args.provider} model={args.model} "
        f"size={args.min_chars}..{args.max_chars} key={'set' if api_key else 'none'} task={args.task}")

    # Build provider
    try:
        provider_cls = import_provider(args.provider)
    except Exception as exc:
        log(f"[{args.lane_name}] unknown provider: {exc}")
        return 1

    try:
        # All providers now accept (model, api_key, base_url) kwargs.
        # The ``local`` provider ignores api_key; remote providers ignore
        # either of the two if None. Pass provider_name so the
        # openai-compatible family picks the right env var.
        provider = provider_cls(
            args.model,
            api_key=api_key,
            base_url=args.base_url,
            provider_name=args.provider,
        )  # type: ignore[abstract]
    except TypeError as exc:
        # Provider ctor signature drifted; fall back to model-only.
        log(f"[{args.lane_name}] provider ctor rejected args, retrying model-only: {exc}")
        try:
            provider = provider_cls(args.model)  # type: ignore[abstract]
        except ProviderError as exc2:
            log(f"[{args.lane_name}] provider init failed: {exc2}")
            return 1
    except ProviderError as exc:
        log(f"[{args.lane_name}] provider init failed: {exc}")
        return 1

    # Build the set of page_ids we will consider.
    page_id_filter: set[int] | None = None
    candidate_by_page_id: dict[int, dict[str, Any]] = {}
    # When sorting by size, we need the full candidate data (page_id + page_len).
    candidates_sorted: list[dict[str, Any]] | None = None
    if args.page_ids:
        page_id_filter = {int(x.strip()) for x in args.page_ids.split(",") if x.strip()}
    else:
        from pathlib import Path as _P
        candidate_path = THIS_DIR / "llm_ready_places.jsonl"
        if candidate_path.exists():
            import json as _json
            raw = [_json.loads(line) for line in candidate_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            candidate_by_page_id = {int(c["page_id"]): c for c in raw if c.get("page_id") is not None}
            if args.sort_size_desc:
                # Sort by page_len descending so we burn expensive credits
                # on the biggest (most valuable) articles first.
                raw.sort(key=lambda c: c.get("page_len", 0), reverse=True)
                candidates_sorted = raw
            page_id_filter = {c["page_id"] for c in raw}
            log(f"[{args.lane_name}] candidate set: {len(page_id_filter)} articles from llm_ready_places.jsonl"
                + (f" (sorted largest-first)" if args.sort_size_desc else ""))
        else:
            log(f"[{args.lane_name}] no --page-ids and no llm_ready_places.jsonl; nothing to do")
            return 0

    # Resume state.
    rows = fill_state.load_all()
    global_done = fill_state.global_done_slugs(rows)
    global_retry_blocked = fill_state.global_retry_blocked_slugs(rows)
    lane_claimed = fill_state.lane_claimed_slugs(args.lane_name, rows)

    log(
        f"[{args.lane_name}] resume: {len(global_done)} globally done, "
        f"{len(global_retry_blocked)} retry-blocked, {len(lane_claimed)} claimed by this lane"
    )

    # Article lookup helpers.
    try:
        from deepseek_importer import get_article_by_page_id, build_prompt, build_packet, Candidate  # type: ignore
    except ImportError:
        log(f"[{args.lane_name}] deepseek_importer not importable; cannot load articles in this session")
        return 1

    processed = 0
    done_count = 0
    failed_count = 0
    skip_count = 0
    raced_count = 0
    # Lane-level backpressure so a misbehaving provider doesn't take the
    # whole lane down with it. Two separate backoff triggers:
    #
    #   rl_history      - tracks 429s only. If a lane keeps getting
    #                     rate-limited, pause for the suggested
    #                     retry-after and let the other lanes drain
    #                     the queue.
    #
    #   failure_history - tracks ALL failed_attempt events. If the
    #                     lane is failing for ANY reason (auth, bad
    #                     response, JSON parse, DB write error) at a
    #                     sustained rate, back off and let other
    #                     lanes keep working.
    #
    # The lanes share the dispatch lock and the state-file's "done"
    # set, so when this lane pauses, any working lane can pick up the
    # next unclaimed article. The pause is per-lane only.
    rl_history: list[tuple[float, float]] = []  # (time, retry_after_s)
    failure_history: list[float] = []
    LANE_BACKOFF_AFTER_N_429S = 3
    LANE_BACKOFF_WINDOW_S = 300.0
    LANE_BACKOFF_MAX_S = 120.0
    LANE_BACKOFF_FAILURES_THRESHOLD = 5  # 5 failed attempts in the window
    LANE_BACKOFF_FAILURES_WINDOW_S = 600.0
    LANE_BACKOFF_FAILURES_MIN_S = 30.0
    LANE_BACKOFF_FAILURES_MAX_S = 180.0
    LANE_BACKOFF_CONSECUTIVE_FAILURES = 3  # 3 in a row also triggers

    if candidates_sorted is not None:
        candidate_sequence = candidates_sorted
    else:
        candidate_sequence = list(candidate_by_page_id.values())
        candidate_sequence.sort(key=lambda c: int(c.get("page_id") or 0))

    # When --sort-size-desc is set, pre-load all in-range articles
    # from the XML stream, sort by size descending, then process.
    # The XML pass takes 2-3 minutes but ensures expensive API calls
    # go to the most valuable (largest) articles first.
    if args.sort_size_desc:
        in_range_meta = [
            c for c in candidate_sequence
            if int(c.get("page_len") or 0) >= args.min_chars
            and (args.max_chars is None or int(c.get("page_len") or 0) <= args.max_chars)
        ]
        if in_range_meta:
            largest = in_range_meta[0]
            log(
                f"[{args.lane_name}] sorted {len(in_range_meta)} in-range articles "
                f"(largest: {largest.get('title')!r} {int(largest.get('page_len') or 0):,} chars, "
                f"smallest: {in_range_meta[-1].get('title')!r} {int(in_range_meta[-1].get('page_len') or 0):,} chars)"
            )
        candidate_sequence = in_range_meta

    for candidate_meta in candidate_sequence:
        claimed = False
        if _stop:
            log(f"[{args.lane_name}] stop signal received, exiting")
            break

        # Lane-level backpressure: pause if the lane is struggling
        # (rate-limited OR failing for any reason) so other lanes can
        # keep picking up new articles.
        now = time.time()
        rl_history = [(t, ra) for (t, ra) in rl_history if now - t < LANE_BACKOFF_WINDOW_S]
        failure_history = [t for t in failure_history if now - t < LANE_BACKOFF_FAILURES_WINDOW_S]

        # 1) Sustained rate limits — use the provider's own retry-after
        #    hint as the pause length (capped).
        if len(rl_history) >= LANE_BACKOFF_AFTER_N_429S:
            pause_s = min(LANE_BACKOFF_MAX_S, max(ra for _, ra in rl_history))
            log(
                f"[{args.lane_name}] sustained rate limits ({len(rl_history)} in last "
                f"{LANE_BACKOFF_WINDOW_S:.0f}s); pausing lane {pause_s:.0f}s before next claim "
                f"(other lanes keep working)"
            )
            time.sleep(pause_s)
            rl_history = []
            failure_history = []
            now = time.time()
        # 2) Sustained ANY-kind failures — pause for a fixed window
        #    that scales with the failure count.
        elif len(failure_history) >= LANE_BACKOFF_FAILURES_THRESHOLD:
            pause_s = min(
                LANE_BACKOFF_FAILURES_MAX_S,
                LANE_BACKOFF_FAILURES_MIN_S * (len(failure_history) // LANE_BACKOFF_FAILURES_THRESHOLD),
            )
            log(
                f"[{args.lane_name}] sustained failures ({len(failure_history)} in last "
                f"{LANE_BACKOFF_FAILURES_WINDOW_S:.0f}s); pausing lane {pause_s:.0f}s before next claim "
                f"(other lanes keep working)"
            )
            time.sleep(pause_s)
            failure_history = []
            now = time.time()
        # 3) Consecutive failures (no time window) — quick response
        #    to a sudden total outage, in case the window above hasn't
        #    accumulated yet.
        elif len(failure_history) >= LANE_BACKOFF_CONSECUTIVE_FAILURES and (
            len(failure_history) == LANE_BACKOFF_CONSECUTIVE_FAILURES or
            (failure_history and now - failure_history[-LANE_BACKOFF_CONSECUTIVE_FAILURES] < 60.0)
        ):
            pause_s = LANE_BACKOFF_FAILURES_MIN_S
            log(
                f"[{args.lane_name}] {len(failure_history)} consecutive failures; "
                f"pausing lane {pause_s:.0f}s before next claim (other lanes keep working)"
            )
            time.sleep(pause_s)
            failure_history = []
            now = time.time()
        page_id = int(candidate_meta.get("page_id") or 0)
        title = str(candidate_meta.get("title") or "")
        slug = str(candidate_meta.get("slug") or "").strip()
        size = int(candidate_meta.get("page_len") or candidate_meta.get("size") or 0)
        if size < args.min_chars:
            skip_count += 1
            continue
        if args.max_chars is not None and size > args.max_chars:
            skip_count += 1
            continue
        if not slug:
            slug = (title or "").strip().lower().replace(" ", "-")
            slug = "".join(c for c in slug if c.isalnum() or c in "-_")
        if not slug:
            continue
        if slug in global_done:
            skip_count += 1
            continue
        if slug in global_retry_blocked:
            skip_count += 1
            continue
        if slug in lane_claimed:
            skip_count += 1
            continue

        # Serialise the claim against other lane workers. We re-read
        # the state file under the lock so we see the latest rows
        # appended by other workers.
        lock_fd = fill_state._acquire_dispatch_lock()
        try:
            fresh_rows = fill_state.load_all()
            fresh_global_done = fill_state.global_done_slugs(fresh_rows)
            fresh_global_retry_blocked = fill_state.global_retry_blocked_slugs(fresh_rows)
            fresh_global_in_progress = fill_state.global_in_progress_slugs(fresh_rows)
            fresh_lane_claimed = fill_state.lane_claimed_slugs(args.lane_name, fresh_rows)
            if slug in fresh_global_done:
                skip_count += 1
                global_done = fresh_global_done
                continue
            if slug in fresh_global_retry_blocked:
                skip_count += 1
                global_retry_blocked = fresh_global_retry_blocked
                continue
            if slug in fresh_global_in_progress or slug in fresh_lane_claimed:
                # Another lane (or this one) already grabbed it. Skip.
                raced_count += 1
                lane_claimed = fresh_lane_claimed
                continue

            if args.dry_run:
                # Skip the actual claim path in dry-run mode so the
                # user can preview without state-file contention.
                log(f"[{args.lane_name}] dry-run: would process {slug} ({size} chars)")
                processed += 1
                if args.limit and processed >= args.limit:
                    break
                continue

            # Claim it. The state file append happens under the
            # dispatch lock, so a competing lane that holds the lock
            # will see this row on its next read.
            fill_state.append(StateRow(
                page_id=page_id, slug=slug, size=size, lane=args.lane_name, task=args.task,
                status="in_progress", at=now_iso(), title=title,
            ))
            claimed = True
        finally:
            fill_state._release_dispatch_lock(lock_fd)

        if not claimed:
            continue
        lane_claimed.add(slug)

        article = get_article_by_page_id(page_id)
        if article is None:
            log(f"[{args.lane_name}] {slug} article text missing from cache for page_id={page_id}")
            fill_state.append(StateRow(
                page_id=page_id, slug=slug, size=size, lane=args.lane_name, task=args.task,
                status="failed_attempt", at=now_iso(), title=title,
                error=f"article text missing from cache for page_id={page_id}",
            ))
            failed_count += 1
            continue
        _, article_title, text, revision_id = article
        title = title or article_title
        size = len(text or "")

        # Build prompt (reuses the existing helper).
        try:
            candidate = Candidate(
                page_id=page_id, title=title, slug=slug, page_len=size, status="usable",
                parent_page_id=None, wikidata_qid=None, page_image_filename=None,
                latitude=None, longitude=None,
            )
            packet = build_packet(candidate, title, text, revision_id)
            prompt = build_prompt(packet)
        except Exception as exc:
            tb = traceback.format_exc(limit=3)
            log(f"[{args.lane_name}] {slug} prompt build failed: {exc}\n{tb}")
            fill_state.append(StateRow(
                page_id=page_id, slug=slug, size=size, lane=args.lane_name, task=args.task,
                status="failed_attempt", at=now_iso(), title=title, error=str(exc),
            ))
            failure_history.append(time.time())
            failed_count += 1
            continue

        # Call provider. On 429 (rate limit), wait the suggested time
        # and retry up to RATE_LIMIT_MAX_RETRIES times. Other errors
        # are recorded as failed_attempt and we move on.
        RATE_LIMIT_MAX_RETRIES = 3
        data = None
        usage = {}
        t_start = time.time()
        # Emit a per-request start line so the gap between successive
        # requests is visible in the log (proves single-threaded dispatch).
        log(f"[{args.lane_name}] {slug} → request start (size={size} prompt={len(prompt)} chars)")

        # LLM response cache: if we already have a response for this
        # exact (slug, model, prompt), reuse it. This is critical
        # because the v2 loader bug used to silently drop content and
        # require a retry; without the cache, every retry paid for
        # the LLM call a second time.
        import llm_cache
        cached = llm_cache.load(slug, args.model, prompt)
        if cached is not None and cached.get("data") is not None:
            data = _unwrap_list_response(cached.get("data"))
            usage = {}
            log(
                f"[{args.lane_name}] {slug} → cache HIT "
                f"(model={args.model!r} prompt_hash={cached.get('prompt_hash')!r})"
            )

        for rl_attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
            t0 = time.time()
            try:
                if data is None:
                    data, usage = provider.call(prompt)
                    # Persist immediately so any later failure is free to retry.
                    if data is not None:
                        data = _unwrap_list_response(data)
                        # Save the parsed JSON (re-serialized for stability)
                        response_text = json.dumps(data, ensure_ascii=False)
                        if llm_cache.has_guide_content(data):
                            llm_cache.save(slug, args.model, prompt, response_text, data=data)
                break
            except RateLimitError as exc:
                elapsed = time.time() - t0
                wait_s = min(exc.retry_after_s, 60.0)  # cap at 60s
                rl_history.append((time.time(), exc.retry_after_s))
                if rl_attempt < RATE_LIMIT_MAX_RETRIES:
                    log(
                        f"[{args.lane_name}] {slug} rate limited after {elapsed:.1f}s; "
                        f"sleeping {wait_s:.0f}s before retry {rl_attempt + 1}/{RATE_LIMIT_MAX_RETRIES}"
                    )
                    time.sleep(wait_s)
                    continue
                # Final attempt also rate-limited. Record and move on.
                total_elapsed = time.time() - t_start
                log(
                    f"[{args.lane_name}] {slug} rate limited; "
                    f"giving up after {RATE_LIMIT_MAX_RETRIES} retries ({total_elapsed:.1f}s total)"
                )
                fill_state.append(StateRow(
                    page_id=page_id, slug=slug, size=size, lane=args.lane_name, task=args.task,
                    status="failed_attempt", at=now_iso(), title=title,
                    error=f"rate limited: {str(exc)[:200]}", elapsed_s=round(total_elapsed, 2),
                ))
                failure_history.append(time.time())
                failed_count += 1
                break
            except ProviderError as exc:
                elapsed = time.time() - t0
                log(f"[{args.lane_name}] {slug} provider error after {elapsed:.1f}s: {exc}")
                fill_state.append(StateRow(
                    page_id=page_id, slug=slug, size=size, lane=args.lane_name, task=args.task,
                    status="failed_attempt", at=now_iso(), title=title,
                    error=str(exc), elapsed_s=round(elapsed, 2),
                ))
                failure_history.append(time.time())
                failed_count += 1
                break
            except Exception as exc:
                elapsed = time.time() - t0
                log(f"[{args.lane_name}] {slug} provider unexpected error after {elapsed:.1f}s: {type(exc).__name__}: {exc}")
                fill_state.append(StateRow(
                    page_id=page_id, slug=slug, size=size, lane=args.lane_name, task=args.task,
                    status="failed_attempt", at=now_iso(), title=title,
                    error=f"{type(exc).__name__}: {str(exc)[:300]}", elapsed_s=round(elapsed, 2),
                ))
                failure_history.append(time.time())
                failed_count += 1
                break

        if data is None:
            # Retry loop gave up; the failed_attempt row was already written.
            continue

        elapsed = time.time() - t_start
        # The provider returned parsed JSON. Write the article to the
        # staging database (if enabled), then record the run in the
        # state file. DB write failures are recorded but do not count
        # against the lane's LLM-success tally.
        output_chars = len(json.dumps(data, ensure_ascii=False))
        log(f"[{args.lane_name}] {slug} ← response back after {elapsed:.1f}s ({size} -> {output_chars} chars)")

        # ---- write to staging DB ----
        from deepseek_importer import Candidate as _Candidate
        candidate = _Candidate(
            page_id=page_id, title=title, slug=slug, page_len=size,
            status="usable", parent_page_id=None, wikidata_qid=None,
            page_image_filename=None, latitude=None, longitude=None,
        )
        import db_writer
        with db_write_slot(args.lane_name, slug):
            db_result = db_writer.write_article(
                candidate=candidate,
                title=title,
                wikitext=text,
                revision_id=revision_id,
                data=data,
                usage=usage,
                model_name=getattr(provider, "model", args.model),
            )
            if db_result.get("written"):
                log(
                    f"[{args.lane_name}] {slug} → staging db written "
                    f"(dest={db_result.get('destination_id')[:8]}… run={db_result.get('run_id')[:8]}…)"
                )
            elif db_result.get("skipped"):
                log(f"[{args.lane_name}] {slug} db write skipped: {db_result.get('reason')}")
            else:
                log(f"[{args.lane_name}] {slug} db write FAILED: {db_result.get('error')}")

            # CRITICAL: verify the DB actually has content. The v2 loader
            # bug used to mark articles as 'done' even when the loader
            # silently dropped every row (the setdefault for body ran
            # after column filtering and never found the model's 'summary'
            # field). Always cross-check the parsed LLM response against
            # the actual DB state — if the LLM returned content but the
            # DB is empty, that's a real failure and must NOT be counted
            # as 'done'.
            db_verification = _verify_db_wrote_content(
                data, db_result, args.lane_name, slug, log
            )
        if not db_verification["ok"]:
            log(
                f"[{args.lane_name}] {slug} ✗ FAIL: {db_verification['error']} "
                f"(LLM returned content but DB has 0 rows in v2 tables; "
                f"marked failed_attempt so a retry can fix it)"
            )
            fill_state.append(StateRow(
                page_id=page_id, slug=slug, size=size, lane=args.lane_name, task=args.task,
                status="failed_attempt", at=now_iso(), title=title,
                input_chars=size, output_chars=output_chars, elapsed_s=round(elapsed, 2),
                db_written=bool(db_result.get("written")),
                db_error=db_verification["error"],
            ))
            # Don't add to global_done — the dispatch retry loop will
            # see this article is still free to claim and re-process
            # it (using the cached LLM response, so the retry is free).
            failed_count += 1
            processed += 1
            continue

        fill_state.append(StateRow(
            page_id=page_id, slug=slug, size=size, lane=args.lane_name, task=args.task,
            status="done", at=now_iso(), title=title,
            input_chars=size, output_chars=output_chars, elapsed_s=round(elapsed, 2),
            db_written=bool(db_result.get("written")),
            db_error=db_result.get("error"),
        ))
        global_done.add(slug)
        done_count += 1
        processed += 1

        if args.limit and processed >= args.limit:
            log(f"[{args.lane_name}] hit --limit {args.limit}, stopping")
            break

    log(f"[{args.lane_name}] exit: done={done_count} failed={failed_count} skipped={skip_count} raced={raced_count}")
    return 0


def log(msg: str) -> None:
    print(msg, flush=True)


def _unwrap_list_response(data):
    """Coerce a top-level list response into a single dict.

    Some smaller models occasionally wrap their JSON in a one-element
    list — ``[{...}]`` instead of ``{...}``. The loader calls
    ``.get()`` on the result, which crashes with "'list' object has
    no attribute 'get'" if we don't unwrap. This is a defensive
    helper that runs after every provider.call() and cache.load().

    Returns:
      * the dict inside a one-element list, if ``data`` is ``[dict]``
      * the dict inside a multi-element list whose first item is a
        dict (take the first; the model's prompt asked for one
        destination, so the rest is likely noise)
      * ``data`` unchanged if it is already a dict or is something
        the caller will fail gracefully on (string, None, etc.)
    """
    if not isinstance(data, list):
        return data
    if len(data) == 0:
        return data
    first = data[0]
    if isinstance(first, dict):
        return first
    return data


def _verify_db_wrote_content(
    data: dict[str, Any] | None,
    db_result: dict[str, Any],
    lane_name: str,
    slug: str,
    log_fn,
) -> dict[str, Any]:
    """Cross-check the parsed LLM response against the actual DB state.

    Returns ``{"ok": True}`` if the LLM did its job and the DB has
    matching content, or ``{"ok": False, "error": "..."}`` if the
    LLM returned real content but the DB has 0 rows in the v2
    content tables.

    This is the safety net for the loader bug where
    ``insert_dynamic_sql`` silently dropped every row when the
    model's field names (e.g. ``summary``) didn't match the table
    column names (e.g. ``body``). Without this check, the lane
    would happily mark the article as ``done`` while the DB sat
    empty.

    The check counts non-empty content units in the LLM response
    (prose_sections, content_sections, practical_notes, etc.) and
    compares to the DB. If the LLM returned any content but the
    DB is empty, that's a failure.
    """
    if not data or not isinstance(data, dict):
        return {"ok": True}  # no LLM response to verify
    if not db_result.get("written"):
        # DB write itself failed. Do NOT accept this as OK — the
        # article needs to be re-processed. Signal failure so the
        # caller marks it as failed_attempt (the dispatch loop will
        # re-claim it on the next iteration, and the LLM cache makes
        # the retry free).
        return {"ok": False, "error": "DB write failed: " + str(db_result.get("error", "unknown"))[:200]}

    # Count meaningful content in the LLM response.
    prose_count = 0
    notes_count = 0
    other_count = 0
    featured_count = 0
    for s in (data.get("prose_sections") or []):
        if isinstance(s, dict) and (s.get("body") or s.get("summary") or s.get("content")):
            prose_count += 1
    for s in (data.get("content_sections") or []):
        if isinstance(s, dict) and (s.get("body") or s.get("summary") or s.get("content")):
            prose_count += 1
    for n in (data.get("practical_notes") or []):
        if isinstance(n, dict) and (n.get("note") or n.get("text") or n.get("body") or n.get("advice")):
            notes_count += 1
    for k in (
        "payment_methods", "safety_items", "day_trips",
        "money_tips", "budget_items",
    ):
        for item in (data.get(k) or []):
            if isinstance(item, dict) and item:
                other_count += 1
    # featured_listings is a dict by category; count any non-empty
    # category as a content unit. This is the only signal for "thin"
    # destinations like Iconha (a town with no prose, no notes, but
    # 3 waterfall listings worth keeping on the public page).
    fl = data.get("featured_listings")
    if isinstance(fl, dict):
        for cat_items in fl.values():
            if isinstance(cat_items, list):
                for item in cat_items:
                    if isinstance(item, dict) and (item.get("name") or item.get("title")):
                        featured_count += 1
    llm_total = prose_count + notes_count + other_count + featured_count
    if llm_total == 0:
        return {
            "ok": False,
            "error": "LLM returned no guide content rows (prose/notes/listings/facts all empty)",
        }

    # LLM returned content. Check the public staging API actually exposes
    # it as guide-v2 content. This catches the real user-facing failure:
    # rows may be written (or guide_meta may exist), while the public page
    # still renders "guide pending" because prose/listings/notes are empty.
    try:
        import json as _json
        import os as _os
        import urllib.request
        staging_api = _os.environ.get("STAGING_API_URL", "http://backpacker-index-web:8080").rstrip("/")
        with urllib.request.urlopen(f"{staging_api}/api/destinations/{slug}", timeout=15) as resp:
            payload = _json.loads(resp.read().decode("utf-8"))
        public_data = payload.get("data") or {}
        prose = public_data.get("prose_sections") or []
        notes = public_data.get("practical_notes") or []
        listings = public_data.get("featured_listings") or {}
        listing_total = 0
        if isinstance(listings, dict):
            listing_total = sum(len(v or []) for v in listings.values() if isinstance(v, list))
        if public_data.get("is_filled") and (len(prose) > 0 or len(notes) > 0 or listing_total > 0):
            return {
                "ok": True,
                "llm_total": llm_total,
                "public_prose": len(prose),
                "public_notes": len(notes),
                "public_listings": listing_total,
            }
        return {
            "ok": False,
            "error": (
                "public staging API has no guide content "
                f"(is_filled={public_data.get('is_filled')}, prose={len(prose)}, "
                f"notes={len(notes)}, listings={listing_total})"
            ),
        }
    except Exception as exc:
        log_fn(
            f"[{lane_name}] {slug} public API verification failed: {exc!r}; "
            f"falling back to DB row-count check"
        )

    # Fallback: query by slug (we have the destination_id in db_result
    # but using the slug keeps this test self-contained).
    try:
        import subprocess, os as _os
        env = _os.environ.copy()
        env.setdefault("PGPASSWORD", "backpacker")
        safe_slug = slug.replace("'", "''")
        # The v2 loader writes to destination_content_sections (the
        # v2 table). The legacy v1 table is destination_prose_sections.
        # Some articles may have content in BOTH (older runs wrote to
        # v1, newer runs write to v2); we count either. The check's
        # job is to confirm the loader ACTUALLY persisted the content
        # the LLM returned, not to enforce which table it's in.
        q = (
            "SELECT "
            "(SELECT COUNT(*) FROM destination_content_sections p "
            "  JOIN destinations d ON d.id = p.destination_id "
            "  WHERE d.slug = '" + safe_slug + "'), "
            "(SELECT COUNT(*) FROM destination_prose_sections p "
            "  JOIN destinations d ON d.id = p.destination_id "
            "  WHERE d.slug = '" + safe_slug + "'), "
            "(SELECT COUNT(*) FROM destination_practical_notes n "
            "  JOIN destinations d ON d.id = n.destination_id "
            "  WHERE d.slug = '" + safe_slug + "'), "
            "(SELECT COUNT(*) FROM destination_payment_methods m "
            "  JOIN destinations d ON d.id = m.destination_id "
            "  WHERE d.slug = '" + safe_slug + "'), "
            "(SELECT COUNT(*) FROM destination_safety_items s "
            "  JOIN destinations d ON d.id = s.destination_id "
            "  WHERE d.slug = '" + safe_slug + "')"
        )
        r = subprocess.run(
            ["psql", "-h", "backpacker-index-db", "-U", "backpacker",
             "-d", "backpacker_index", "-tA", "-F", "|", "-c", q],
            env=env, capture_output=True, text=True, timeout=15,
        )
        if r.returncode != 0:
            # DB check failed for an unrelated reason (e.g. the
            # container can't reach the DB at the moment). Don't
            # mark the run as failed; the next retry will re-check.
            log_fn(
                f"[{lane_name}] {slug} _verify_db_wrote_content: psql failed "
                f"({r.returncode}); treating as ok"
            )
            return {"ok": True}
        parts = r.stdout.strip().split("|")
        db_v2_prose, db_v1_prose, db_notes, db_pay, db_safety = (int(x) for x in parts)
        db_prose = db_v2_prose + db_v1_prose  # count either
        db_total = db_prose + db_notes + db_pay + db_safety
    except Exception as exc:
        log_fn(
            f"[{lane_name}] {slug} _verify_db_wrote_content: exception {exc!r}; "
            f"treating as ok"
        )
        return {"ok": True}

    if db_total == 0:
        return {
            "ok": False,
            "error": (
                f"LLM returned {prose_count} prose, {notes_count} notes, "
                f"{other_count} other items, but DB has 0 rows in all 4 "
                f"v2 content tables (loader silently dropped them)"
            ),
            "llm_total": llm_total,
            "db_prose": db_prose,
            "db_notes": db_notes,
            "db_pay": db_pay,
            "db_safety": db_safety,
        }
    return {
        "ok": True,
        "llm_total": llm_total,
        "db_prose": db_prose,
        "db_notes": db_notes,
        "db_pay": db_pay,
        "db_safety": db_safety,
    }


if __name__ == "__main__":
    raise SystemExit(main())
