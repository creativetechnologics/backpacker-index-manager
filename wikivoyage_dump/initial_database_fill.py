#!/usr/bin/env python3
"""Initial database fill — terminal-native entrypoint.

Run this script to:
  1. Validate lane + key configuration
  2. Start the web dashboard on http://127.0.0.1:8742
  3. Spawn one subprocess per enabled lane
  4. Forward Ctrl-C / SIGTERM to all workers (graceful drain)
  5. Exit when all workers complete or are stopped

Examples:
  python3 wikivoyage_dump/initial_database_fill.py
  python3 wikivoyage_dump/initial_database_fill.py --port 8800
  python3 wikivoyage_dump/initial_database_fill.py --skip-web   # headless

State is written to ~/Library/Application Support/Backpacker Index Manager/fill_state.jsonl
(or the legacy deepseek_import_state.jsonl if that already has data).
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
import webbrowser
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(THIS_DIR))

import lane_config
from orchestrator import Orchestrator


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Initial database fill (terminal-native)")
    p.add_argument("--host", default=os.environ.get("FILL_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.environ.get("FILL_PORT", "8742")))
    p.add_argument("--skip-web", action="store_true", help="Do not start the web server")
    p.add_argument("--no-browser", action="store_true", help="Do not auto-open the dashboard")
    p.add_argument("--validate-only", action="store_true", help="Validate config and exit")
    p.add_argument("--auto-start", action="store_true",
                   help="Auto-start workers on boot. Default: wait for user to click Start in dashboard.")
    return p.parse_args()


def load_and_report() -> list[lane_config.Lane]:
    """Load lanes and print any problems, but never abort.

    A bad lanes.json must not prevent the server from booting —
    otherwise the user can't reach the UI to fix it. Validation
    that blocks saves is enforced at the API layer instead.
    """
    lanes = lane_config.load_lanes()
    errors = lane_config.validate_lanes(lanes)
    if errors:
        print("WARNING: lanes.json has errors. Open the dashboard to fix them.")
        for e in errors:
            print(f"  ! {e}")
    warnings = lane_config.warn_missing_keys(lanes)
    for w in warnings:
        print(f"  ! {w}")
    return lanes


def main() -> int:
    args = parse_args()
    # Run the legacy api-keys.json → lanes.json migration BEFORE we
    # load or print any lanes, so load_lanes() sees the new format
    # and the print below can fingerprint the embedded keys.
    try:
        from lane_config import migrate_legacy_keys_file
        n = migrate_legacy_keys_file()
        if n:
            print(f"Migrated {n} key(s) from legacy api-keys.json into lanes.json")
    except Exception as exc:
        print(f"warning: legacy key migration failed: {exc}")
    lanes = load_and_report()
    print(f"Loaded {len(lanes)} lanes ({sum(1 for l in lanes if l.enabled)} enabled):")
    for l in lanes:
        rng = f"{l.min_chars}..{l.max_chars if l.max_chars is not None else '∞'}"
        key = f"key={l.api_key_fingerprint()}" if l.api_key else "no-key"
        en = "ON" if l.enabled else "OFF"
        print(f"  [{en}] {l.name}: {l.provider}/{l.model} size={rng} {key}")

    if args.validate_only:
        return 0

    orchestrator = Orchestrator()

    server_thread = None
    if not args.skip_web:
        os.environ["FILL_HOST"] = args.host
        os.environ["FILL_PORT"] = str(args.port)
        import fill_server  # noqa: F401  ensure FastAPI app is loaded
        import uvicorn

        config = uvicorn.Config(
            fill_server.app,
            host=args.host,
            port=args.port,
            log_level="info",
            access_log=False,
        )
        server = uvicorn.Server(config)

        def _run_server():
            server.run()

        server_thread = threading.Thread(target=_run_server, daemon=True)
        server_thread.start()
        # Give uvicorn a moment to bind.
        time.sleep(0.5)
        url = f"http://{args.host}:{args.port}/"
        print(f"\nDashboard: {url}\n")
        if not args.no_browser:
            try:
                webbrowser.open(url)
            except Exception:
                pass

    # Auto-start only if explicitly requested. By default, the user
    # configures keys and lanes in the dashboard first, then clicks Start.
    if args.auto_start:
        orchestrator.start()
        print(f"Orchestrator state: {orchestrator.state}")
        print("Press Ctrl-C to stop. Workers will drain gracefully (30s timeout).\n")
    else:
        print("Orchestrator idle. Open the dashboard and click Start when ready.")
        print("Or re-run with --auto-start to begin immediately.\n")

    stop_signal = threading.Event()

    def _on_signal(signum, _frame):
        if stop_signal.is_set():
            # Second signal: hard kill.
            print("\nForced exit.")
            os._exit(130)
        print(f"\nReceived signal {signum}; stopping workers…")
        stop_signal.set()
        orchestrator.stop()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    # Wait for all workers to exit (they exit on their own when the
    # XML stream is exhausted, or when we stop them).
    try:
        while not stop_signal.is_set():
            # If orchestrator state went back to idle (all workers exited),
            # break the loop.
            if orchestrator.state == "idle" and orchestrator.processes:
                break
            # If we never auto-started and no workers are running, just
            # wait for SIGINT/SIGTERM.
            if not args.auto_start and not orchestrator.processes:
                time.sleep(0.5)
                continue
            time.sleep(0.5)
    except KeyboardInterrupt:
        _on_signal(signal.SIGINT, None)

    print(f"Final state: {orchestrator.status()['state']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
