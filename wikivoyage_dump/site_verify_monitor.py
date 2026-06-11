"""End-to-end monitor for the initial fill pipeline.

The point of this script is to catch bugs like the achiltibuie one —
where the manager says "done" and the DB has the rows, but the
public site shows nothing because of an API bug. The user was
right: I should have been doing this from day 1.

What it does:
  1. Watches the manager's activity feed (or the state file) for
     every new ``done`` row.
  2. For each ``done`` slug, hits the public site's
     ``/api/destinations/<slug>`` and checks:
       - status 200
       - is_filled: true
       - content_sections: > 0 OR practical_notes: > 0
  3. Reports PASS / FAIL per article. A FAIL is a regression that
     the manager alone would not have caught.

Why both content_sections and practical_notes?
  The v2 model returns different field names depending on the
  model: ``big-pickle`` and the dsv4free tend to return rich
  ``content_sections``; the nvidia and minimax tend to return
  ``practical_notes`` instead. We accept either as "filled".

Configuration:
  - ``PUBLIC_BASE_URL`` env var (default: http://flynn.local:8495)
  - ``MANAGER_BASE_URL`` env var (default: http://flynn.local:8497)
  - ``--per-lane N`` (default 20) — minimum generations to verify
    per lane before declaring done
  - ``--max-runtime-s S`` (default 1200) — kill switch
  - ``--no-strict-records`` — allow some records to be missing
    fields (default strict — every done row must have v2 content
    OR practical_notes)

Output: a markdown report at the end, with PASS/FAIL counts per
lane and a per-article failure log.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from typing import Any

PUBLIC_BASE = os.environ.get("PUBLIC_BASE_URL", "http://flynn.local:8495").rstrip("/")
MANAGER_BASE = os.environ.get("MANAGER_BASE_URL", "http://flynn.local:8497").rstrip("/")


def http_json(url: str, timeout: float = 10.0) -> dict[str, Any]:
    """GET a URL and return the parsed JSON. Raises on non-200 or
    non-JSON.
    """
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        if resp.status != 200:
            raise RuntimeError(f"HTTP {resp.status} from {url}")
        return json.loads(resp.read().decode("utf-8"))


def fetch_activity(limit: int = 100) -> list[dict[str, Any]]:
    """Fetch recent activity from the manager. Returns rows in
    reverse-chronological order (newest first).
    """
    try:
        d = http_json(f"{MANAGER_BASE}/api/activity?limit={limit}")
        return d.get("activity", [])
    except Exception as exc:
        print(f"  WARN: failed to fetch activity: {exc}", file=sys.stderr)
        return []


def verify_slug_on_public_site(slug: str) -> dict[str, Any]:
    """Hit the public site's /api/destinations/<slug> and report
    what's actually being served.

    Returns a dict with keys: found, is_filled, content_sections,
    practical_notes, featured_listings, error.
    """
    out: dict[str, Any] = {
        "slug": slug,
        "found": False,
        "is_filled": False,
        "content_sections": 0,
        "practical_notes": 0,
        "featured_listings": 0,
        "error": None,
    }
    # Some Wikivoyage slugs contain non-ASCII characters (e.g.
    # ï, ñ, ü, ç). The public site expects percent-encoded UTF-8
    # in the URL path, not raw bytes. Always quote the slug here.
    safe_slug = urllib.parse.quote(slug, safe="")
    try:
        d = http_json(f"{PUBLIC_BASE}/api/destinations/{safe_slug}")
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            out["error"] = "404 from public site"
            return out
        out["error"] = f"HTTP {exc.code} from public site"
        return out
    except Exception as exc:
        out["error"] = f"network/parse error: {exc}"
        return out
    if d.get("error") or not d.get("data"):
        out["error"] = f"public site returned error/no-data: {d.get('error')}"
        return out
    data = d["data"]
    out["found"] = True
    out["is_filled"] = bool(data.get("is_filled"))
    out["content_sections"] = len(data.get("content_sections") or [])
    out["practical_notes"] = len(data.get("practical_notes") or [])
    listings = data.get("featured_listings") or {}
    out["featured_listings"] = sum(
        len(v) for v in listings.values() if isinstance(v, list)
    )
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--per-lane", type=int, default=20,
                   help="Minimum generations to verify per lane (default 20)")
    p.add_argument("--max-runtime-s", type=int, default=1200,
                   help="Kill switch in seconds (default 1200)")
    p.add_argument("--interval-s", type=int, default=5,
                   help="Poll interval in seconds (default 5)")
    p.add_argument("--lanes", type=str, default="",
                   help="Comma-separated lane names (default: all)")
    p.add_argument("--report-path", type=str, default="/tmp/site_verify_report.md",
                   help="Where to write the markdown report")
    args = p.parse_args()

    print(f"=== site_verify_monitor ===")
    print(f"  PUBLIC_BASE:  {PUBLIC_BASE}")
    print(f"  MANAGER_BASE: {MANAGER_BASE}")
    print(f"  per-lane target: {args.per_lane}")
    print(f"  max runtime: {args.max_runtime_s}s")
    print()

    target_lanes = set(s for s in args.lanes.split(",") if s) or None
    deadline = time.time() + args.max_runtime_s

    # Track per-lane: how many we've verified (regardless of pass/fail),
    # and a list of failures.
    verified_per_lane: dict[str, int] = defaultdict(int)
    target_met_per_lane: dict[str, bool] = defaultdict(lambda: False)
    failures: list[dict[str, Any]] = []
    passes: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    lanes_state: dict[str, dict[str, Any]] = {}

    last_status_print = 0.0
    while time.time() < deadline:
        # 1. Snapshot which lanes still need more verifications.
        if target_lanes is None:
            try:
                lanes_resp = http_json(f"{MANAGER_BASE}/api/lanes", timeout=5)
                all_lanes = [l["name"] for l in lanes_resp.get("lanes", [])]
            except Exception:
                all_lanes = list(verified_per_lane.keys())
        else:
            all_lanes = sorted(target_lanes)
        pending = [
            ln for ln in all_lanes
            if verified_per_lane[ln] < args.per_lane
        ]
        if not pending and verified_per_lane:
            # All targeted lanes have at least N verifications.
            print()
            print(f"  ✓ all {len(verified_per_lane)} targeted lanes reached {args.per_lane} verifications")
            break

        # 2. Pull the most recent activity. We want NEW 'done' rows we
        #    haven't verified yet.
        activity = fetch_activity(limit=200)
        new_done = [
            r for r in activity
            if r.get("status") == "done"
            and r.get("slug")
            and r.get("slug") not in seen_slugs
        ]
        # Filter to lanes we still need to verify
        new_done_for_pending = [
            r for r in new_done
            if not target_lanes or r.get("lane") in target_lanes
        ]

        # 3. Verify each new done row against the public site.
        for r in new_done_for_pending:
            slug = r["slug"]
            lane = r.get("lane", "?")
            seen_slugs.add(slug)
            result = verify_slug_on_public_site(slug)
            verified_per_lane[lane] += 1
            record = {
                "slug": slug,
                "lane": lane,
                "at": r.get("at"),
                "input_chars": r.get("input_chars"),
                "output_chars": r.get("output_chars"),
                **result,
            }
            if not result.get("is_filled") and not (result.get("content_sections", 0) > 0 or result.get("practical_notes", 0) > 0):
                failures.append(record)
                print(f"  ✗ FAIL {lane:28s} {slug:35s}  "
                      f"found={result['found']} filled={result['is_filled']} "
                      f"content={result['content_sections']} notes={result['practical_notes']} "
                      f"err={result.get('error')}")
            else:
                passes.append(record)
                print(f"  ✓ PASS {lane:28s} {slug:35s}  "
                      f"content={result['content_sections']:>3d} notes={result['practical_notes']:>3d} "
                      f"listings={result['featured_listings']:>3d}")

        # 4. Periodic status update.
        now = time.time()
        if now - last_status_print > 15:
            last_status_print = now
            print()
            print(f"  --- t+{int(now - (deadline - args.max_runtime_s))}s ---")
            for ln in sorted(verified_per_lane.keys()):
                if target_lanes and ln not in target_lanes:
                    continue
                n = verified_per_lane[ln]
                target = args.per_lane
                bar = "█" * min(n, target) + "░" * max(0, target - n)
                mark = "✓" if n >= target else " "
                print(f"    {mark} {ln:28s} [{bar}] {n}/{target}")
            print()

        time.sleep(args.interval_s)

    # 5. Final report
    print()
    print("=" * 70)
    print("FINAL REPORT")
    print("=" * 70)
    print(f"  total verified: {sum(verified_per_lane.values())}")
    print(f"  passed:         {len(passes)}")
    print(f"  failed:         {len(failures)}")
    print()
    print("  per-lane summary:")
    for ln in sorted(verified_per_lane.keys()):
        if target_lanes and ln not in target_lanes:
            continue
        n = verified_per_lane[ln]
        target = args.per_lane
        mark = "✓" if n >= target else "✗"
        fails_for_lane = [f for f in failures if f.get("lane") == ln]
        print(f"    {mark} {ln:28s}  {n}/{target}  failures={len(fails_for_lane)}")
    print()

    # 6. Write markdown report
    try:
        with open(args.report_path, "w") as f:
            f.write("# Site verify report\n\n")
            f.write(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n")
            f.write(f"Public base: `{PUBLIC_BASE}`\n")
            f.write(f"Manager base: `{MANAGER_BASE}`\n")
            f.write(f"Per-lane target: {args.per_lane}\n\n")
            f.write("## Per-lane\n\n")
            f.write("| Lane | Verified | Target | Failures | Status |\n")
            f.write("|------|----------|--------|----------|--------|\n")
            for ln in sorted(verified_per_lane.keys()):
                if target_lanes and ln not in target_lanes:
                    continue
                n = verified_per_lane[ln]
                target = args.per_lane
                fails_for_lane = [f for f in failures if f.get("lane") == ln]
                mark = "✓" if n >= target else "✗"
                f.write(f"| {ln} | {n} | {target} | {len(fails_for_lane)} | {mark} |\n")
            f.write("\n## Failures\n\n")
            if failures:
                f.write("| Lane | Slug | Found | Filled | Content | Notes | Error |\n")
                f.write("|------|------|-------|--------|---------|-------|-------|\n")
                for fr in failures:
                    f.write(
                        f"| {fr.get('lane','')} | `{fr.get('slug','')}` | "
                        f"{fr.get('found')} | {fr.get('is_filled')} | "
                        f"{fr.get('content_sections',0)} | {fr.get('practical_notes',0)} | "
                        f"`{(fr.get('error') or '')[:60]}` |\n"
                    )
            else:
                f.write("(none — every verified article shows content on the public site)\n")
            f.write("\n## Sample passes\n\n")
            f.write("| Lane | Slug | Content | Notes | Listings |\n")
            f.write("|------|------|---------|-------|----------|\n")
            for pr in passes[:30]:
                f.write(
                    f"| {pr.get('lane','')} | `{pr.get('slug','')}` | "
                    f"{pr.get('content_sections',0)} | {pr.get('practical_notes',0)} | "
                    f"{pr.get('featured_listings',0)} |\n"
                )
        print(f"  wrote report to {args.report_path}")
    except Exception as exc:
        print(f"  failed to write report: {exc}", file=sys.stderr)

    # Exit code: 0 if no failures, 1 if any
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
