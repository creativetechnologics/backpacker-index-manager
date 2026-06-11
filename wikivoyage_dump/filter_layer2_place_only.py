#!/usr/bin/env python3
"""
Layer-2 deterministic pre-filter for LLM-bound Wikivoyage candidates.

Operates on the output of filter_top_level_subtrees.py. Cross-references the
existing deterministic_article_classification.jsonl + deterministic_unresolved_articles.jsonl
to remove articles that the deterministic rules already identified as
non-destination kinds (hierarchy_node, attachable_resource, supporting_topic).

Then applies additional structural rules to the remaining
unresolved + untouched pool:
  - no place-name signal: title is a Wikivoyage section header, a "List of X"
    page, a meta page, a generic stub marker, etc.
  - extremely small pages with no listings, no coords, and a generic title.

No LLM calls. No DB writes.

Usage:
    python3 wikivoyage_dump/filter_layer2_place_only.py
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

LAYER1_REMAINING = ROOT / "top_level_remaining_candidates.jsonl"
LAYER1_FILTERED = ROOT / "top_level_filtered_articles.jsonl"
DET_CLASSIFIED = ROOT / "deterministic_article_classification.jsonl"
DET_UNRESOLVED = ROOT / "deterministic_unresolved_articles.jsonl"

# Outputs
LLM_READY = ROOT / "llm_ready_places.jsonl"
NON_PLACE_EXCLUDED = ROOT / "layer2_excluded_non_places.jsonl"
SUMMARY = ROOT / "layer2_summary.json"

# Non-destination routing_roles from the deterministic classifier.
NON_PLACE_ROLES = {
    "hierarchy_node",
    "attachable_resource",
    "supporting_topic",
    "reject",
}

# Wikivoyage section-header titles (these are not real pages in destination
# sense, but section anchors). The candidate file should not contain them,
# but guard against any that slip in.
WV_SECTION_HEADER_TITLES = {
    "Talk", "See", "Do", "Eat", "Drink", "Sleep", "Buy", "Connect",
    "Get in", "Get around", "Stay safe", "Stay healthy", "Respect",
    "Understand", "See also", "Go next", "History", "Geography",
    "Climate", "Background", "By plane", "By train", "By bus", "By car",
    "On foot", "By boat", "Cope", "Nearby", "Learn", "Work",
    "Tourist information", "Fees and permits", "Get out",
}

# Generic stub / placeholder title patterns.
GENERIC_TITLE_PATTERNS = [
    re.compile(r"^list of ", re.IGNORECASE),
    re.compile(r"^outline of ", re.IGNORECASE),
    re.compile(r"^index of ", re.IGNORECASE),
    re.compile(r"^stub$", re.IGNORECASE),
    re.compile(r"^placeholder$", re.IGNORECASE),
    re.compile(r"^disambiguation$", re.IGNORECASE),
    re.compile(r"^disambiguation\b", re.IGNORECASE),
    re.compile(r"\(disambiguation\)$", re.IGNORECASE),
    re.compile(r"^template:", re.IGNORECASE),
    re.compile(r"^category:", re.IGNORECASE),
    re.compile(r"^module:", re.IGNORECASE),
    re.compile(r"^file:", re.IGNORECASE),
    re.compile(r"^image:", re.IGNORECASE),
    re.compile(r"^mediawiki:", re.IGNORECASE),
    re.compile(r"^help:", re.IGNORECASE),
    re.compile(r"^wikipedia:", re.IGNORECASE),
    re.compile(r"^wikivoyage:", re.IGNORECASE),
    re.compile(r"^user:", re.IGNORECASE),
    re.compile(r"^project:", re.IGNORECASE),
    re.compile(r"^portal:", re.IGNORECASE),
    re.compile(r"^recent changes", re.IGNORECASE),
    re.compile(r"^random page", re.IGNORECASE),
    re.compile(r"^about wikivoyage", re.IGNORECASE),
    re.compile(r"^how to edit", re.IGNORECASE),
    re.compile(r"^policies and guidelines", re.IGNORECASE),
    re.compile(r"^contribute", re.IGNORECASE),
    re.compile(r"^plunge", re.IGNORECASE),
    re.compile(r"^tourist office", re.IGNORECASE),
]

# Itinerary/transport-route like page title patterns for the unresolved +
# untouched pool, where there is no chain to walk. Conservative: only the
# very obvious ones.
ITIN_LIKE_RESIDUAL_PATTERNS = [
    re.compile(r"\bpilgrimage\b", re.IGNORECASE),
    re.compile(r"\btrain tour\b", re.IGNORECASE),
    re.compile(r"\bwalking tour\b", re.IGNORECASE),
    re.compile(r"\bcircle tour\b", re.IGNORECASE),
    re.compile(r"\bdistillery tour\b", re.IGNORECASE),
    re.compile(r"\bbrewery tour\b", re.IGNORECASE),
    re.compile(r"\bworld heritage tour\b", re.IGNORECASE),
    re.compile(r"\bscenic byway\b", re.IGNORECASE),
    re.compile(r"\bscenic drive\b", re.IGNORECASE),
    re.compile(r"\bscenic route\b", re.IGNORECASE),
    re.compile(r"\bsacred trail\b", re.IGNORECASE),
    re.compile(r"\bstate trail\b", re.IGNORECASE),
    re.compile(r"\belroy-sparta state trail\b", re.IGNORECASE),
    re.compile(r"\bto brunei by land\b", re.IGNORECASE),
    re.compile(r"\bby land\b", re.IGNORECASE),
    re.compile(r"\bby rail\b", re.IGNORECASE),
    re.compile(r"\bby train\b", re.IGNORECASE),
    re.compile(r"\bby car\b", re.IGNORECASE),
    re.compile(r"\bto kuching\b", re.IGNORECASE),
    re.compile(r"\btour du mont blanc\b", re.IGNORECASE),
    re.compile(r"\btop gear\b", re.IGNORECASE),
    re.compile(r"\bpok\xe9mon tour\b", re.IGNORECASE),
    re.compile(r"\btrans\w*continental\b", re.IGNORECASE),
    re.compile(r"\bacross .* by train\b", re.IGNORECASE),
    re.compile(r"\bby bicycle\b", re.IGNORECASE),
    re.compile(r"\bbike tour\b", re.IGNORECASE),
    re.compile(r"\bto brunei by\b", re.IGNORECASE),
    re.compile(r"\bha giang loop\b", re.IGNORECASE),
    re.compile(r"\bmae hong son loop\b", re.IGNORECASE),
    re.compile(r"\bquilotoa loop\b", re.IGNORECASE),
    re.compile(r"\bnorth cascade loop\b", re.IGNORECASE),
    re.compile(r"\bthe wire tour\b", re.IGNORECASE),
    re.compile(r"\byunnan tourist trail\b", re.IGNORECASE),
    re.compile(r"\bbreaking bad tour\b", re.IGNORECASE),
    re.compile(r"\bamerican industry tour\b", re.IGNORECASE),
    re.compile(r"\bcraft brewery tour\b", re.IGNORECASE),
    re.compile(r"\balong the yangtze\b", re.IGNORECASE),
    re.compile(r"\balong the yellow river\b", re.IGNORECASE),
    re.compile(r"\balong the grand canal\b", re.IGNORECASE),
    re.compile(r"\b88 temple\b", re.IGNORECASE),
    re.compile(r"\bchugoku 33 kannon\b", re.IGNORECASE),
    re.compile(r"\bsunshine coast-vancouver\b", re.IGNORECASE),
    re.compile(r"\bgreat post road\b", re.IGNORECASE),
    re.compile(r"\btokaido road\b", re.IGNORECASE),
    re.compile(r"\bkumano kodo\b", re.IGNORECASE),
    re.compile(r"\bdarien gap\b", re.IGNORECASE),
    re.compile(r"\balaska highway\b", re.IGNORECASE),
    re.compile(r"\btransylvania triangle\b", re.IGNORECASE),
    re.compile(r"\bgreina walking\b", re.IGNORECASE),
    re.compile(r"\bone day in\b", re.IGNORECASE),
    re.compile(r"\bhong kong culinary\b", re.IGNORECASE),
    re.compile(r"\bworld heritage tour in nara\b", re.IGNORECASE),
    re.compile(r"\byaowarat and phahurat\b", re.IGNORECASE),
    re.compile(r"\bdriving tour of scotland\b", re.IGNORECASE),
    re.compile(r"\btouring .* shaker\b", re.IGNORECASE),
    re.compile(r"\bappian way\b", re.IGNORECASE),
    re.compile(r"\bcamino\b", re.IGNORECASE),
    re.compile(r"\bsilk road\b", re.IGNORECASE),
    re.compile(r"\bglen\b", re.IGNORECASE),
    re.compile(r"\bhigh road\b", re.IGNORECASE),
    re.compile(r"\broyal road\b", re.IGNORECASE),
    re.compile(r"\bking's highway\b", re.IGNORECASE),
    re.compile(r"\bconstitutional road\b", re.IGNORECASE),
    re.compile(r"\bappalachian trail\b", re.IGNORECASE),
    re.compile(r"\bcontinental divide trail\b", re.IGNORECASE),
    re.compile(r"\bpacific crest trail\b", re.IGNORECASE),
    re.compile(r"\bice age trail\b", re.IGNORECASE),
    re.compile(r"\bnorth country trail\b", re.IGNORECASE),
    re.compile(r"\bflorida trail\b", re.IGNORECASE),
    re.compile(r"\bazara trail\b", re.IGNORECASE),
    re.compile(r"\bnakasendo\b", re.IGNORECASE),
    re.compile(r"\bsan'in kaigan\b", re.IGNORECASE),
    re.compile(r"\bcoast to coast walk\b", re.IGNORECASE),
    re.compile(r"\boffa's dyke\b", re.IGNORECASE),
    re.compile(r"\bhadrian's wall\b", re.IGNORECASE),
    re.compile(r"\bvia francigena\b", re.IGNORECASE),
    re.compile(r"\blep\xc3\xa9e\b", re.IGNORECASE),
    re.compile(r"\btour of britain\b", re.IGNORECASE),
    re.compile(r"\broute of\b", re.IGNORECASE),
    re.compile(r"\bferry route\b", re.IGNORECASE),
    re.compile(r"\bshipping route\b", re.IGNORECASE),
    re.compile(r"\bship route\b", re.IGNORECASE),
    re.compile(r"\bhistoric route\b", re.IGNORECASE),
    re.compile(r"\bcolonial route\b", re.IGNORECASE),
    re.compile(r"\bcruise\b", re.IGNORECASE),
    re.compile(r"\bvoyage\b", re.IGNORECASE),
    re.compile(r"\bexpedition\b", re.IGNORECASE),
    re.compile(r"\boverland\b", re.IGNORECASE),
    re.compile(r"\bsafari\b", re.IGNORECASE),
    re.compile(r"\btrek\b", re.IGNORECASE),
]

# Topic / non-place patterns - articles that describe a topic, not a place,
# even if their parent chain looked geographic. Use only on unresolved +
# untouched rows, where the deterministic rule was uncertain.
TOPIC_TITLE_RESIDUAL = [
    re.compile(r"\btourism\b", re.IGNORECASE),  # e.g. "Science tourism", "Agritourism"
    re.compile(r"\bcuisine of\b", re.IGNORECASE),
    re.compile(r"\bhistory of\b", re.IGNORECASE),
    re.compile(r"\bculture of\b", re.IGNORECASE),
    re.compile(r"\bgeography of\b", re.IGNORECASE),
    re.compile(r"\bclimate of\b", re.IGNORECASE),
    re.compile(r"\bdemographics of\b", re.IGNORECASE),
    re.compile(r"\beconomy of\b", re.IGNORECASE),
    re.compile(r"\btransportation in\b", re.IGNORECASE),
    re.compile(r"\bsports in\b", re.IGNORECASE),
    re.compile(r"\bledyard\b", re.IGNORECASE),
]

# Minimum page length to be considered for the LLM when there is no
# classification signal at all (truly untouched). Anything shorter is almost
# certainly a redirect/empty/missing-stub.
MIN_PAGE_LEN_FLOOR = 200


def is_wv_section_header(title: str) -> bool:
    return title.strip() in WV_SECTION_HEADER_TITLES


def is_generic_meta_title(title: str) -> bool:
    for pat in GENERIC_TITLE_PATTERNS:
        if pat.search(title):
            return True
    return False


def is_residual_itinerary(title: str) -> bool:
    for pat in ITIN_LIKE_RESIDUAL_PATTERNS:
        if pat.search(title):
            return True
    return False


def is_residual_topic_title(title: str) -> bool:
    for pat in TOPIC_TITLE_RESIDUAL:
        if pat.search(title):
            return True
    return False


def main() -> int:
    for p in [LAYER1_REMAINING, LAYER1_FILTERED, DET_CLASSIFIED, DET_UNRESOLVED]:
        if not p.exists():
            print(f"ERROR: missing required input {p}", file=sys.stderr)
            return 1

    # Load layer-1 remaining
    layer1_rows: list[dict[str, Any]] = []
    with LAYER1_REMAINING.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            layer1_rows.append(json.loads(line))

    # Load layer-1 filtered (so we can union them into the "non-place" output
    # for the final summary)
    layer1_filtered_ids: set[int] = set()
    with LAYER1_FILTERED.open() as f:
        for line in f:
            r = json.loads(line)
            layer1_filtered_ids.add(r["page_id"])

    # Load deterministic classifications
    det: dict[int, dict[str, Any]] = {}
    with DET_CLASSIFIED.open() as f:
        for line in f:
            r = json.loads(line)
            det[r["page_id"]] = r

    # Load deterministic unresolved
    unres: dict[int, dict[str, Any]] = {}
    with DET_UNRESOLVED.open() as f:
        for line in f:
            r = json.loads(line)
            unres[r["page_id"]] = r

    print(
        f"Layer-1 remaining: {len(layer1_rows)}; "
        f"det classified: {len(det)}; "
        f"det unresolved: {len(unres)}; "
        f"layer-1 filtered: {len(layer1_filtered_ids)}",
        file=sys.stderr,
    )

    by_id_lookup: dict[int, dict[str, Any]] = {r["page_id"]: r for r in layer1_rows}

    excluded: list[dict[str, Any]] = []
    llm_ready: list[dict[str, Any]] = []

    excluded_reasons: Counter[str] = Counter()
    excluded_role_breakdown: Counter[str] = Counter()

    for row in layer1_rows:
        pid = row["page_id"]
        title = (row.get("title") or "").strip()
        page_len = row.get("page_len") or 0
        d = det.get(pid)
        u = unres.get(pid)

        # Rule A: deterministic classifier said "not a destination"
        if d and d.get("routing_role") in NON_PLACE_ROLES:
            role = d["routing_role"]
            excluded.append({
                **row,
                "_exclude_layer": 2,
                "_exclude_reason": f"deterministic_routing_role:{role}",
                "_exclude_role": role,
                "_exclude_evidence": d.get("evidence"),
            })
            excluded_reasons[f"deterministic_routing_role:{role}"] += 1
            excluded_role_breakdown[role] += 1
            continue

        # Rule B: Wikivoyage section header title
        if is_wv_section_header(title):
            excluded.append({
                **row,
                "_exclude_layer": 2,
                "_exclude_reason": "section_header_title",
            })
            excluded_reasons["section_header_title"] += 1
            continue

        # Rule C: generic meta / list / disambiguation title
        if is_generic_meta_title(title):
            excluded.append({
                **row,
                "_exclude_layer": 2,
                "_exclude_reason": "generic_meta_title",
            })
            excluded_reasons["generic_meta_title"] += 1
            continue

        # For unresolved + untouched, apply additional rules.
        if d is None:  # untouched
            # Rule D: extremely short page AND no useful signal
            has_coords = bool(row.get("latitude") and row.get("longitude"))
            has_qid = bool(row.get("wikidata_qid"))
            if page_len < MIN_PAGE_LEN_FLOOR and not has_coords and not has_qid:
                excluded.append({
                    **row,
                    "_exclude_layer": 2,
                    "_exclude_reason": f"untouched_tiny_no_signal:{page_len}",
                })
                excluded_reasons[f"untouched_tiny_no_signal"] += 1
                continue
            # Rule E: residual itinerary title pattern (untouched pool)
            if is_residual_itinerary(title):
                excluded.append({
                    **row,
                    "_exclude_layer": 2,
                    "_exclude_reason": "residual_itinerary_title",
                })
                excluded_reasons["residual_itinerary_title"] += 1
                continue
            # Rule F: residual topic title pattern (untouched pool)
            if is_residual_topic_title(title):
                excluded.append({
                    **row,
                    "_exclude_layer": 2,
                    "_exclude_reason": "residual_topic_title",
                })
                excluded_reasons["residual_topic_title"] += 1
                continue
        elif u is not None:
            # Rule E2: residual itinerary title pattern (unresolved pool)
            if is_residual_itinerary(title):
                excluded.append({
                    **row,
                    "_exclude_layer": 2,
                    "_exclude_reason": "residual_itinerary_title",
                })
                excluded_reasons["residual_itinerary_title"] += 1
                continue
            # Rule F2: residual topic title pattern (unresolved pool)
            if is_residual_topic_title(title):
                excluded.append({
                    **row,
                    "_exclude_layer": 2,
                    "_exclude_reason": "residual_topic_title",
                })
                excluded_reasons["residual_topic_title"] += 1
                continue

        # Survives layer 2 - LLM-ready.
        llm_ready.append({
            **row,
            "_llm_bucket": "destination",
            "_llm_status": (
                "deterministic_canonical" if d
                else "untouched"
                if u is None
                else "deterministic_unresolved"
            ),
        })

    # Write outputs
    with LLM_READY.open("w") as f:
        for row in llm_ready:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    with NON_PLACE_EXCLUDED.open("w") as f:
        for row in excluded:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    summary = {
        "layer1_remaining_in": len(layer1_rows),
        "layer1_filtered_in": len(layer1_filtered_ids),
        "llm_ready_total": len(llm_ready),
        "llm_ready_by_status": {
            "deterministic_canonical": sum(1 for r in llm_ready if r["_llm_status"] == "deterministic_canonical"),
            "deterministic_unresolved": sum(1 for r in llm_ready if r["_llm_status"] == "deterministic_unresolved"),
            "untouched": sum(1 for r in llm_ready if r["_llm_status"] == "untouched"),
        },
        "layer2_excluded_total": len(excluded),
        "layer2_excluded_reasons": dict(excluded_reasons.most_common()),
        "layer2_excluded_role_breakdown": dict(excluded_role_breakdown.most_common()),
        "layer2_excluded_titles_sample": [
            r["title"] for r in excluded[:20]
        ],
        "outputs": {
            "llm_ready": str(LLM_READY),
            "excluded": str(NON_PLACE_EXCLUDED),
        },
    }
    with SUMMARY.open("w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)

    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
