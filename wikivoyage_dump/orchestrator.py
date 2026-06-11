"""Multi-lane orchestrator.

For each enabled lane, spawn a subprocess running ``run_lane.py`` with
arguments derived from the lane config. The orchestrator:

  - tracks subprocess PIDs
  - forwards SIGTERM / SIGINT to children (then SIGKILL after timeout)
  - exposes a small status API used by the web server
  - watches the state file and reports per-lane progress
  - supports per-lane start/stop in addition to the global lifecycle

The orchestrator is intentionally a thin supervisor. The lane workers
own the actual LLM call, prompt building, and DB write logic. This
keeps the orchestrator crash-safe: a worker crash does not corrupt
orchestrator state.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import fill_state
import lane_config
from lane_config import Lane

THIS_DIR = Path(__file__).resolve().parent


def worker_start_interval_s() -> float:
    """Seconds to wait between worker subprocess starts.

    Starting many workers at once can turn cached LLM responses into a sudden
    DB-write storm. A small ramp keeps 30-worker lanes from thundering into
    Postgres/network all at once while still reaching full concurrency.
    """
    try:
        return max(0.0, float(os.environ.get("FILL_WORKER_START_INTERVAL_S", "2.0")))
    except ValueError:
        return 2.0


@dataclass
class LaneProcess:
    lane: Lane
    proc: subprocess.Popen
    started_at: float
    last_activity_at: float | None = None


class Orchestrator:
    def __init__(self):
        self.lanes: list[Lane] = lane_config.load_lanes()
        self.processes: dict[str, list[LaneProcess]] = {}
        self.state: str = "idle"  # idle | running | stopping | error
        self.started_at: float | None = None
        self.last_error: str | None = None
        self._watchdog_active: bool = False

    # -- Lifecycle: global ----------------------------------------------------

    def start(self) -> dict[str, Any]:
        """Start every enabled lane.

        Returns a small status dict so the HTTP layer can surface a
        useful message to the dashboard. Shape:

            {"ok": True, "state": ..., "message": ..., "spawned": N}

        ``state`` is the new orchestrator state after this call.
        ``message`` is a one-line human description of what changed.
        ``spawned`` is the number of worker subprocesses started.
        """
        self.lanes = lane_config.load_lanes()
        prior_state = self.state
        # Cancel any prior watchdog so a stale one does not fight
        # with this fresh start. The new one is started at the end.
        self._watchdog_active = False
        # Always reset the elapsed timer so the dashboard shows a
        # fresh "0s" when the user clicks Start. Prior runs
        # accumulated a 47-minute elapsed that was confusing the
        # user (a click on Start appeared to do nothing because the
        # state did not visibly change).
        self.started_at = time.time()
        self.last_error = None
        spawned = 0
        for lane in self.lanes:
            if not lane.enabled:
                continue
            # If a previous run left the lane slot in self.processes
            # but the workers all exited, drop the stale slot so
            # _spawn gets a clean slate.
            prior = self.processes.get(lane.name)
            if prior and all(lp.proc.poll() is not None for lp in prior):
                self.processes.pop(lane.name, None)
            # Re-spawn only if the lane has claimable work. The
            # watchdog does the same check; we just inline it here
            # so the response can tell the user "no work to do"
            # without waiting 15s for a watchdog tick.
            work_state = self._lane_work_state(lane)
            if work_state == "complete":
                continue
            try:
                self._spawn(lane)
                spawned += len(self.processes.get(lane.name, []))
            except Exception as exc:
                self.last_error = f"spawn {lane.name} failed: {exc}"
                print(f"[orchestrator] spawn {lane.name} failed: {exc}")
        if spawned == 0:
            # Nothing to do: surface a clear reason so the dashboard
            # can show "no work" instead of leaving the user guessing.
            if not any(l.enabled for l in self.lanes):
                self.state = "error"
                return {"ok": False, "state": self.state,
                        "message": "No lanes enabled. Enable a lane in the Configure tab.",
                        "spawned": 0}
            # At least one lane is enabled but nothing was spawnable.
            # Check whether the issue is cooldown (waiting) or no
            # work at all (complete).
            any_waiting = any(
                self._lane_work_state(l) == "waiting" for l in self.lanes if l.enabled
            )
            if any_waiting:
                self.state = "waiting"
                return {"ok": True, "state": self.state,
                        "message": "All remaining work is in retry cooldown. The watchdog will resume automatically.",
                        "spawned": 0}
            self.state = "complete"
            return {"ok": True, "state": self.state,
                    "message": "Nothing to do \u2014 every candidate is done.",
                    "spawned": 0}
        self.state = "running"
        # Watchdog: respawn dead workers so the fill never stalls.
        self._watchdog_active = True
        import threading
        threading.Thread(target=self._watchdog_loop, daemon=True).start()
        return {"ok": True, "state": self.state,
                "message": f"Started {spawned} workers across {len(self.processes)} lane(s).",
                "spawned": spawned}

    def stop(self, drain_timeout: float = 30.0) -> None:
        """Stop every running lane. Graceful drain, then SIGKILL."""
        self._watchdog_active = False
        if not self.processes and self.state != "running":
            return
        self.state = "stopping"
        for workers in list(self.processes.values()):
            for lp in workers:
                try:
                    lp.proc.terminate()
                except ProcessLookupError:
                    pass
        deadline = time.time() + drain_timeout
        while time.time() < deadline:
            all_done = True
            for workers in self.processes.values():
                for lp in workers:
                    if lp.proc.poll() is None:
                        all_done = False
                        break
            if all_done:
                break
            time.sleep(0.2)
        for workers in self.processes.values():
            for lp in workers:
                if lp.proc.poll() is None:
                    try:
                        lp.proc.kill()
                    except ProcessLookupError:
                        pass
        for workers in self.processes.values():
            for lp in workers:
                try:
                    lp.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        self.processes.clear()
        self.state = "idle"

    # -- Lifecycle: per lane --------------------------------------------------

    def start_lane(self, name: str) -> tuple[bool, str]:
        """Start a single lane. Returns (ok, error_message).

        The error_message is a human-readable reason. ``ok=True`` with
        a non-empty error_message is used to communicate 'started but
        nothing to do' (e.g. all remaining work is in cooldown).
        """
        self.lanes = lane_config.load_lanes()  # pick up config edits
        if self.state not in ("running", "idle", "waiting", "complete"):
            return False, f"orchestrator is {self.state}; cannot start a lane"
        lane = next((l for l in self.lanes if l.name == name), None)
        if lane is None:
            return False, f"no lane named {name!r}"
        if not lane.enabled:
            return False, f"lane {name!r} is disabled in config; enable it first"
        if name in self.processes and any(lp.proc.poll() is None for lp in self.processes[name]):
            self._ensure_watchdog()
            return True, ""  # already running
        # Validate key availability before spawn. The local provider
        # is special: oMLX auth is optional and not required to call
        # the local server.
        if lane.api_key is None and lane.provider != "local":
            return False, f"lane {name!r} has no API key set; paste one in the lane card on the Configure tab"
        # If the lane has no claimable work, surface that to the
        # caller instead of spawning a worker that immediately exits.
        work_state = self._lane_work_state(lane)
        if work_state == "complete":
            # No work for this lane; leave the state as-is so the
            # caller can decide whether to start other lanes.
            return True, f"lane {name!r} has no remaining work"
        try:
            self._spawn(lane)
        except Exception as exc:
            return False, f"spawn {name} failed: {exc}"
        if self.state == "idle" or self.state == "complete" or self.state == "waiting":
            self.state = "running"
            self.started_at = self.started_at or time.time()
            self.last_error = None
        self._ensure_watchdog()
        if work_state == "waiting":
            return True, f"lane {name!r} started, but all remaining work is in retry cooldown; watchdog will resume"
        return True, ""

    def stop_lane(self, name: str, drain_timeout: float = 15.0) -> tuple[bool, str]:
        """Stop a single lane (all workers). Returns (ok, error_message)."""
        workers = self.processes.get(name)
        if workers is None or all(lp.proc.poll() is not None for lp in workers):
            # Already gone — clean up the slot and report ok.
            self.processes.pop(name, None)
            if not self.processes and self.state == "running":
                self.state = "idle"
            return True, ""
        for lp in workers:
            if lp.proc.poll() is None:
                try:
                    lp.proc.terminate()
                except ProcessLookupError:
                    pass
        deadline = time.time() + drain_timeout
        while time.time() < deadline:
            if all(lp.proc.poll() is not None for lp in workers):
                break
            time.sleep(0.2)
        for lp in workers:
            if lp.proc.poll() is None:
                try:
                    lp.proc.kill()
                except ProcessLookupError:
                    pass
            try:
                lp.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
        self.processes.pop(name, None)
        if not self.processes and self.state == "running":
            self.state = "idle"
        return True, ""

    def lane_running(self, name: str) -> bool:
        """True if the named lane has at least one live subprocess."""
        workers = self.processes.get(name)
        if not workers:
            return False
        return any(lp.proc.poll() is None for lp in workers)

    # -- Internal ------------------------------------------------------------

    def _spawn(self, lane: Lane, task: str = "guide_fill") -> None:
        cmd = [
            sys.executable,
            str(THIS_DIR / "run_lane.py"),
            "--lane-name", lane.name,
            "--provider", lane.provider,
            "--model", lane.model,
            "--min-chars", str(lane.min_chars),
            "--workers", "1",  # one subprocess per worker; multiple workers = multiple subprocesses
            "--task", task,
        ]
        if lane.max_chars is not None:
            cmd.extend(["--max-chars", str(lane.max_chars)])
        if lane.api_key:
            cmd.extend(["--api-key", lane.api_key])
        if lane.base_url:
            cmd.extend(["--base-url", lane.base_url])
        env = os.environ.copy()
        if lane.api_key:
            envname = {
                "deepseek-direct": "DEEPSEEK_API_KEY",
                "openrouter": "OPENROUTER_API_KEY",
                "opencode-go": "OPENCODE_GO_API_KEY",
                "local": "LOCAL_PROVIDER_API_KEY",
                "opencode-zen": "OPENCODE_ZEN_API_KEY",
                "nvidia": "NVIDIA_API_KEY",
                "minimax": "MINIMAX_API_KEY",
            }.get(lane.provider)
            if envname:
                env[envname] = lane.api_key
        # Pin the lane worker's state-file path to the same one the
        # dashboard reads, so the subprocess writes to the volume
        # instead of resolving a default path against its own root.
        import fill_state
        env["FILL_STATE_PATH"] = str(fill_state.state_path())
        # Spawn ``lane.workers`` copies of the subprocess. Each worker
        # claims a different article (dispatch lock serialises claims)
        # so they can run in parallel against the same provider.
        workers: list[LaneProcess] = []
        start_interval = worker_start_interval_s()
        for i in range(lane.workers):
            proc = subprocess.Popen(cmd, env=env, cwd=str(THIS_DIR.parent))
            workers.append(LaneProcess(lane=lane, proc=proc, started_at=time.time()))
            if i < lane.workers - 1:
                time.sleep(start_interval)
        self.processes[lane.name] = workers

    def _spawn_missing(self, lane: Lane, current_workers: list[LaneProcess]) -> None:
        """Top a lane back up to its configured worker count."""
        alive = [w for w in current_workers if w.proc.poll() is None]
        missing = max(0, lane.workers - len(alive))
        if missing <= 0:
            self.processes[lane.name] = alive
            return
        cmd = [
            sys.executable,
            str(THIS_DIR / "run_lane.py"),
            "--lane-name", lane.name,
            "--provider", lane.provider,
            "--model", lane.model,
            "--min-chars", str(lane.min_chars),
            "--workers", "1",
            "--task", "guide_fill",
        ]
        if lane.max_chars is not None:
            cmd.extend(["--max-chars", str(lane.max_chars)])
        if lane.api_key:
            cmd.extend(["--api-key", lane.api_key])
        if lane.base_url:
            cmd.extend(["--base-url", lane.base_url])
        env = os.environ.copy()
        if lane.api_key:
            envname = {
                "deepseek-direct": "DEEPSEEK_API_KEY",
                "openrouter": "OPENROUTER_API_KEY",
                "opencode-go": "OPENCODE_GO_API_KEY",
                "local": "LOCAL_PROVIDER_API_KEY",
                "opencode-zen": "OPENCODE_ZEN_API_KEY",
                "nvidia": "NVIDIA_API_KEY",
                "minimax": "MINIMAX_API_KEY",
            }.get(lane.provider)
            if envname:
                env[envname] = lane.api_key
        env["FILL_STATE_PATH"] = str(fill_state.state_path())
        topped_up = list(alive)
        start_interval = worker_start_interval_s()
        for _ in range(missing):
            proc = subprocess.Popen(cmd, env=env, cwd=str(THIS_DIR.parent))
            topped_up.append(LaneProcess(lane=lane, proc=proc, started_at=time.time()))
            time.sleep(start_interval)
        self.processes[lane.name] = topped_up

    def _ensure_watchdog(self) -> None:
        if self._watchdog_active:
            return
        self._watchdog_active = True
        import threading
        threading.Thread(target=self._watchdog_loop, daemon=True).start()

    def _lane_work_state(self, lane: Lane) -> str:
        """Return claimable | waiting | complete for this lane.

        ``waiting`` means remaining work exists, but every currently
        available candidate is either in-progress or inside retry cooldown.
        The watchdog should keep checking and restart workers after cooldown,
        not mark the lane done.
        """
        try:
            candidates = fill_state._load_candidates()
            rows = fill_state.load_all()
            done = fill_state.global_done_slugs(rows)
            blocked = fill_state.global_retry_blocked_slugs(rows)
            in_progress = fill_state.global_in_progress_slugs(rows)
            has_remaining = False
            for c in candidates:
                slug = c.get("slug")
                if not slug:
                    continue
                size = int(c.get("page_len") or c.get("size") or 0)
                if not lane.matches_size(size):
                    continue
                if slug in done:
                    continue
                has_remaining = True
                if slug not in blocked and slug not in in_progress:
                    return "claimable"
        except Exception as exc:
            print(f"[orchestrator] watchdog: claimable-work check failed for {lane.name}: {exc}; assuming work remains")
            return "claimable"
        return "waiting" if has_remaining else "complete"

    def _watchdog_loop(self) -> None:
        """Background thread: keep enabled started lanes at target worker count.

        Also responsible for transitioning the orchestrator state to
        ``waiting`` (work exists but is in cooldown) or ``complete``
        (nothing left) when no workers are alive.
        """
        while self._watchdog_active:
            time.sleep(15)
            if not self._watchdog_active:
                break
            lanes = lane_config.load_lanes()
            any_waiting = False
            any_complete = True  # flipped to False if any lane has work
            for lane in lanes:
                if not lane.enabled:
                    continue
                workers = self.processes.get(lane.name)
                if workers is None:
                    # Lane was never spawned (e.g. newly enabled).
                    # Don't auto-spawn — user must start manually.
                    # But account for it in the complete/waiting tally.
                    ws = self._lane_work_state(lane)
                    if ws == "complete":
                        continue
                    any_complete = False
                    if ws == "waiting":
                        any_waiting = True
                    continue
                alive = [w for w in workers if w.proc.poll() is None]
                if len(alive) >= lane.workers:
                    self.processes[lane.name] = alive
                    any_complete = False
                    continue
                work_state = self._lane_work_state(lane)
                if work_state == "complete":
                    print(f"[orchestrator] watchdog: {lane.name} has no remaining work; not respawning")
                    self.processes.pop(lane.name, None)
                    continue
                any_complete = False
                if work_state == "waiting":
                    if not alive:
                        print(f"[orchestrator] watchdog: {lane.name} is waiting for retry cooldown/in-flight work; rechecking later")
                    self.processes[lane.name] = alive
                    any_waiting = True
                    continue
                print(
                    f"[orchestrator] watchdog: {lane.name} has {len(alive)}/{lane.workers} "
                    f"workers alive, topping up"
                )
                self._spawn_missing(lane, alive)
            # State transition: if all workers have exited AND no work
            # is left, drop to 'complete'. If workers exited but
            # work is just in cooldown, drop to 'waiting'. Otherwise
            # the run is still actively spawning workers.
            if not self.processes or all(
                not [w for w in ws if w.proc.poll() is None]
                for ws in self.processes.values()
            ):
                if any_complete and not any_waiting:
                    if self.state != "idle":
                        print(f"[orchestrator] watchdog: all work complete, state -> idle")
                        self.state = "idle"
                elif any_waiting:
                    if self.state != "waiting":
                        print(f"[orchestrator] watchdog: only cooldown-bound work remains, state -> waiting")
                        self.state = "waiting"
                else:
                    # Work exists, not in cooldown, but no workers
                    # are alive. This can happen if the user
                    # manually stopped a lane. Leave the state as
                    # 'running' so the dashboard reflects that the
                    # orchestrator is still in a run mode.
                    pass

    # -- Status --------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        elapsed = (time.time() - self.started_at) if self.started_at else 0
        return {
            "state": self.state,
            "started_at": self.started_at,
            "elapsed_s": elapsed,
            "pid": os.getpid(),
            "last_error": self.last_error,
            "lane_processes": {
                name: {
                    "pid": workers[0].proc.pid if workers else None,
                    "running": any(w.proc.poll() is None for w in workers),
                    "worker_count": len(workers),
                    "returncode": workers[0].proc.returncode if workers else None,
                    "started_at": workers[0].started_at if workers else None,
                }
                for name, workers in self.processes.items()
            },
        }

    def aggregate_stats(self) -> dict[str, Any]:
        lane_stats = fill_state.lane_stats()
        done = sum(b.get("done", 0) for b in lane_stats.values())
        in_progress = sum(b.get("in_progress", 0) for b in lane_stats.values())
        failed = sum(b.get("failed_permanent", 0) for b in lane_stats.values())
        return {
            "total_done": done,
            "total_in_progress": in_progress,
            "total_failed_permanent": failed,
            "per_lane": lane_stats,
        }
