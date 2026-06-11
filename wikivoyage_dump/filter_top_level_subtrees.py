#!/usr/bin/env python3
"""
Deterministic top-level subtree filter for Wikivoyage candidates.

Buckets the four top-level non-destination subtrees (Other destinations,
Itineraries, Phrasebooks, Travel topics) into a separate filtered list and
writes a remaining-candidates list for the next stage.

No LLM calls. No DB writes. Reads only wikivoyage_dump/candidate_destinations.jsonl.

Usage:
    python3 wikivoyage_dump/filter_top_level_subtrees.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CANDIDATE_PATH = ROOT / "candidate_destinations.jsonl"

FILTERED_PATH = ROOT / "top_level_filtered_articles.jsonl"
REMAINING_PATH = ROOT / "top_level_remaining_candidates.jsonl"
SUMMARY_PATH = ROOT / "top_level_filter_summary.json"

MAX_CHAIN_DEPTH = 20

# Bucket keys
BUCKET_OTHER = "other_destinations"
BUCKET_ITIN = "itineraries"
BUCKET_PHRASE = "phrasebooks"
BUCKET_TOPIC = "travel_topics"
BUCKET_NONE = None

# Known missing parent_page_id values: these are top-level Wikivoyage
# category pages that are referenced as parent_page_id by many candidates
# but are not present in the candidate file. Mapping these is the
# deterministic way to bucket large subtrees that have no chain ancestor
# in the candidate data.
#
# Discovered by inspecting children of unresolved parent ids:
#   19835 -> Phrasebooks (313 children, all phrasebooks)
#   19833 -> Itineraries (7 children, all "* itineraries" regional roots)
#   121356 -> Travel topics (14 children: Space, Stay safe, Talk, Sleep,
#             Activities, Transportation, Cultural attractions, etc.)
#   26245 -> Other destinations (6 children: Arctic, Islands, Latin
#             America, Mediterranean Sea, Tropics, The West)
KNOWN_BUCKET_MISSING_PARENTS: dict[int, str] = {
    19835: BUCKET_PHRASE,
    19833: BUCKET_ITIN,
    121356: BUCKET_TOPIC,
    26245: BUCKET_OTHER,
}

# Root titles that belong to the "Travel topics" subtree in Wikivoyage.
# These are pages whose own title appears in the data as a top-level
# travel-topic page (i.e. "Travel topics" itself is not in the candidate
# file but these subtree roots are).
TRAVEL_TOPIC_SUBTREE_ROOTS: set[str] = {
    "Cultural attractions",
    "Historical travel",
    "Food and drink",
    "Fiction tourism",
    "Natural attractions",
    "Concerns",
    "National parks",
    "Activities",
    "Transportation",
    "Stay safe",
    "Stay healthy",
    "Reasons to travel",
    "Preparation",
    "Shopping",
    "Sleep",
    "Talk",
    "Space",
    "Drinking",
    "Other destinations",
    "See",
    "Do",
    "Buy",
    "Eat",
    "Connect",
    "Money",
    "Stay",
    "Work",
    "Volunteer travel",
    "Honeymoon travel",
    "Business travel",
    "Accessible travel",
    "LGBT travel",
    "Family travel",
    "Travel as a lifestyle",
    "Independent travel",
    "Sustainable travel",
    "Slow travel",
    "Flashpacking",
    "Visiting cities",
    "Visiting deserts",
    "Visiting farms",
    "Visiting historical sites",
    "Visiting islands",
    "Visiting mountains",
    "Visiting national parks",
    "Visiting small towns",
    "Visiting waterways",
    "Visiting wildlife",
    "Visiting jungles",
    "Visiting forests",
    "Architecture",
    "European history",
    "Military tourism",
    "Science tourism",
    "Driving",
    "Rail travel",
    "Air travel",
    "Bus travel",
    "Cycling",
    "Hiking",
    "Motorcycling",
    "Scuba diving",
    "Snorkeling",
    "Backpacking",
    "Hitchhiking",
    "Volunteer travel",
    "Work",
    "Honeymoon travel",
    "Travel as a lifestyle",
    "Flashpacking",
    "Slow travel",
    "Sustainable travel",
    "Independent travel",
    "Family travel",
    "Accessible travel",
    "LGBT travel",
    "Business travel",
}

# Patterns that bucket purely from candidate title.
PHRASEBOOK_TITLE_RE = re.compile(r"phrasebook", re.IGNORECASE)
# Itinerary title patterns - titles that look like an itinerary article.
# Conservative: contains "itinerary" (with variant suffixes) or a known
# itinerary-style token.
ITINERARY_TITLE_PATTERNS = [
    re.compile(r"\bitinerary\b", re.IGNORECASE),
    re.compile(r"\bitineraries\b", re.IGNORECASE),
    re.compile(r"\bwalking tour\b", re.IGNORECASE),
    re.compile(r"\bdriving tour\b", re.IGNORECASE),
    re.compile(r"\bcircle tour\b", re.IGNORECASE),
    re.compile(r"\bpilgrimage\b", re.IGNORECASE),
    re.compile(r"\btour du\b", re.IGNORECASE),
    re.compile(r"\btrain tour\b", re.IGNORECASE),
    re.compile(r"\btourist trail\b", re.IGNORECASE),
    re.compile(r"\btour of\b", re.IGNORECASE),
    re.compile(r"\btour in\b", re.IGNORECASE),
    re.compile(r"\bday in\b", re.IGNORECASE),
    re.compile(r"\bculinary tour\b", re.IGNORECASE),
    re.compile(r"\bdistillery tour\b", re.IGNORECASE),
    re.compile(r"\bbrewery tour\b", re.IGNORECASE),
    re.compile(r"\bheritage tour\b", re.IGNORECASE),
    re.compile(r"\bscenic route\b", re.IGNORECASE),
    re.compile(r"\bworld heritage tour\b", re.IGNORECASE),
    re.compile(r"\btouring\b", re.IGNORECASE),
    re.compile(r"\bpok\xe9mon tour\b", re.IGNORECASE),
    re.compile(r"\btop gear\b", re.IGNORECASE),
    re.compile(r"\btheme tour\b", re.IGNORECASE),
    re.compile(r"\bspecial\b", re.IGNORECASE),
    re.compile(r"\belroy-sparta state trail\b", re.IGNORECASE),
    re.compile(r"\bstate trail\b", re.IGNORECASE),
    re.compile(r"\blegacy trail\b", re.IGNORECASE),
    re.compile(r"\bby land\b", re.IGNORECASE),
    re.compile(r"\bby rail\b", re.IGNORECASE),
    re.compile(r"\bby train\b", re.IGNORECASE),
    re.compile(r"\bgrand tour\b", re.IGNORECASE),
    re.compile(r"\bround trip\b", re.IGNORECASE),
    re.compile(r"\bdrive\b", re.IGNORECASE),
    re.compile(r"\bha giang loop\b", re.IGNORECASE),
    re.compile(r"\bloop tour\b", re.IGNORECASE),
    re.compile(r"\bloop\b", re.IGNORECASE),
    re.compile(r"\btrek\b", re.IGNORECASE),
    re.compile(r"\bpilgrim route\b", re.IGNORECASE),
    re.compile(r"\bscenic byway\b", re.IGNORECASE),
    re.compile(r"\bscenic drive\b", re.IGNORECASE),
    re.compile(r"\bpilgrimage trail\b", re.IGNORECASE),
    re.compile(r"\bsacred trail\b", re.IGNORECASE),
    re.compile(r"\bcircumnavigate\b", re.IGNORECASE),
    re.compile(r"\boverland\b", re.IGNORECASE),
    re.compile(r"\briver road\b", re.IGNORECASE),
    re.compile(r"\bsea route\b", re.IGNORECASE),
    re.compile(r"\btrade route\b", re.IGNORECASE),
    re.compile(r"\bsilk road\b", re.IGNORECASE),
    re.compile(r"\bappian way\b", re.IGNORECASE),
    re.compile(r"\bcamino\b", re.IGNORECASE),
    re.compile(r"\bkumano kodo\b", re.IGNORECASE),
    re.compile(r"\btokaido\b", re.IGNORECASE),
    re.compile(r"\bpost road\b", re.IGNORECASE),
    re.compile(r"\bglen\b", re.IGNORECASE),
    re.compile(r"\bglen\b", re.IGNORECASE),
    re.compile(r"\bhigh road\b", re.IGNORECASE),
    re.compile(r"\broyal road\b", re.IGNORECASE),
    re.compile(r"\bking's highway\b", re.IGNORECASE),
    re.compile(r"\bconstitutional road\b", re.IGNORECASE),
    re.compile(r"\btrans\b", re.IGNORECASE),
    re.compile(r"\binterstate\b", re.IGNORECASE),
    re.compile(r"\bhighway\b", re.IGNORECASE),
    re.compile(r"\bexpy\b", re.IGNORECASE),
    re.compile(r"\bexpressway\b", re.IGNORECASE),
]


def title_to_bucket(title: str) -> str | None:
    """Bucket decision based purely on candidate title text."""
    if not title:
        return None
    # Phrasebook titles always win - they're never district pages.
    if PHRASEBOOK_TITLE_RE.search(title):
        return BUCKET_PHRASE
    # City-district page style: "CityName / District" or "CityName/Suffix".
    # These are sub-articles of a city, not itineraries, even if the district
    # name happens to contain "Loop" or "Tour".
    if " / " in title or re.match(r"^[A-Za-z][A-Za-z .'-]*/", title):
        return None
    for pat in ITINERARY_TITLE_PATTERNS:
        if pat.search(title):
            return BUCKET_ITIN
    return None


def is_phrasebook_ancestor(title: str) -> bool:
    return bool(title) and PHRASEBOOK_TITLE_RE.search(title)


def is_itinerary_ancestor(title: str) -> bool:
    if not title:
        return False
    t = title.strip().lower()
    if t in {"itineraries", "itinerary"}:
        return True
    # e.g. "Europe itineraries", "Red Centre Itinerary", "Helsinki itineraries"
    if t.endswith(" itineraries") or t.endswith(" itinerary"):
        return True
    return False


def is_travel_topic_ancestor(title: str) -> bool:
    if not title:
        return False
    if title.strip() == "Travel topics":
        return True
    return title in TRAVEL_TOPIC_SUBTREE_ROOTS


def is_other_destinations_ancestor(title: str) -> bool:
    return bool(title) and title.strip() == "Other destinations"


def classify(candidate: dict[str, Any], by_id: dict[int, dict[str, Any]]) -> tuple[str | None, dict[str, Any]]:
    """Return (bucket, debug_info) for a candidate."""
    title = (candidate.get("title") or "").strip()
    debug: dict[str, Any] = {
        "title": title,
        "matched_bucket": None,
        "matched_by": None,
        "chain": [],
        "ancestors_checked": 0,
    }

    # 1) Title-based classification (catches leaf articles whose own title
    # is conclusive, e.g. "Arabic phrasebook", "Brașov cultural itinerary").
    b = title_to_bucket(title)
    if b:
        debug["matched_bucket"] = b
        debug["matched_by"] = f"title:{title}"
        return b, debug

    # 2) Walk parent chain.
    chain: list[str] = []
    cur_id = candidate.get("parent_page_id")
    seen: set[int] = {candidate.get("page_id")}
    depth = 0
    last_title: str | None = None

    # 2a) If the direct parent_page_id points to a known bucket root that
    # is not in the candidate file, classify immediately.
    if cur_id and cur_id in KNOWN_BUCKET_MISSING_PARENTS:
        b = KNOWN_BUCKET_MISSING_PARENTS[cur_id]
        debug["matched_bucket"] = b
        debug["matched_by"] = f"missing-parent:{cur_id}"
        return b, debug

    while cur_id and cur_id in by_id and cur_id not in seen and depth < MAX_CHAIN_DEPTH:
        cur = by_id[cur_id]
        cur_title = (cur.get("title") or "").strip()
        chain.append(cur_title)
        debug["ancestors_checked"] += 1
        if is_other_destinations_ancestor(cur_title):
            debug["matched_bucket"] = BUCKET_OTHER
            debug["matched_by"] = f"ancestor:{cur_title}"
            return BUCKET_OTHER, debug
        if is_itinerary_ancestor(cur_title):
            debug["matched_bucket"] = BUCKET_ITIN
            debug["matched_by"] = f"ancestor:{cur_title}"
            return BUCKET_ITIN, debug
        if is_phrasebook_ancestor(cur_title):
            debug["matched_bucket"] = BUCKET_PHRASE
            debug["matched_by"] = f"ancestor:{cur_title}"
            return BUCKET_PHRASE, debug
        if is_travel_topic_ancestor(cur_title):
            debug["matched_bucket"] = BUCKET_TOPIC
            debug["matched_by"] = f"ancestor:{cur_title}"
            return BUCKET_TOPIC, debug
        last_title = cur_title
        seen.add(cur_id)
        cur_id = cur.get("parent_page_id")
        depth += 1

    # 3) Root match - if the topmost ancestor we found is in a known bucket
    # set, also classify.
    if last_title is not None:
        if is_other_destinations_ancestor(last_title):
            debug["matched_bucket"] = BUCKET_OTHER
            debug["matched_by"] = f"root:{last_title}"
            return BUCKET_OTHER, debug
        if is_itinerary_ancestor(last_title):
            debug["matched_bucket"] = BUCKET_ITIN
            debug["matched_by"] = f"root:{last_title}"
            return BUCKET_ITIN, debug
        if is_phrasebook_ancestor(last_title):
            debug["matched_bucket"] = BUCKET_PHRASE
            debug["matched_by"] = f"root:{last_title}"
            return BUCKET_PHRASE, debug
        if is_travel_topic_ancestor(last_title):
            debug["matched_bucket"] = BUCKET_TOPIC
            debug["matched_by"] = f"root:{last_title}"
            return BUCKET_TOPIC, debug

    debug["chain"] = chain
    return None, debug


def main() -> int:
    if not CANDIDATE_PATH.exists():
        print(f"ERROR: missing {CANDIDATE_PATH}", file=sys.stderr)
        return 1

    by_id: dict[int, dict[str, Any]] = {}
    with CANDIDATE_PATH.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            by_id[r["page_id"]] = r

    total = len(by_id)
    print(f"Loaded {total} candidates", file=sys.stderr)

    bucket_counts: Counter[str] = Counter()
    matched_by_counter: Counter[str] = Counter()
    unresolved_chains = 0
    filtered_rows: list[dict[str, Any]] = []
    remaining_rows: list[dict[str, Any]] = []

    for pid, cand in by_id.items():
        bucket, debug = classify(cand, by_id)
        if bucket is None:
            remaining_rows.append({**cand, "_filter_debug": debug})
            if debug.get("chain") == [] and cand.get("parent_page_id"):
                unresolved_chains += 1
        else:
            row = {**cand, "_filter_bucket": bucket, "_filter_matched_by": debug.get("matched_by")}
            filtered_rows.append(row)
            bucket_counts[bucket] += 1
            mb = debug.get("matched_by") or "?"
            matched_by_counter[f"{bucket}::{mb}"] += 1

    with FILTERED_PATH.open("w") as f:
        for row in filtered_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    with REMAINING_PATH.open("w") as f:
        for row in remaining_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary = {
        "total_candidates": total,
        "filtered_total": len(filtered_rows),
        "remaining_total": len(remaining_rows),
        "filtered_by_bucket": {
            BUCKET_OTHER: bucket_counts.get(BUCKET_OTHER, 0),
            BUCKET_ITIN: bucket_counts.get(BUCKET_ITIN, 0),
            BUCKET_PHRASE: bucket_counts.get(BUCKET_PHRASE, 0),
            BUCKET_TOPIC: bucket_counts.get(BUCKET_TOPIC, 0),
        },
        "unresolved_parent_chains": unresolved_chains,
        "matched_by_sample": dict(matched_by_counter.most_common(20)),
        "outputs": {
            "filtered": str(FILTERED_PATH),
            "remaining": str(REMAINING_PATH),
        },
    }
    with SUMMARY_PATH.open("w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
