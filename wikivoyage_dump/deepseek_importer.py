#!/usr/bin/env python3
"""
Backpacker Index Wikivoyage -> DeepSeek importer.

What it does:
  - Reads local Wikivoyage dump files.
  - Calls DeepSeek V4 Flash directly, or an OpenCode-Go-compatible command.
  - Validates basic JSON shape.
  - Writes extraction output to local PostgreSQL in real time.
  - Optionally mirrors successful article transactions to Flynn staging.
  - Resumes from DB extraction runs and a local JSONL state file.

No non-stdlib Python dependencies.

Examples:
  python3 wikivoyage_dump/deepseek_importer.py
"""

from __future__ import annotations

import argparse
import bz2
import getpass
import hashlib
import html
import json
import os
import random
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parent
CONFIG_PATH = Path.home() / ".config" / "backpacker-index" / "wikivoyage_importer.json"
STATE_PATH = ROOT / "deepseek_import_state.jsonl"
PROMPT_PATH = ROOT / "prompts" / "deepseek-wikivoyage-unified.md"
# [REMOVED: CLASSIFY_PROMPT_PATH = ROOT / "prompts" / "deepseek-classify-wikivoyage-v1.md", MASTER_CLASSIFY_PROMPT_PATH = ROOT / "prompts" / "deepseek-master-classify-wikiv... (2 lines)]
SCHEMA_PATH = ROOT / "schemas" / "deepseek_wikivoyage_v1.json"
CANDIDATES_PATH = ROOT / "candidate_destinations.jsonl"
XML_DUMP_PATH = ROOT / "enwikivoyage-latest-pages-articles.xml.bz2"
SKIP_LIST_PATH = ROOT / "wikivoyage_skip_list.jsonl"
NON_LOCATION_RESOURCES_PATH = ROOT / "wikivoyage_non_location_resources.jsonl"
MASTER_CLASSIFICATION_PATH = ROOT / "master_article_classification.jsonl"
MASTER_HIERARCHY_PATH = ROOT / "master_destination_hierarchy.jsonl"
MASTER_ATTACHABLE_PATH = ROOT / "master_attachable_resources.jsonl"
MASTER_SUPPORTING_PATH = ROOT / "master_supporting_topics.jsonl"
MASTER_REJECTED_PATH = ROOT / "master_rejected_articles.jsonl"
DETERMINISTIC_CLASSIFICATION_PATH = ROOT / "deterministic_article_classification.jsonl"
DETERMINISTIC_HIERARCHY_PATH = ROOT / "deterministic_destination_hierarchy.jsonl"
DETERMINISTIC_ATTACHABLE_PATH = ROOT / "deterministic_attachable_resources.jsonl"
DETERMINISTIC_SUPPORTING_PATH = ROOT / "deterministic_supporting_topics.jsonl"
DETERMINISTIC_REJECTED_PATH = ROOT / "deterministic_rejected_articles.jsonl"
DETERMINISTIC_UNRESOLVED_PATH = ROOT / "deterministic_unresolved_articles.jsonl"

DEFAULT_DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_OPENAI_COMPATIBLE_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_ANTHROPIC_MODEL = "claude-3-5-haiku-latest"
PROVIDER_CHOICES = ["deepseek-direct", "opencode-go", "openai-compatible", "anthropic"]
DEFAULT_LOCAL_PSQL = "docker exec -i backpacker-index-db psql -U backpacker -d backpacker_index -v ON_ERROR_STOP=1"
DEFAULT_STAGING_PSQL = "ssh gtbarnes@flynn.local docker exec -i bp-staging-db psql -U backpacker -d backpacker_index -v ON_ERROR_STOP=1"
KEEP_PARSE_STRATEGIES = {"full_destination", "limited_destination"}
MASTER_ROUTING_ROLES = {"canonical_destination", "hierarchy_node", "attachable_resource", "supporting_topic", "reject", "needs_llm"}
MASTER_HIERARCHY_LEVELS = {None, "continent", "macro_region", "country", "state_province", "county_district", "city_town_village", "neighborhood", "park_or_island_base"}
MASTER_PLACEMENTS = {None, "things_to_do", "day_trips", "go_next", "transport", "language", "background", "safety_health", "related_guides"}
MASTER_PARSE_STRATEGIES = {"full_destination", "limited_destination", "hierarchy_only", "attach_resource", "topic_only", "skip", "needs_llm"}


TOP_LEVEL_TABLES: dict[str, tuple[str, bool]] = {
    "classification": ("destination_classification", False),
    "content_sections": ("destination_content_sections", True),
    "practicalities": ("destination_practicalities", False),
    "neighborhoods": ("destination_neighborhoods", True),
    "payment_methods": ("destination_payment_methods", True),
    "cash_access": ("destination_cash_access", False),
    "money_tips": ("destination_money_tips", True),
    "connectivity_providers": ("destination_connectivity_providers", True),
    "internet_access": ("destination_internet_access", True),
    "power_plugs": ("destination_power_plugs", True),
    "language_notes": ("destination_language_notes", True),
    "religion_culture": ("destination_religion_culture", False),
    "etiquette_items": ("destination_etiquette_items", True),
    "safety_items": ("destination_safety_items", True),
    "health_risks": ("destination_health_risks", True),
    "medical_services": ("destination_medical_services", True),
    "water_safety": ("destination_water_safety", False),
    "accessibility": ("destination_accessibility", False),
    "legal_notes": ("destination_legal_notes", True),
    "emergency_services": ("destination_emergency_services", True),
    "tourist_information_centers": ("destination_tourist_information_centers", True),
    "permits_fees": ("destination_permits_fees", True),
    "entry_requirements": ("destination_entry_requirements", True),
    "driving_rules": ("destination_driving_rules", False),
    "vehicle_rental_options": ("vehicle_rental_options", True),
    "apps": ("destination_apps", True),
    "media_links": ("destination_media_links", True),
    "budget_items": ("destination_budget_items", True),
    "events": ("destination_events", True),
    "day_trips": ("destination_day_trips", True),
    "special_interest_details": ("destination_special_interest_details", True),
    "work_study_volunteer": ("destination_work_study_volunteer", True),
    "source_snippets": ("destination_source_snippets", True),
}

ARRAY_INT_COLUMNS = {"typical_months", "months"}
ARRAY_TEXT_COLUMNS = {
    "alternate_names",
    "affected_areas",
    "adapter_needed_from_countries",
    "best_for",
    "best_for_tags",
    "blocking_reasons",
    "brand_names",
    "caution_areas",
    "destination_section_placement",
    "do_not_publish_reasons",
    "important_religious_sites",
    "missing_major_sections",
    "needs_review_reasons",
    "network_generation",
    "operator_names",
    "language_codes",
    "timezone_ids",
    "plug_types",
    "risk_tags",
    "locked_fields",
    "prevention_tips",
    "primary_religions",
    "purchase_locations",
    "recommended_areas",
    "required_gear",
    "required_permits",
    "services",
    "source_priority",
    "source_section_keys",
    "tags",
    "topup_locations",
    "transport_modes",
    "typical_use_cases",
    "useful_phrases",
}
NUMERIC_COLUMNS = {
    "amount_low", "amount_typical", "amount_high", "amount_usd_typical",
    "cost_low", "cost_typical", "cost_high", "cost_usd",
    "discount_amount", "exchange_rate_used", "latitude", "longitude",
}
BOOLEAN_COLUMNS = {
    "advance_booking_required", "agent_suggested", "airport_purchase_available",
    "altitude_risk", "bargaining_common", "beach_destination", "bottled_water_common",
    "coastal", "curb_cuts_common", "data_only_available", "desert_destination",
    "drinking_water_safe", "embed_allowed", "family_safe", "filter_recommended",
    "good_for_backpackers", "guide_required", "has_attribution", "has_budget",
    "has_image", "has_location", "has_safety", "helmet_required",
    "international_driving_permit_required", "island", "jungle_destination",
    "legal_or_affiliate_disclosure_needed", "local_address_required", "manually_approved",
    "market_cash_needed", "mosquito_risk", "mountain_destination", "motorcycle_license_required",
    "offline_supported", "passport_required", "publishable", "refill_stations_available",
    "required", "requires_human_review", "requires_local_bank", "requires_local_id",
    "requires_local_phone", "seatbelt_required", "should_publish", "small_bills_needed",
    "sponsored_or_commercial", "stale_critical_info", "tap_water_safe", "tethering_allowed",
    "thin_content", "tipping_cash_needed", "tourist_plan_available", "translation_app_useful",
    "usd_eur_useful", "visible", "voice_sms_available",
}

ENUM_VALUES: dict[str, set[str]] = {
    "atm_availability": {"rare", "limited", "common", "widespread"},
    "exchange_office_availability": {"rare", "limited", "common", "widespread"},
    "cash_needed_level": {"low", "medium", "high"},
    "acceptance_level": {"rare", "limited", "moderate", "common", "near_universal"},
    "foreign_card_reliability": {"poor", "mixed", "good", "excellent"},
    "coverage_quality": {"poor", "mixed", "good", "excellent"},
    "availability_level": {"rare", "limited", "common", "widespread"},
    "severity": {"low", "medium", "high", "critical", "notice", "important", "serious"},
    "verification_state": {"staging", "pending_review", "verified", "needs_review", "rejected"},

    "traveler_relevance": {"primary_destination", "side_trip", "transit_stop", "special_interest", "low"},
    "parse_strategy": {"full_destination", "limited_destination", "route_or_itinerary", "topic_only", "skip"},
    "category": {"sleep", "food", "transit", "activities", "shopping", "misc"},
    "opportunity_type": {"study", "language_school", "university", "volunteer", "work", "working_holiday", "digital_nomad", "internship", "other"},
    "access_type": {"hostel_wifi", "hotel_wifi", "cafe_wifi", "public_wifi", "coworking", "library", "internet_cafe", "other"},
    "service_type": {"physical_sim", "esim", "tourist_sim", "data_only_esim", "pocket_wifi", "public_wifi", "other", "hospital", "clinic", "pharmacy", "dentist", "emergency_number", "travel_clinic", "general_emergency", "police", "tourist_police", "ambulance", "fire", "mountain_rescue", "coast_guard", "park_ranger", "embassy", "consulate", "immigration"},
    "item_type": {"scam", "crime", "area_warning", "transport_warning", "health", "natural_hazard", "political", "other"},
    "risk_type": {"water", "food_safety", "mosquito", "malaria", "dengue", "altitude", "heat", "cold", "air_quality", "sun", "wildlife", "ocean", "medical_access", "vaccination", "other"},
}

REQUIRED_COLUMNS: dict[str, list[str]] = {
    "destination_payment_methods": ["method_type", "method_name"],
    "destination_money_tips": ["tip_type", "title"],
    "destination_content_sections": ["section_key", "heading", "body"],
    "destination_budget_items": ["category", "item_name"],
    "destination_permits_fees": ["fee_or_permit_name", "fee_type"],
    "destination_safety_items": ["item_type", "title"],
    "destination_health_risks": ["risk_type", "title"],
    "destination_connectivity_providers": ["provider_name", "service_type"],
    "destination_internet_access": ["access_type"],
    "destination_power_plugs": ["plug_type"],
    "destination_language_notes": ["language_name"],
    "destination_etiquette_items": ["etiquette_type", "title"],
    "destination_legal_notes": ["legal_topic", "title"],
    "destination_day_trips": ["target_name"],
    "destination_special_interest_details": ["interest_type", "title"],
    "destination_source_snippets": ["target_table", "snippet"],
    "destination_emergency_services": ["service_type"],
    "destination_tourist_information_centers": ["name"],
    "destination_events": ["event_name"],
    "destination_apps": ["app_name", "app_category"],
    "destination_medical_services": ["service_type"],
    "vehicle_rental_options": ["vehicle_type"],
    "destination_media_links": ["media_type", "title", "media_url"],
    "destination_work_study_volunteer": ["opportunity_type", "title"],
    "destination_entry_requirements": ["requirement_type", "title"],
}


@dataclass
class Candidate:
    page_id: int
    title: str
    slug: str
    page_len: int
    status: str
    parent_page_id: int | None
    wikidata_qid: str | None
    page_image_filename: str | None
    latitude: float | None
    longitude: float | None
    categories: list[str] = field(default_factory=list)


class ImporterError(Exception):
    pass


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def log(message: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {message}", flush=True)


def load_config() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    try:
        os.chmod(CONFIG_PATH, 0o600)
    except OSError:
        pass


def configure() -> None:
    config = load_config()
    print("Backpacker Index Wikivoyage importer config")
    print(f"Config file: {CONFIG_PATH}")
    print()

    current_key = config.get("deepseek_api_key") or os.environ.get("DEEPSEEK_API_KEY")
    if current_key:
        masked = current_key[:6] + "..." + current_key[-4:]
        print(f"DeepSeek API key already set: {masked}")
    key = getpass.getpass("DeepSeek API key (blank to keep current): ").strip()
    if key:
        config["deepseek_api_key"] = key

    model = input(f"DeepSeek model [{config.get('deepseek_model', DEFAULT_DEEPSEEK_MODEL)}]: ").strip()
    if model:
        config["deepseek_model"] = model

    base_url = input(f"DeepSeek API URL [{config.get('deepseek_api_url', DEFAULT_DEEPSEEK_URL)}]: ").strip()
    if base_url:
        config["deepseek_api_url"] = base_url

    opencode_cmd = input(
        "OpenCode-Go command, reads prompt on stdin and returns JSON on stdout "
        f"[{config.get('opencode_go_command', '')}]: "
    ).strip()
    if opencode_cmd:
        config["opencode_go_command"] = opencode_cmd

    db_target = input(f"Default DB target local/staging/both [{config.get('db_target', 'staging')}]: ").strip()
    if db_target in {"local", "staging", "both"}:
        config["db_target"] = db_target

    local_psql = input(f"Local psql command [{config.get('local_psql', DEFAULT_LOCAL_PSQL)}]: ").strip()
    if local_psql:
        config["local_psql"] = local_psql

    staging_psql = input(f"Staging psql command [{config.get('staging_psql', DEFAULT_STAGING_PSQL)}]: ").strip()
    if staging_psql:
        config["staging_psql"] = staging_psql

    save_config(config)
    print("Saved config.")


def shell_words(command: str) -> list[str]:
    return shlex.split(command)


class PsqlClient:
    def __init__(self, command: str, name: str):
        # Append -t (tuples only) -A (unaligned) to the psql command.
        # This avoids the intermittent ``\a: extra argument "on" ignored``
        # error that occurs when psql parses inline meta-commands
        # incorrectly under load.
        parts = shlex.split(command)
        for flag in ("-t", "-A"):
            if flag not in parts:
                parts.append(flag)
        self.command = shlex.join(parts)
        self.name = name
        self.columns_cache: dict[str, set[str]] = {}

    def run(self, sql: str, capture: bool = False) -> str:
        # Strip inline formatting meta-commands. The -t -A flags are
        # already on the command line; inline \t on / \a on cause
        # intermittent ``\a: extra argument "on" ignored`` errors.
        for prefix in ("\\t on\n\\a on\n", "\\a on\n\\t on\n", "\\t on\n", "\\a on\n"):
            if sql.startswith(prefix):
                sql = sql[len(prefix):]
                break
        try:
            timeout_s = max(5.0, float(os.environ.get("FILL_PSQL_TIMEOUT_S", "120")))
        except ValueError:
            timeout_s = 120.0
        for attempt in range(3):
            try:
                proc = subprocess.run(
                    shell_words(self.command),
                    input=sql,
                    text=True,
                    capture_output=True,
                    cwd=PROJECT_ROOT,
                    timeout=timeout_s,
                )
            except subprocess.TimeoutExpired as exc:
                if attempt < 2:
                    time.sleep(0.3)
                    continue
                raise ImporterError(f"{self.name} psql timed out after {timeout_s:.0f}s") from exc
            if proc.returncode != 0:
                stderr = proc.stderr.strip() or proc.stdout.strip()
                # ``\a: extra argument "on" ignored`` is a known
                # psql race condition. Retry up to 3 times with a
                # brief sleep between attempts.
                if "extra argument" in stderr and attempt < 2:
                    time.sleep(0.3)
                    continue
                raise ImporterError(f"{self.name} psql failed: {stderr}")
            return proc.stdout if capture else ""
        raise ImporterError(f"{self.name} psql failed after 3 retries")

    def scalar(self, sql: str) -> str | None:
        out = self.run(sql, capture=True).strip()
        lines = [line.strip() for line in out.splitlines() if line.strip()]
        noise = re.compile(r"^(INSERT|UPDATE|DELETE|SELECT|BEGIN|COMMIT|CREATE|ALTER|NOTICE|SET)\b", re.I)
        data_lines = [line for line in lines if not noise.match(line)]
        uuidish = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
        for line in data_lines:
            if uuidish.match(line):
                return line
        return data_lines[0] if data_lines else None

    def columns(self, table: str) -> set[str]:
        if table in self.columns_cache:
            return self.columns_cache[table]
        # Use -t -A flags on the command line instead of \t on/\a on
        # inside the SQL. The inline format sometimes hits a psql
        # parsing edge case where \a on is interpreted as \a (toggle)
        # + "on" (extra argument, ignored), which pollutes stderr and
        # can cause spurious failures.
        command = shlex.split(self.command)
        # Append -t (tuples only) -A (unaligned) right before the db name
        if "-t" not in command and "--tuples-only" not in command:
            # Insert -t -A before the database argument or at end
            has_A = "-A" in command or "--no-align" in command
            has_t = "-t" in command or "--tuples-only" in command
            if not has_t:
                command.append("-t")
            if not has_A:
                command.append("-A")
        sql = f"""
SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = {sql_literal(table)};
"""
        try:
            out = subprocess.run(
                command, input=sql, text=True, capture_output=True,
                cwd=PROJECT_ROOT, timeout=10,
            )
            if out.returncode != 0:
                # Fall back to the old inline approach on failure
                command2 = shlex.split(self.command)
                sql2 = f"""
\\t on
\\a on
SELECT column_name
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name = {sql_literal(table)};
"""
                out = subprocess.run(
                    command2, input=sql2, text=True, capture_output=True,
                    cwd=PROJECT_ROOT, timeout=10,
                )
            cols = {line.strip() for line in out.stdout.splitlines()
                     if line.strip() and not line.startswith("SELECT")}
            self.columns_cache[table] = cols
            return cols
        except Exception:
            return set()

    def test(self) -> bool:
        try:
            out = self.run("SELECT 1;\n", capture=True)
            return any(line.strip() == "1" for line in out.splitlines())
        except Exception:
            return False


def sql_literal(value: Any, column: str | None = None) -> str:
    if value is None:
        return "NULL"
    if column in ARRAY_TEXT_COLUMNS and not isinstance(value, list):
        return "ARRAY[" + sql_literal(str(value)) + "]::text[]"
    if column in ARRAY_INT_COLUMNS and not isinstance(value, list):
        try:
            return "ARRAY[" + str(int(value)) + "]::integer[]"
        except (TypeError, ValueError):
            return "ARRAY[]::integer[]"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if isinstance(value, (dict, list)) and column not in ARRAY_TEXT_COLUMNS and column not in ARRAY_INT_COLUMNS:
        return sql_literal(json.dumps(value, ensure_ascii=False)) + "::jsonb"
    if isinstance(value, list):
        if column in ARRAY_INT_COLUMNS:
            ints = []
            for item in value:
                try:
                    ints.append(str(int(item)))
                except (TypeError, ValueError):
                    pass
            return "ARRAY[" + ",".join(ints) + "]::integer[]"
        vals = [sql_literal(str(item)) for item in value if item is not None]
        return "ARRAY[" + ",".join(vals) + "]::text[]"
    text = str(value)
    return "'" + text.replace("'", "''") + "'"


def safe_float(value: Any, min_value: float | None = None, max_value: float | None = None) -> float | None:
    if value in (None, ""):
        return None
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    if min_value is not None and parsed < min_value:
        return None
    if max_value is not None and parsed > max_value:
        return None
    return parsed


def clean_llm_text(value: Any) -> str:
    text = str(value or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {'"', "'"}:
        text = text[1:-1].strip()
    return text


def slugify(title: str) -> str:
    s = re.sub(r"[\s_]+", "-", title.strip())
    return re.sub(r"[^a-z0-9\-]", "", s.lower())


def display_title(title: str) -> str:
    if "/" in title:
        parent, child = title.split("/", 1)
        return f"{child.strip()}, {parent.strip()}"
    match = re.match(r"^(.+?)\s+\((.+?)\)$", title.strip())
    if match:
        return f"{match.group(1).strip()}, {match.group(2).strip()}"
    return title.strip()


def load_candidates() -> list[Candidate]:
    candidates: list[Candidate] = []
    with CANDIDATES_PATH.open(encoding="utf-8") as f:
        for line in f:
            data = json.loads(line)
            candidates.append(Candidate(**data))
    return candidates


def load_state() -> dict[int, dict[str, Any]]:
    state: dict[int, dict[str, Any]] = {}
    if not STATE_PATH.exists():
        return state
    with STATE_PATH.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            row = json.loads(line)
            state[int(row["page_id"])] = row
    return state


def append_state(row: dict[str, Any]) -> None:
    with STATE_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def article_scope(candidates: list[Candidate], scope: str, limit: int | None, seed: int, local_db: PsqlClient | None) -> list[Candidate]:
    if scope == "tier1":
        selected = [c for c in candidates if c.status in {"usable", "star", "guide"}]
    elif scope == "tier2":
        selected = [c for c in candidates if c.status in {"usable", "star", "guide"} or c.page_len >= 5000]
    elif scope == "all":
        selected = candidates[:]
    elif scope == "pilot":
        rng = random.Random(seed)
        selected = candidates[:]
        rng.shuffle(selected)
    elif scope == "existing":
        if local_db is None:
            raise ImporterError("existing scope requires local DB access")
        sql = """
\\t on
\\a on
SELECT slug FROM destinations WHERE slug IS NOT NULL ORDER BY slug;
"""
        out = local_db.run(sql, capture=True)
        slugs = {line.strip() for line in out.splitlines() if line.strip()}
        selected = [c for c in candidates if c.slug in slugs]
    elif scope == "failed":
        state = load_state()
        failed = {pid for pid, row in state.items() if row.get("status") == "failed"}
        selected = [c for c in candidates if c.page_id in failed]
    else:
        raise ImporterError(f"Unknown scope: {scope}")
    if scope != "pilot":
        selected.sort(key=lambda c: (c.status, c.page_len, c.title))
    if scope == "pilot" and limit:
        selected = selected[:limit]
    elif scope != "pilot" and limit:
        selected = selected[:limit]
    return selected


_ARTICLE_CACHE_DIR = Path(os.environ.get(
    "BACKPACKER_SUPPORT_DIR",
    str(Path(__file__).resolve().parent),
))
ARTICLE_CACHE_PATH = _ARTICLE_CACHE_DIR / "article_text_cache.jsonl"
ARTICLE_CACHE_LOCK_PATH = _ARTICLE_CACHE_DIR / "article_text_cache.lock"
ARTICLE_CACHE_DB_PATH = _ARTICLE_CACHE_DIR / "article_text_cache.sqlite"


def _article_cache_ready() -> bool:
    if not ARTICLE_CACHE_PATH.exists() or ARTICLE_CACHE_PATH.stat().st_size == 0:
        return False
    try:
        return ARTICLE_CACHE_PATH.stat().st_mtime >= XML_DUMP_PATH.stat().st_mtime
    except OSError:
        return False


def _article_cache_db_ready() -> bool:
    if not ARTICLE_CACHE_DB_PATH.exists() or ARTICLE_CACHE_DB_PATH.stat().st_size == 0:
        return False
    try:
        return ARTICLE_CACHE_DB_PATH.stat().st_mtime >= XML_DUMP_PATH.stat().st_mtime
    except OSError:
        return False


def _build_article_cache_db() -> None:
    """Build a persistent SQLite page_id -> article text cache once."""
    import fcntl
    import sqlite3

    ARTICLE_CACHE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(ARTICLE_CACHE_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if _article_cache_db_ready():
            return
        tmp = ARTICLE_CACHE_DB_PATH.with_suffix(".sqlite.tmp")
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass
        conn = sqlite3.connect(str(tmp))
        try:
            conn.execute("PRAGMA journal_mode=OFF")
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute(
                "CREATE TABLE articles ("
                "page_id INTEGER PRIMARY KEY, title TEXT NOT NULL, "
                "revision_id INTEGER, text TEXT NOT NULL)"
            )
            batch: list[tuple[int, str, int | None, str]] = []
            with bz2.open(XML_DUMP_PATH, "rt", encoding="utf-8", errors="ignore") as f:
                buf = ""
                for chunk in f:
                    buf += chunk
                    while "</page>" in buf:
                        page_xml, buf = buf.split("</page>", 1)
                        m = re.search(r"<id>(\d+)</id>", page_xml)
                        if not m:
                            continue
                        page_id = int(m.group(1))
                        title_m = re.search(r"<title>(.*?)</title>", page_xml, re.S)
                        rev_ids = re.findall(r"<id>(\d+)</id>", page_xml)
                        text_m = re.search(r"<text[^>]*>(.*?)</text>", page_xml, re.S)
                        batch.append((
                            page_id,
                            html.unescape(title_m.group(1)) if title_m else "",
                            int(rev_ids[1]) if len(rev_ids) > 1 else None,
                            html.unescape(text_m.group(1)) if text_m else "",
                        ))
                        if len(batch) >= 1000:
                            conn.executemany(
                                "INSERT OR REPLACE INTO articles (page_id, title, revision_id, text) VALUES (?, ?, ?, ?)",
                                batch,
                            )
                            conn.commit()
                            batch = []
            if batch:
                conn.executemany(
                    "INSERT OR REPLACE INTO articles (page_id, title, revision_id, text) VALUES (?, ?, ?, ?)",
                    batch,
                )
                conn.commit()
            conn.execute("CREATE INDEX IF NOT EXISTS idx_articles_page_id ON articles(page_id)")
            conn.commit()
        finally:
            conn.close()
        os.replace(tmp, ARTICLE_CACHE_DB_PATH)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def get_article_by_page_id(page_id: int) -> tuple[int, str, str, int | None] | None:
    """Return ``(page_id, title, text, revision_id)`` from the SQLite cache."""
    import sqlite3

    _build_article_cache_db()
    conn = sqlite3.connect(str(ARTICLE_CACHE_DB_PATH))
    try:
        row = conn.execute(
            "SELECT title, text, revision_id FROM articles WHERE page_id = ?",
            (int(page_id),),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None
    title, text, revision_id = row
    return int(page_id), title or "", text or "", revision_id


def _build_article_cache() -> None:
    """Materialize page_id/title/revision/text rows once.

    Lane workers used to parse the compressed XML independently. With 30
    workers that meant 30 repeated BZ2/XML walks before the first API call.
    This cache keeps the expensive parse single-pass; workers then stream a
    plain JSONL file.
    """
    import fcntl

    ARTICLE_CACHE_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(ARTICLE_CACHE_LOCK_PATH), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        if _article_cache_ready():
            return
        tmp = ARTICLE_CACHE_PATH.with_suffix(".jsonl.tmp")
        with bz2.open(XML_DUMP_PATH, "rt", encoding="utf-8", errors="ignore") as f, tmp.open("w", encoding="utf-8") as out:
            buf = ""
            for chunk in f:
                buf += chunk
                while "</page>" in buf:
                    page_xml, buf = buf.split("</page>", 1)
                    m = re.search(r"<id>(\d+)</id>", page_xml)
                    if not m:
                        continue
                    page_id = int(m.group(1))
                    title_m = re.search(r"<title>(.*?)</title>", page_xml, re.S)
                    rev_ids = re.findall(r"<id>(\d+)</id>", page_xml)
                    text_m = re.search(r"<text[^>]*>(.*?)</text>", page_xml, re.S)
                    row = {
                        "page_id": page_id,
                        "title": html.unescape(title_m.group(1)) if title_m else "",
                        "revision_id": int(rev_ids[1]) if len(rev_ids) > 1 else None,
                        "text": html.unescape(text_m.group(1)) if text_m else "",
                    }
                    out.write(json.dumps(row, ensure_ascii=False) + "\n")
        os.replace(tmp, ARTICLE_CACHE_PATH)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def stream_articles(page_ids: set[int]) -> Iterable[tuple[int, str, str, int | None]]:
    _build_article_cache()
    with ARTICLE_CACHE_PATH.open(encoding="utf-8") as f:
        for line in f:
            if not page_ids:
                return
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            page_id = int(row.get("page_id") or 0)
            if page_id not in page_ids:
                continue
            yield page_id, row.get("title") or "", row.get("text") or "", row.get("revision_id")
            page_ids.remove(page_id)


def stream_articles_from_xml(page_ids: set[int]) -> Iterable[tuple[int, str, str, int | None]]:
    with bz2.open(XML_DUMP_PATH, "rt", encoding="utf-8", errors="ignore") as f:
        buf = ""
        for chunk in f:
            buf += chunk
            while "</page>" in buf:
                page_xml, buf = buf.split("</page>", 1)
                m = re.search(r"<id>(\d+)</id>", page_xml)
                if not m:
                    continue
                page_id = int(m.group(1))
                if page_id not in page_ids:
                    continue
                title_m = re.search(r"<title>(.*?)</title>", page_xml, re.S)
                rev_ids = re.findall(r"<id>(\d+)</id>", page_xml)
                text_m = re.search(r"<text[^>]*>(.*?)</text>", page_xml, re.S)
                title = html.unescape(title_m.group(1)) if title_m else ""
                revision_id = int(rev_ids[1]) if len(rev_ids) > 1 else None
                text = html.unescape(text_m.group(1)) if text_m else ""
                yield page_id, title, text, revision_id
                page_ids.remove(page_id)
                if not page_ids:
                    return


def strip_listing_templates(wikitext: str) -> str:
    starts = [m.start() for m in re.finditer(r"\{\{\s*(see|do|buy|eat|drink|sleep|listing|marker|go|around)\b", wikitext, re.I)]
    if not starts:
        return wikitext
    ranges: list[tuple[int, int]] = []
    for start in starts:
        depth = 0
        end = start
        while end < len(wikitext) - 1:
            pair = wikitext[end : end + 2]
            if pair == "{{":
                depth += 1
                end += 2
                continue
            if pair == "}}":
                depth -= 1
                end += 2
                if depth <= 0:
                    break
                continue
            end += 1
        ranges.append((start, end))
    out: list[str] = []
    last = 0
    for start, end in ranges:
        if start < last:
            continue
        out.append(wikitext[last:start])
        last = end
    out.append(wikitext[last:])
    return "".join(out)


def sectionize(wikitext: str, body_cap: int = 12000, lead_cap: int = 8000) -> list[dict[str, str]]:
    matches = list(re.finditer(r"^(={2,6})\s*(.*?)\s*\1\s*$", wikitext, flags=re.M))
    if not matches:
        return [{"key": "lead", "heading": "Lead", "body": wikitext[:body_cap]}]
    sections: list[dict[str, str]] = []
    lead = wikitext[: matches[0].start()].strip()
    if lead:
        sections.append({"key": "lead", "heading": "Lead", "body": lead[:lead_cap]})
    for i, match in enumerate(matches):
        heading = re.sub(r"<.*?>", "", match.group(2)).strip()
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(wikitext)
        body = wikitext[start:end].strip()
        key = slugify(heading).replace("-", "_") or "section"
        if body:
            sections.append({"key": key, "heading": heading, "body": body[:body_cap]})
    return sections


def extract_listing_templates(wikitext: str, limit: int = 300) -> list[dict[str, Any]]:
    starts = [m.start() for m in re.finditer(r"\{\{\s*(see|do|buy|eat|drink|sleep|listing|marker|go|around)\b", wikitext, re.I)]
    listings: list[dict[str, Any]] = []
    for idx, start in enumerate(starts[:limit]):
        depth = 0
        end = start
        while end < len(wikitext) - 1:
            pair = wikitext[end : end + 2]
            if pair == "{{":
                depth += 1
                end += 2
                continue
            if pair == "}}":
                depth -= 1
                end += 2
                if depth <= 0:
                    break
                continue
            end += 1
        raw = wikitext[start:end]
        first = raw[2:].split("|", 1)[0].strip().lower() if raw.startswith("{{") else "listing"
        fields: dict[str, str] = {}
        for part in raw.strip("{}").split("|")[1:]:
            if "=" in part:
                k, v = part.split("=", 1)
                fields[k.strip().lower().replace("-", "_")] = v.strip()
        listings.append({
            "listing_uid": hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()[:16],
            "listing_type": first,
            "name": fields.get("name"),
            "raw_template": fields,
        })
    return listings


def build_packet(candidate: Candidate, title: str, wikitext: str, revision_id: int | None, packet_mode: str = "lean") -> dict[str, Any]:
    lean = packet_mode == "lean"
    source_text = strip_listing_templates(wikitext) if lean else wikitext
    sections = sectionize(source_text, body_cap=3000 if lean else 12000, lead_cap=3000 if lean else 8000)
    useful_sections = [
        s for s in sections
        if s["key"] in {
            "lead", "understand", "talk", "get_in", "get_around", "see", "do", "buy",
            "eat", "drink", "sleep", "stay_safe", "stay_healthy", "connect", "respect",
            "cope", "go_next", "fees_and_permits", "learn", "work", "cities",
            "regions", "other_destinations", "tourist_information", "climate",
        }
        or (not lean and len(s["body"]) > 600)
    ]
    listings = extract_listing_templates(wikitext)
    if lean:
        def compact_listing(x: dict[str, Any]) -> dict[str, Any]:
            raw = x.get("raw_template") or {}
            keep = {
                "listing_uid": x.get("listing_uid"),
                "listing_type": x.get("listing_type"),
                "name": x.get("name"),
            }
            for key in ("alt", "address", "directions", "price", "hours", "url", "website", "content"):
                if raw.get(key):
                    keep[key] = raw.get(key)
            return keep

        listings_for_llm = [
            compact_listing(x)
            for x in listings[:250]
        ]
    else:
        listings_for_llm = listings
    return {
        "metadata": {
            "page_id": candidate.page_id,
            "revision_id": revision_id,
            "title": title or candidate.title,
            "slug": candidate.slug,
            "status": candidate.status,
            "page_len": candidate.page_len,
            "parent_page_id": candidate.parent_page_id,
            "wikidata_qid": candidate.wikidata_qid,
            "page_image_filename": candidate.page_image_filename,
            "latitude": candidate.latitude,
            "longitude": candidate.longitude,
            "categories": candidate.categories,
            "source_url": f"https://en.wikivoyage.org/wiki/{candidate.title.replace(' ', '_')}",
        },
        "packet_mode": packet_mode,
        "sections": useful_sections[:80],
        "listings": listings_for_llm,
    }


class ModelClient:
    def extract(self, prompt: str) -> tuple[dict[str, Any], dict[str, int]]:
        raise NotImplementedError


class DeepSeekDirectClient(ModelClient):
    def __init__(self, config: dict[str, Any]):
        self.api_key = os.environ.get("DEEPSEEK_API_KEY") or config.get("deepseek_api_key")
        if not self.api_key:
            raise ImporterError("Missing DeepSeek API key. Run configure or set DEEPSEEK_API_KEY.")
        self.model = os.environ.get("DEEPSEEK_MODEL") or config.get("deepseek_model") or DEFAULT_DEEPSEEK_MODEL
        self.url = config.get("deepseek_api_url") or DEFAULT_DEEPSEEK_URL

    def extract(self, prompt: str) -> tuple[dict[str, Any], dict[str, int]]:
        content, usage = self.call(prompt)
        try:
            return parse_model_json(content), usage
        except ImporterError:
            repair_prompt = (
                "The following text was intended to be JSON but is invalid. "
                "Return only corrected valid JSON. Do not add or remove facts.\n\n"
                + content[:120000]
            )
            repaired, repair_usage = self.call(repair_prompt)
            usage = merge_usage(usage, repair_usage)
            return parse_model_json(repaired), usage

    def call(self, prompt: str) -> tuple[str, dict[str, int]]:
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You are a strict JSON extraction engine."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=180) as res:
                response = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="ignore")
            raise ImporterError(f"DeepSeek HTTP {exc.code}: {body[:1000]}") from exc
        content = response["choices"][0]["message"]["content"]
        return content, response.get("usage", {})


# [REMOVED: class OpenAICompatibleClient(DeepSeekDirectClient):, def __init__(self, config: dict[str, Any]):, self.api_key = (... (113 lines)]
def parse_model_json(content: str) -> dict[str, Any]:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).strip()
        text = re.sub(r"```$", "", text).strip()
    # Some models (notably MiniMax-M2.7/M3 and DeepSeek Reasoner) emit
    # a ``<think>…</think>`` block BEFORE the JSON. The thinking block
    # can itself contain curly braces, which would confuse the
    # brace-extraction fallback below. Strip the entire thinking
    # block first so we only try to parse the real response.
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(text[start : end + 1])
        else:
            raise ImporterError(f"Model returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ImporterError("Model JSON was not an object")
    if "classifications" in data:
        return data
    if "prose_sections" in data and "featured_listings" in data:
        data.setdefault("destination", {})
        data.setdefault("classification", {
            "parse_strategy": "full_destination",
            "article_kind": "city",
            "confidence_score": data.get("quality", {}).get("overall_confidence"),
        })
        data.setdefault("content_sections", [])
        data.setdefault("quality", {
            "overall_confidence": None,
            "missing_major_sections": [],
            "needs_review_reasons": [],
            "do_not_publish_reasons": [],
        })
    if "classification" in data:
        data.setdefault("destination", {})
        data.setdefault("content_sections", [])
        data.setdefault("quality", {
            "overall_confidence": data.get("classification", {}).get("confidence_score"),
            "missing_major_sections": [],
            "needs_review_reasons": [],
            "do_not_publish_reasons": [],
        })
    for key in ("destination", "classification", "content_sections", "quality"):
        if key not in data:
            raise ImporterError(f"Model JSON missing required key: {key}")
    return data


# [REMOVED: def merge_usage(a: dict[str, Any], b: dict[str, Any]) -> dict[str, int]:, merged: dict[str, int] = {}, for key in set(a) | set(b):... (10 lines)]
def build_prompt(packet: dict[str, Any], prior: dict[str, Any] | None = None) -> str:
    base = PROMPT_PATH.read_text(encoding="utf-8")
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    prior_block = ""
    if prior:
        prior_block = (
            "\n\nPre-screen signals from deterministic filters (treat as prior; "
            "confirm with the article packet, override if wrong, and explain in evidence):\n"
            + json.dumps(prior, ensure_ascii=False, indent=2)
        )
    return (
        base
        + prior_block
        + "\n\nJSON schema:\n"
        + schema
        + "\n\nArticle packet:\n"
        + json.dumps(packet, ensure_ascii=False)
    )


# [REMOVED: def build_classification_prompt(packet: dict[str, Any]) -> str:, compact = {, "metadata": packet["metadata"],... (250 lines)]
def upsert_destination_sql(candidate: Candidate, revision_id: int | None) -> str:
    image_url = None
    if candidate.page_image_filename:
        image_url = "https://commons.wikimedia.org/wiki/Special:FilePath/" + candidate.page_image_filename.replace(" ", "_")
    source_url = f"https://en.wikivoyage.org/wiki/{candidate.title.replace(' ', '_')}"
    public_name = display_title(candidate.title)
    loc_type = "city"
    if "/" in candidate.title:
        loc_type = "district"
    return f"""
INSERT INTO destinations (
    name, slug, location_type, latitude, longitude, source_urls,
    wikivoyage_page_id, wikivoyage_revision_id, attribution_statement,
    verification_state, confidence_score, researched_by, image_url, updated_at
) VALUES (
    {sql_literal(public_name)}, {sql_literal(candidate.slug)}, {sql_literal(loc_type)},
    {sql_literal(candidate.latitude)}, {sql_literal(candidate.longitude)}, ARRAY[{sql_literal(source_url)}]::text[],
    {candidate.page_id}, {sql_literal(revision_id)},
    'Content derived from Wikivoyage, licensed under CC BY-SA 4.0.',
    'staging', 5, NULL, {sql_literal(image_url)}, NOW()
)
ON CONFLICT (slug) DO UPDATE SET
    latitude = COALESCE(EXCLUDED.latitude, destinations.latitude),
    longitude = COALESCE(EXCLUDED.longitude, destinations.longitude),
    verification_state = CASE WHEN destinations.verification_state = 'rejected' THEN 'staging' ELSE destinations.verification_state END,
    wikivoyage_page_id = COALESCE(EXCLUDED.wikivoyage_page_id, destinations.wikivoyage_page_id),
    wikivoyage_revision_id = COALESCE(EXCLUDED.wikivoyage_revision_id, destinations.wikivoyage_revision_id),
    source_urls = CASE WHEN destinations.source_urls @> EXCLUDED.source_urls THEN destinations.source_urls ELSE destinations.source_urls || EXCLUDED.source_urls END,
    image_url = COALESCE(destinations.image_url, EXCLUDED.image_url),
    updated_at = NOW()
RETURNING id;
"""


PUBLIC_LOCATION_KINDS = {
    "city", "town", "village", "district", "neighborhood", "region", "country", "park",
    "island", "cultural_landscape", "wine_region", "sea_or_lake",
}

DESTINATION_LOCATION_TYPE_MAP = {
    "city": "city",
    "town": "city",
    "village": "city",
    "district": "district",
    "neighborhood": "neighborhood",
    "region": "region",
    "country": "country",
    "park": "park",
    "island": "region",
    "cultural_landscape": "region",
    "wine_region": "region",
    "sea_or_lake": "region",
    "airport": "airport",
    "other": "other",
}


def upsert_classification_from_prior_sql(destination_id: str, prior: dict[str, Any] | None, guide_destination: dict[str, Any] | None = None) -> str:
    if not prior:
        prior = {}
    guide_destination = guide_destination if isinstance(guide_destination, dict) else {}
    article_kind = guide_destination.get("location_type") or prior.get("deterministic_article_kind") or "other"
    parse_strategy = prior.get("deterministic_parse_strategy")
    traveler_relevance = prior.get("deterministic_traveler_relevance") or "primary_destination"
    confidence = prior.get("deterministic_confidence_score") or 6
    evidence = prior.get("deterministic_evidence") or "guide_v2 destination.location_type used for classification."
    if article_kind not in {
        "city", "town", "village", "district", "neighborhood", "region", "country", "park", "island",
        "airport", "itinerary", "travel_topic", "dive_site", "wine_region", "cultural_landscape",
        "route", "sea_or_lake", "other",
    }:
        return ""
    if parse_strategy not in {"full_destination", "limited_destination", "route_or_itinerary", "topic_only", "skip"}:
        parse_strategy = "full_destination" if article_kind in PUBLIC_LOCATION_KINDS else "limited_destination"
    if traveler_relevance not in {"primary_destination", "side_trip", "transit_stop", "special_interest", "low"}:
        traveler_relevance = "primary_destination"
    should_publish = article_kind in PUBLIC_LOCATION_KINDS and parse_strategy in {"full_destination", "limited_destination"}
    location_update = ""
    destination_location_type = DESTINATION_LOCATION_TYPE_MAP.get(article_kind)
    if destination_location_type:
        location_update = f"""
UPDATE destinations
SET location_type = {sql_literal(destination_location_type)}, updated_at = NOW()
WHERE id = {sql_literal(destination_id)}::uuid;
"""
    return f"""
INSERT INTO destination_classification (
    destination_id, article_kind, traveler_relevance, parse_strategy, should_publish,
    source_evidence, confidence_score, updated_at
) VALUES (
    {sql_literal(destination_id)}::uuid,
    {sql_literal(article_kind)},
    {sql_literal(traveler_relevance)},
    {sql_literal(parse_strategy)},
    {sql_literal(should_publish)},
    {sql_literal(str(evidence)[:2000])},
    {sql_literal(confidence)},
    NOW()
)
ON CONFLICT (destination_id) DO UPDATE SET
    article_kind = EXCLUDED.article_kind,
    traveler_relevance = EXCLUDED.traveler_relevance,
    parse_strategy = EXCLUDED.parse_strategy,
    should_publish = EXCLUDED.should_publish,
    source_evidence = EXCLUDED.source_evidence,
    confidence_score = EXCLUDED.confidence_score,
    updated_at = NOW();
{location_update}
"""


def insert_source_document_sql(candidate: Candidate, destination_id: str, wikitext: str, revision_id: int | None) -> str:
    source_url = f"https://en.wikivoyage.org/wiki/{candidate.title.replace(' ', '_')}"
    content_hash = hashlib.sha256(wikitext.encode("utf-8", errors="ignore")).hexdigest()
    return f"""
INSERT INTO source_documents (
    source_type, source_url, title, publisher, scraped_at, language,
    destination_id, content_hash, extracted_text, crawl_method, agent_name,
    license_name, attribution_text, updated_at
) VALUES (
    'wikivoyage', {sql_literal(source_url)}, {sql_literal(candidate.title)}, 'Wikivoyage', NOW(), 'en',
    {sql_literal(destination_id)}::uuid, {sql_literal(content_hash)}, {sql_literal(wikitext[:200000])},
    'local_dump', 'deepseek_importer', 'CC BY-SA 4.0',
    'Content derived from Wikivoyage, licensed under CC BY-SA 4.0.', NOW()
)
ON CONFLICT (source_url, content_hash) DO UPDATE SET
    destination_id = EXCLUDED.destination_id,
    updated_at = NOW()
RETURNING id;
"""


def wikivoyage_listings_sql(destination_id: str, candidate: Candidate, listings: list[dict[str, Any]]) -> str:
    statements: list[str] = []
    for item in listings:
        raw = item.get("raw_template") or {}
        statements.append(f"""
INSERT INTO wikivoyage_listings (
    destination_id, wikivoyage_page_id, listing_uid, listing_type, name, alt,
    address, directions, latitude, longitude, phone, email, website, hours,
    price, content, image, wikidata_qid, raw_template, confidence_score
) VALUES (
    {sql_literal(destination_id)}::uuid, {candidate.page_id}, {sql_literal(item.get('listing_uid'))},
    {sql_literal(item.get('listing_type') or 'listing')}, {sql_literal(raw.get('name') or item.get('name'))},
    {sql_literal(raw.get('alt'))}, {sql_literal(raw.get('address'))}, {sql_literal(raw.get('directions'))},
    {sql_literal(safe_float(raw.get('lat'), -90, 90))}, {sql_literal(safe_float(raw.get('long') or raw.get('lon'), -180, 180))}, {sql_literal(raw.get('phone'))},
    {sql_literal(raw.get('email'))}, {sql_literal(raw.get('url') or raw.get('website'))}, {sql_literal(raw.get('hours'))},
    {sql_literal(raw.get('price'))}, {sql_literal(raw.get('content'))}, {sql_literal(raw.get('image'))},
    {sql_literal(raw.get('wikidata'))}, {sql_literal(raw)}, 9
) ON CONFLICT (destination_id, listing_uid) DO UPDATE SET
    listing_type = EXCLUDED.listing_type,
    name = COALESCE(EXCLUDED.name, wikivoyage_listings.name),
    raw_template = EXCLUDED.raw_template,
    updated_at = NOW();
""")
    return "\n".join(statements)


def begin_run_sql(candidate: Candidate, destination_id: str, wikitext: str, revision_id: int | None, model: str, prompt_version: str, schema_version: str) -> str:
    source_hash = hashlib.sha256(wikitext.encode("utf-8", errors="ignore")).hexdigest()
    return f"""
INSERT INTO wikivoyage_extraction_runs (
    wikivoyage_page_id, destination_id, dump_date, source_revision_id,
    source_sha256, model, prompt_version, schema_version, status
) VALUES (
    {candidate.page_id}, {sql_literal(destination_id)}::uuid, 'latest', {sql_literal(revision_id)},
    {sql_literal(source_hash)}, {sql_literal(model)}, {sql_literal(prompt_version)}, {sql_literal(schema_version)}, 'processing'
)
ON CONFLICT (wikivoyage_page_id, source_sha256, model, prompt_version, schema_version) DO UPDATE SET
    status = 'processing', error_message = NULL
RETURNING id;
"""


def finish_run_sql(run_id: str, status: str, data: dict[str, Any] | None, usage: dict[str, Any] | None, error: str | None = None) -> str:
    input_tokens = usage.get("prompt_tokens") if usage else None
    output_tokens = usage.get("completion_tokens") if usage else None
    # Conservative placeholder. DeepSeek billing can be reconciled from actual usage dashboard later.
    cost = None
    return f"""
UPDATE wikivoyage_extraction_runs SET
    status = {sql_literal(status)},
    input_tokens = {sql_literal(input_tokens)},
    output_tokens = {sql_literal(output_tokens)},
    cost_usd = {sql_literal(cost)},
    raw_output = {sql_literal(data)},
    error_message = {sql_literal(error)},
    completed_at = NOW()
WHERE id = {sql_literal(run_id)}::uuid;
"""


def insert_dynamic_sql(db: PsqlClient, table: str, destination_id: str, obj: dict[str, Any]) -> str | None:
    if not obj:
        return None
    obj = normalize_item(table, obj)
    # Fallback for required NOT NULL fields that the LLM might omit.
    if table == "destination_classification":
        obj.setdefault("article_kind", "other")
        obj.setdefault("should_publish", True)
    # Special-case destination_content_sections: the v2 model output
    # uses the field name ``summary`` (and sometimes ``content``) for
    # the prose body, but the SQL table's column is named ``body``.
    # The setdefault MUST run BEFORE the column filtering below,
    # because once ``summary`` is filtered out (it's not in the
    # table's columns) the fallback to ``row.get("summary")`` is
    # always None and the row gets silently dropped.
    if table == "destination_content_sections":
        obj.setdefault("heading", obj.get("title") or obj.get("section_key", "Section").replace("_", " ").title())
        # The v2 model has historically used several different field
        # names for the prose body depending on the model/prompt
        # version: ``body`` (current), ``summary`` (early v2), ``content``
        # (intermediate), and ``body_text`` (a few ab-* test articles
        # returned by the big-pickle model). Try them all so a model
        # that picks any of these names still produces a row.
        obj.setdefault("body", obj.get("content") or obj.get("summary") or obj.get("body") or obj.get("body_text") or "")
        if not obj.get("section_key") or not obj.get("body"):
            return None
    cols = db.columns(table)
    if not cols:
        return None
    row: dict[str, Any] = {k: v for k, v in obj.items() if k in cols and k not in {"id", "created_at", "updated_at"}}
    row = sanitize_row(row)
    # sanitize_row may add fallback text columns such as ``notes`` after
    # the initial table-column filter. Drop anything the target table
    # still does not have, otherwise one model's extra enum text can
    # abort the whole destination transaction.
    row = {k: v for k, v in row.items() if k in cols and k not in {"id", "created_at", "updated_at"}}
    # sanitize_row may re-add a ``notes`` field as a fallback dump for
    # unrecognised enum values. For destination_water_safety, ``notes``
    # is not a column — the real column is ``tap_water_notes``. Pop
    # ``notes`` here so it does not cause a spurious SQL error.
    if table == "destination_water_safety" and "notes" in row:
        existing_tap = row.get("tap_water_notes", "")
        if existing_tap:
            row["tap_water_notes"] = str(existing_tap) + " " + str(row.pop("notes", ""))
        else:
            row["tap_water_notes"] = row.pop("notes", "")
    if "destination_id" in cols:
        row["destination_id"] = destination_id
    elif "origin_destination_id" in cols:
        row["origin_destination_id"] = destination_id
    else:
        return None
    for required in REQUIRED_COLUMNS.get(table, []):
        if row.get(required) in (None, "", []):
            return None
    columns = list(row.keys())
    values = [sql_literal(row[c], c) for c in columns]
    conflict = " ON CONFLICT DO NOTHING"
    if table in {
        "destination_classification", "destination_practicalities", "destination_cash_access",
        "destination_religion_culture", "destination_water_safety", "destination_accessibility",
        "destination_driving_rules",
    }:
        update_cols = [c for c in columns if c != "destination_id"]
        if update_cols:
            conflict = " ON CONFLICT (destination_id) DO UPDATE SET " + ", ".join(
                f"{c}=EXCLUDED.{c}" for c in update_cols
            )
        else:
            conflict = " ON CONFLICT (destination_id) DO NOTHING"
    elif table == "destination_content_sections":
        conflict = " ON CONFLICT (destination_id, section_key) DO UPDATE SET heading=EXCLUDED.heading, body=EXCLUDED.body, image_url=EXCLUDED.image_url, sort_order=EXCLUDED.sort_order, source_url=EXCLUDED.source_url, updated_at=NOW()"
    return f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(values)}){conflict};"


def sanitize_row(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    # Defensive: if a scalar numeric column somehow received a list
    # (model variation, schema drift), pick the first numeric value
    # and stash the original list in source_text so the data is not
    # silently lost. This catches future regressions before they
    # reach Postgres and produce a "type jsonb vs integer" error.
    for column in NUMERIC_COLUMNS:
        if column in out and isinstance(out[column], list):
            chosen = None
            for item in out[column]:
                try:
                    chosen = float(str(item).replace(",", "").strip())
                    break
                except (TypeError, ValueError):
                    continue
            existing = out.get("source_text") or out.get("notes") or ""
            extra = f"{column} list {out[column]!r} -> {chosen}"
            out["source_text"] = (str(existing) + " " if existing else "") + extra
            out[column] = chosen
    # Defensive: clamp confidence_score and other score columns to
    # the 1-10 range (or NULL) so the *_confidence_score_check
    # constraints don't reject the row when the model returns 0,
    # 11, or some other out-of-range value. The original value is
    # preserved in source_text for auditability. Covers the
    # destination_*.confidence_score,
    # destination_*.backpacker_relevance_score, and similar columns
    # — all use 1-10 per the current schema.
    for column in ("confidence_score", "backpacker_relevance_score",
                   "overall_confidence", "research_confidence_score",
                   "relevance_score", "quality_score", "score",
                   "priority_score"):
        if column not in out or out[column] in (None, ""):
            continue
        try:
            parsed = int(float(str(out[column]).replace(",", "").strip()))
        except (TypeError, ValueError):
            existing = out.get("source_text") or out.get("notes") or ""
            out["source_text"] = (str(existing) + " " if existing else "") + \
                f"{column} not numeric: {out[column]!r}"
            out[column] = None
            continue
        if 1 <= parsed <= 10:
            out[column] = parsed
        else:
            existing = out.get("source_text") or out.get("notes") or ""
            out["source_text"] = (str(existing) + " " if existing else "") + \
                f"{column}={parsed} out of 1-10 range; nulled"
            out[column] = None
    # Defensive: empty currency_code would violate the
    # destination_featured_listings_currency_code_fkey foreign key.
    # The model sometimes returns "" or null when it doesn't know
    # the currency. Set to NULL so the FK is satisfied.
    for column in ("currency_code", "currency", "currency_symbol"):
        if column in out and isinstance(out[column], str) and not out[column].strip():
            out[column] = None
    for column in NUMERIC_COLUMNS:
        if column not in out or out[column] in (None, ""):
            continue
        try:
            parsed = float(str(out[column]).replace(",", "").strip())
        except (TypeError, ValueError):
            existing = out.get("source_text") or out.get("notes") or ""
            out["source_text"] = (str(existing) + " " if existing else "") + f"{column}: {out[column]}"
            out[column] = None
            continue
        if column == "latitude" and not (-90 <= parsed <= 90):
            parsed = None
        elif column == "longitude" and not (-180 <= parsed <= 180):
            parsed = None
        out[column] = parsed
    for column in BOOLEAN_COLUMNS:
        if column not in out or out[column] in (None, ""):
            continue
        if isinstance(out[column], bool):
            continue
        value = str(out[column]).strip().lower().replace(" ", "_").replace("-", "_")
        if value in {"true", "yes", "required", "available", "common"}:
            out[column] = True
        elif value in {"false", "no", "not_required", "unavailable", "none"}:
            out[column] = False
        else:
            existing = out.get("notes") or out.get("source_text") or ""
            out["notes"] = (str(existing) + " " if existing else "") + f"{column}: {out[column]}"
            out[column] = None
    for column, allowed in ENUM_VALUES.items():
        if column not in out or out[column] in (None, ""):
            continue
        value = str(out[column]).strip().lower().replace(" ", "_").replace("-", "_")
        if value in allowed:
            out[column] = value
            continue
        note_column = None
        if column == "atm_availability":
            note_column = "atm_reliability_notes"
        elif column == "exchange_office_availability":
            note_column = "exchange_rate_notes"
        elif column == "coverage_quality":
            note_column = "coverage_notes"
        elif column in {"acceptance_level", "foreign_card_reliability"}:
            note_column = "traveler_advice"
        if note_column:
            existing = out.get(note_column)
            out[note_column] = (str(existing) + " " if existing else "") + str(out[column])
        out[column] = None
    return out


def first_present(obj: dict[str, Any], keys: list[str]) -> Any:
    for key in keys:
        value = obj.get(key)
        if value not in (None, "", []):
            return value
    return None


def normalize_source(obj: dict[str, Any]) -> dict[str, Any]:
    out = dict(obj)
    source = out.pop("source", None) or out.pop("section", None)
    if source and "source_section_key" not in out:
        out["source_section_key"] = str(source).replace(" section", "").replace(" ", "_").lower()
    if "text" in out and "source_text" not in out:
        out["source_text"] = out["text"]
    return out


def normalize_item(table: str, item: dict[str, Any]) -> dict[str, Any]:
    out = normalize_source(item)
    if table == "destination_content_sections":
        out.setdefault("body", first_present(out, ["body", "body_text", "content", "summary", "description"]))
        out.setdefault("heading", first_present(out, ["heading", "title", "section_key"]))
        out.setdefault("sort_order", 0)
    elif table == "destination_day_trips":
        out.setdefault("target_name", first_present(out, ["target_name", "destination", "name", "title"]))
        out.setdefault("summary", first_present(out, ["summary", "description", "details", "notes"]))
        trip_type = str(first_present(out, ["trip_type", "type", "category"]) or "day_trip").lower().replace(" ", "_").replace("-", "_")
        if trip_type in {"overnight", "onward_route", "side_trip", "tour", "other"}:
            out["trip_type"] = trip_type
        elif trip_type in {"city", "town", "village", "nature", "national_park", "attraction", "historical_site", "cultural", "town_visit"}:
            out["trip_type"] = "day_trip"
        else:
            out["trip_type"] = "day_trip"
    elif table == "destination_budget_items":
        out.setdefault("item_name", first_present(out, ["item_name", "item", "title", "name"]))
        category = str(first_present(out, ["category"]) or "misc").lower()
        if category in {"accommodation", "hotel", "hostel", "guesthouse"}:
            category = "sleep"
        elif category in {"transport", "transportation"}:
            category = "transit"
        elif category in {"activity", "attraction", "fee", "permit"}:
            category = "activities"
        elif category not in {"sleep", "food", "transit", "activities", "shopping", "misc"}:
            category = "misc"
        out["category"] = category
        cadence = str(first_present(out, ["cadence", "frequency", "unit"]) or "each").lower().replace(" ", "_").replace("-", "_")
        if cadence in {"each", "hour", "day", "night", "week", "month"}:
            out["cadence"] = cadence
        elif cadence in {"daily", "per_day"}:
            out["cadence"] = "day"
        elif cadence in {"per_night", "nightly"}:
            out["cadence"] = "night"
        elif cadence in {"weekly"}:
            out["cadence"] = "week"
        elif cadence in {"monthly"}:
            out["cadence"] = "month"
        else:
            out["cadence"] = "each"
        if "cost" in out and "source_text" not in out:
            out["source_text"] = f"Cost: {out['cost']}"
    elif table == "destination_money_tips":
        out.setdefault("title", first_present(out, ["title", "tip", "name", "description"]))
        out.setdefault("tip_type", first_present(out, ["tip_type", "type"]) or "other")
        out.setdefault("description", first_present(out, ["description", "details", "notes"]))
    elif table == "destination_permits_fees":
        out.setdefault("fee_or_permit_name", first_present(out, ["fee_or_permit_name", "name", "title", "type"]) or "Permit or fee")
        raw_type = str(first_present(out, ["fee_type", "type"]) or "other").lower()
        out["fee_type"] = "entry_fee" if "entry" in raw_type or "parking" in raw_type or "fee" in raw_type else "other"
        out.setdefault("cost_text", first_present(out, ["cost_text", "amount", "cost", "price"]))
        out.setdefault("notes", first_present(out, ["notes", "details", "description"]))
    elif table == "destination_entry_requirements":
        out.setdefault("title", first_present(out, ["title", "name", "requirement", "details"]) or "Entry requirement")
        raw_type = str(first_present(out, ["requirement_type", "type", "category"]) or "other").lower()
        if "visa" in raw_type:
            out["requirement_type"] = "visa"
        elif "passport" in raw_type:
            out["requirement_type"] = "passport"
        elif "insurance" in raw_type:
            out["requirement_type"] = "insurance"
        elif "vaccine" in raw_type or "vaccination" in raw_type:
            out["requirement_type"] = "vaccination"
        else:
            out["requirement_type"] = "other"
        out.setdefault("details", first_present(out, ["details", "description", "notes", "advice"]))
    elif table == "destination_safety_items":
        out.setdefault("title", first_present(out, ["title", "issue", "risk", "name"]) or "Safety note")
        out.setdefault("description", first_present(out, ["description", "advice", "details", "notes"]))
        out.setdefault("item_type", first_present(out, ["item_type", "type"]) or "other")
    elif table == "destination_health_risks":
        out.setdefault("title", first_present(out, ["title", "risk", "issue", "name"]) or "Health risk")
        out.setdefault("risk_type", first_present(out, ["risk_type", "type"]) or "other")
        out.setdefault("description", first_present(out, ["description", "details", "notes", "advice"]))
    elif table == "destination_connectivity_providers":
        out.setdefault("provider_name", first_present(out, ["provider_name", "name", "provider"]) or "Unknown provider")
        service = first_present(out, ["service_type", "service", "type"])
        service_text = str(service or "physical_sim").lower()
        if service_text in {"4g", "5g", "lte", "3g"}:
            out.setdefault("network_generation", [service_text.upper()])
            out["service_type"] = "physical_sim"
        elif "esim" in service_text:
            out["service_type"] = "esim"
        elif "wifi" in service_text:
            out["service_type"] = "public_wifi"
        else:
            out["service_type"] = "physical_sim"
        out.setdefault("coverage_notes", first_present(out, ["coverage_notes", "notes", "description"]))
    elif table == "destination_internet_access":
        out.setdefault("access_type", first_present(out, ["access_type", "type"]) or "other")
        out.setdefault("speed_reliability_notes", first_present(out, ["notes", "description", "details"]))
    elif table == "destination_power_plugs":
        out.setdefault("plug_type", first_present(out, ["plug_type", "type", "name"]))
        # frequency_hz is an INTEGER column but the model sometimes
        # returns a list like [50, 60] (both Japan and US-style grids
        # in one article). Pick the first numeric value as the
        # primary, and stash the original list in ``notes`` for
        # transparency. If the list has no numeric value at all,
        # null the column out and note that too.
        freq = out.get("frequency_hz")
        if isinstance(freq, list):
            chosen = None
            for item in freq:
                try:
                    chosen = int(item)
                    break
                except (TypeError, ValueError):
                    continue
            extra = f"frequency_hz list {freq}"
            if chosen is not None:
                out["frequency_hz"] = chosen
                extra += f" -> {chosen}"
            else:
                out["frequency_hz"] = None
                extra += " (no numeric value)"
            existing = out.get("notes") or ""
            out["notes"] = (existing + " " if existing else "") + extra
        elif freq in (None, ""):
            out["frequency_hz"] = None
    elif table == "destination_special_interest_details":
        title = first_present(out, ["title", "topic", "name"]) or "Special interest"
        out.setdefault("title", title)
        topic = str(first_present(out, ["interest_type", "topic", "type"]) or "other").lower()
        if "hik" in topic:
            out["interest_type"] = "hiking_area"
        elif "div" in topic:
            out["interest_type"] = "dive_site"
        elif "wine" in topic:
            out["interest_type"] = "wine_region"
        elif "pilgrim" in topic:
            out["interest_type"] = "pilgrimage"
        elif "park" in topic:
            out["interest_type"] = "national_park"
        else:
            out["interest_type"] = "other"
        difficulty = str(first_present(out, ["difficulty_level", "difficulty"]) or "").lower().replace(" ", "_").replace("-", "_")
        if difficulty in {"beginner", "simple"}:
            out["difficulty_level"] = "easy"
        elif difficulty in {"easy", "moderate", "hard", "expert"}:
            out["difficulty_level"] = difficulty
        elif difficulty:
            out["difficulty_level"] = None
        out.setdefault("summary", first_present(out, ["summary", "details", "description", "notes"]))
    elif table == "destination_neighborhoods":
        ntype = str(first_present(out, ["neighborhood_type", "type", "category"]) or "other").lower().replace(" ", "_").replace("-", "_")
        if ntype in {"district", "neighborhood", "suburb", "old_town", "beach_area", "island_area", "station_area", "other"}:
            out["neighborhood_type"] = ntype
        else:
            out["neighborhood_type"] = "other"
    elif table == "destination_events":
        out.setdefault("event_name", first_present(out, ["event_name", "name", "title"]))
        etype = str(first_present(out, ["event_type", "type", "category"]) or "other").lower().replace(" ", "_").replace("-", "_")
        if etype in {"festival", "holiday", "market", "sports", "music", "religious", "seasonal", "cultural", "other"}:
            out["event_type"] = etype
        elif "holiday" in etype:
            out["event_type"] = "holiday"
        elif etype in {"celebration", "heritage", "culture"}:
            out["event_type"] = "cultural"
        else:
            out["event_type"] = "other"
    elif table == "vehicle_rental_options":
        vtype = str(first_present(out, ["vehicle_type", "type", "category"]) or "other").lower().replace(" ", "_").replace("-", "_")
        if vtype in {"car", "scooter", "motorcycle", "bicycle", "e_bike", "campervan", "boat", "other"}:
            out["vehicle_type"] = vtype
        else:
            out["vehicle_type"] = "other"
    elif table == "destination_work_study_volunteer":
        out.setdefault("title", first_present(out, ["title", "name", "opportunity", "description"]) or "Opportunity")
        otype = str(first_present(out, ["opportunity_type", "type", "category"]) or "other").lower().replace(" ", "_").replace("-", "_")
        if otype in {"study", "language_school", "university", "volunteer", "work", "working_holiday", "digital_nomad", "internship", "other"}:
            out["opportunity_type"] = otype
        elif "meditation" in otype or "monastic" in otype or "retreat" in otype:
            out["opportunity_type"] = "study"
        else:
            out["opportunity_type"] = "other"
        out.setdefault("description", first_present(out, ["description", "details", "notes"]))
    elif table == "destination_source_snippets":
        out.setdefault("snippet", first_present(out, ["snippet", "text", "source_text"]) or "")
        out.setdefault("target_table", "wikivoyage_extraction_runs")
    elif table == "destination_practicalities":
        if "getting_there_transport" in out and "orientation_summary" not in out:
            out["orientation_summary"] = out["getting_there_transport"]
        if "currency" in out and "money_summary" not in out:
            out["money_summary"] = f"Currency: {out['currency']}"
    elif table == "destination_cash_access":
        if "atms_nearby" in out and "atm_reliability_notes" not in out:
            out["atm_reliability_notes"] = out["atms_nearby"]
    elif table == "destination_driving_rules":
        out.setdefault("road_quality_notes", first_present(out, ["road_quality_notes", "notes", "description"]))
    elif table == "destination_water_safety":
        if "notes" in out and "tap_water_notes" not in out:
            out["tap_water_notes"] = out.pop("notes")
    return out


def build_load_sql(db: PsqlClient, destination_id: str, source_document_id: str, data: dict[str, Any]) -> str:
    statements: list[str] = []
    for json_key, (table, is_array) in TOP_LEVEL_TABLES.items():
        value = data.get(json_key)
        if not value:
            continue
        items = value if is_array else [value]
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item = dict(item)
            if table == "destination_source_snippets":
                item.setdefault("source_document_id", source_document_id)
            stmt = insert_dynamic_sql(db, table, destination_id, item)
            if stmt:
                statements.append(stmt)

    # featured_listings — dict by category (sleep, eat, see, do, buy).
    # The v1 prompt returns this as a top-level dict; the lane worker
    # loader needs to split it per category so it lands in the
    # destination_featured_listings table.
    fl = data.get("featured_listings")
    if isinstance(fl, dict):
        for cat in ("sleep", "eat", "see", "do", "buy"):
            items = fl.get(cat)
            if isinstance(items, list) and items:
                statements.append(replace_featured_listings_v2_sql(destination_id, cat, items))

    # prose_sections — the main guide content for public display.
    # Written to destination_prose_sections so the public API
    # reads it directly (no merge needed).
    ps = data.get("prose_sections")
    if isinstance(ps, list):
        for i, sec in enumerate(ps):
            if not isinstance(sec, dict):
                continue
            key = (sec.get("section_key") or "").strip()
            if not key:
                continue
            body_val = (sec.get("body") or sec.get("summary") or sec.get("body_text") or sec.get("content") or "").strip()
            if not body_val:
                continue
            statements.append(upsert_prose_section_sql(
                destination_id,
                key,
                (sec.get("heading") or key.replace("_", " ").title()).strip(),
                clean_llm_text(body_val),
                i,
                sec.get("source_text"),
                sec.get("confidence_score"),
            ))

    # practical_notes — list of {topic, body, confidence_score} or
    # dict of topic → body. The v1 prompt returns a list; the old
    # v1 pipeline used a dict. Accept both.
    pn = data.get("practical_notes")
    if isinstance(pn, list):
        for note in pn:
            if not isinstance(note, dict):
                continue
            topic = (note.get("topic") or "").strip()
            body = (note.get("body") or "").strip()
            if topic and body:
                statements.append(
                    upsert_practical_note_sql(
                        destination_id, topic,
                        clean_llm_text(body),
                        note.get("confidence_score")
                    )
                )
    elif isinstance(pn, dict):
        for topic, body in pn.items():
            if body and isinstance(body, str):
                statements.append(
                    upsert_practical_note_sql(
                        destination_id, str(topic),
                        clean_llm_text(body),
                        None
                    )
                )

    return "\n".join(statements)


def upsert_prose_section_sql(destination_id: str, section_key: str, heading: str, body: str, sort_order: int, source_text: str | None, confidence_score: int | None) -> str:
    body_sql = sql_literal(body)
    heading_sql = sql_literal(heading)
    return f"""
INSERT INTO destination_prose_sections (destination_id, section_key, heading, body, sort_order, source_text, confidence_score)
VALUES ({sql_literal(destination_id)}::uuid, {sql_literal(section_key)}, {heading_sql}, {body_sql}, {sort_order}, {sql_literal(source_text)}, {sql_literal(confidence_score)})
ON CONFLICT (destination_id, section_key) DO UPDATE SET
    heading = EXCLUDED.heading,
    body = EXCLUDED.body,
    sort_order = EXCLUDED.sort_order,
    source_text = EXCLUDED.source_text,
    confidence_score = EXCLUDED.confidence_score,
    updated_at = now();
"""


def upsert_practical_facts_sql(destination_id: str, facts: dict[str, Any]) -> str:
    cols = ["destination_id", "visa", "money", "power", "language", "safety", "connectivity", "updated_at"]
    keys = cols[1:-1]
    values_sql = ", ".join([sql_literal(facts.get(k)) for k in keys])
    set_clauses = [f"{k} = EXCLUDED.{k}" for k in keys]
    return f"""
INSERT INTO destination_practical_facts ({", ".join(cols)})
VALUES ({sql_literal(destination_id)}::uuid, {values_sql}, now())
ON CONFLICT (destination_id) DO UPDATE SET
    {", ".join(set_clauses)},
    updated_at = now();
"""


def upsert_guide_meta_sql(destination_id: str, destination: dict[str, Any], quality: dict[str, Any] | None = None) -> str:
    suggested = destination.get("suggested_stay") if isinstance(destination.get("suggested_stay"), dict) else {}
    confidence = (quality or {}).get("overall_confidence")
    review_reasons = (quality or {}).get("needs_review_reasons") or []
    review_state = "needs_review" if review_reasons or (isinstance(confidence, int) and confidence < 6) else "ai_generated"
    return f"""
INSERT INTO destination_guide_meta (
    destination_id, tagline, best_for_tags, min_nights, ideal_nights, suggested_stay_note,
    confidence_score, review_state, updated_at
) VALUES (
    {sql_literal(destination_id)}::uuid,
    {sql_literal(clean_llm_text(destination.get('tagline')))},
    {sql_literal(destination.get('best_for_tags') or [], 'best_for_tags')},
    {sql_literal(suggested.get('min_nights'))},
    {sql_literal(suggested.get('ideal_nights'))},
    {sql_literal(suggested.get('note'))},
    {sql_literal(confidence)},
    {sql_literal(review_state)},
    now()
)
ON CONFLICT (destination_id) DO UPDATE SET
    tagline = CASE WHEN 'tagline' = ANY(destination_guide_meta.locked_fields) THEN destination_guide_meta.tagline ELSE EXCLUDED.tagline END,
    best_for_tags = CASE WHEN 'best_for_tags' = ANY(destination_guide_meta.locked_fields) THEN destination_guide_meta.best_for_tags ELSE EXCLUDED.best_for_tags END,
    min_nights = CASE WHEN 'min_nights' = ANY(destination_guide_meta.locked_fields) THEN destination_guide_meta.min_nights ELSE EXCLUDED.min_nights END,
    ideal_nights = CASE WHEN 'ideal_nights' = ANY(destination_guide_meta.locked_fields) THEN destination_guide_meta.ideal_nights ELSE EXCLUDED.ideal_nights END,
    suggested_stay_note = CASE WHEN 'suggested_stay_note' = ANY(destination_guide_meta.locked_fields) THEN destination_guide_meta.suggested_stay_note ELSE EXCLUDED.suggested_stay_note END,
    confidence_score = EXCLUDED.confidence_score,
    review_state = CASE WHEN destination_guide_meta.review_state = 'user_edited' THEN destination_guide_meta.review_state ELSE EXCLUDED.review_state END,
    parser_version = 'guide_v2',
    updated_at = now();
"""


def upsert_canonical_fact_guesses_sql(destination_id: str, guesses: dict[str, Any], country_code_guess: str | None, quality: dict[str, Any] | None = None) -> str:
    confidence = (quality or {}).get("overall_confidence")
    return f"""
INSERT INTO destination_canonical_facts (
    destination_id, country_iso2, currency_code, language_codes, timezone_ids, plug_types,
    voltage, frequency_hz, tap_water_status, cash_needed_level, sim_card_level,
    llm_suggested, confidence_score, updated_at
) VALUES (
    {sql_literal(destination_id)}::uuid,
    {sql_literal(country_code_guess)},
    {sql_literal(guesses.get('currency_code'))},
    {sql_literal(guesses.get('language_codes') or [], 'language_codes')},
    {sql_literal(guesses.get('timezone_ids') or [], 'timezone_ids')},
    {sql_literal(guesses.get('plug_types') or [], 'plug_types')},
    {sql_literal(guesses.get('voltage'))},
    {sql_literal(guesses.get('frequency_hz'))},
    {sql_literal(guesses.get('tap_water_status'))},
    {sql_literal(guesses.get('cash_needed_level'))},
    {sql_literal(guesses.get('sim_card_level'))},
    {sql_literal(guesses)},
    {sql_literal(confidence)},
    now()
)
ON CONFLICT (destination_id) DO UPDATE SET
    country_iso2 = COALESCE(destination_canonical_facts.country_iso2, EXCLUDED.country_iso2),
    currency_code = COALESCE(destination_canonical_facts.currency_code, EXCLUDED.currency_code),
    language_codes = CASE WHEN destination_canonical_facts.locked_fields @> ARRAY['language_codes']::text[] THEN destination_canonical_facts.language_codes ELSE EXCLUDED.language_codes END,
    timezone_ids = CASE WHEN destination_canonical_facts.locked_fields @> ARRAY['timezone_ids']::text[] THEN destination_canonical_facts.timezone_ids ELSE EXCLUDED.timezone_ids END,
    plug_types = CASE WHEN destination_canonical_facts.locked_fields @> ARRAY['plug_types']::text[] THEN destination_canonical_facts.plug_types ELSE EXCLUDED.plug_types END,
    voltage = COALESCE(destination_canonical_facts.voltage, EXCLUDED.voltage),
    frequency_hz = COALESCE(destination_canonical_facts.frequency_hz, EXCLUDED.frequency_hz),
    tap_water_status = COALESCE(destination_canonical_facts.tap_water_status, EXCLUDED.tap_water_status),
    cash_needed_level = COALESCE(destination_canonical_facts.cash_needed_level, EXCLUDED.cash_needed_level),
    sim_card_level = COALESCE(destination_canonical_facts.sim_card_level, EXCLUDED.sim_card_level),
    llm_suggested = EXCLUDED.llm_suggested,
    confidence_score = EXCLUDED.confidence_score,
    updated_at = now();
"""


def upsert_practical_note_sql(destination_id: str, topic: str, body: str, confidence_score: int | None = None) -> str:
    return f"""
INSERT INTO destination_practical_notes (destination_id, topic, body, confidence_score, parser_version, updated_at)
VALUES ({sql_literal(destination_id)}::uuid, {sql_literal(topic)}, {sql_literal(body)}, {sql_literal(confidence_score)}, 'guide_v2', now())
ON CONFLICT (destination_id, topic) DO UPDATE SET
    body = CASE WHEN destination_practical_notes.locked THEN destination_practical_notes.body ELSE EXCLUDED.body END,
    confidence_score = EXCLUDED.confidence_score,
    updated_at = now();
"""


def replace_featured_listings_sql(destination_id: str, category: str, items: list[dict[str, Any]]) -> str:
    delete = f"DELETE FROM destination_featured_listings WHERE destination_id = {sql_literal(destination_id)}::uuid AND category = {sql_literal(category)};\n"
    inserts = []
    for i, item in enumerate(items):
        name = item.get("name") or ""
        desc = item.get("description")
        price = item.get("price_text")
        if not name:
            continue
        inserts.append(
            f"""INSERT INTO destination_featured_listings (destination_id, category, name, description, price_text, sort_order) VALUES ({sql_literal(destination_id)}::uuid, {sql_literal(category)}, {sql_literal(name)}, {sql_literal(desc)}, {sql_literal(price)}, {i});"""
        )
    return delete + "\n".join(inserts)


PRICE_CURRENCY_MARKERS: dict[str, tuple[str, ...]] = {
    "USD": ("US$", "USD", "U.S.$"),
    "EUR": ("€", "EUR"),
    "GBP": ("£", "GBP"),
    "THB": ("THB", "baht"),
    "VND": ("VND", "dong", "đ", "₫"),
    "CAD": ("CAD", "C$"),
    "AUD": ("AUD", "A$"),
}


def has_nonlocal_currency_marker(price_text: str | None, currency_code: str | None) -> bool:
    if not price_text or not currency_code:
        return False
    text = price_text.upper()
    local = currency_code.upper()
    for code, markers in PRICE_CURRENCY_MARKERS.items():
        if code == local:
            continue
        if any(marker.upper() in text for marker in markers):
            return True
    return False


def has_specific_price_text(price_text: str | None) -> bool:
    if not price_text:
        return False
    text = price_text.strip().lower()
    if re.search(r"\d", text):
        return True
    return "free" in text


def replace_featured_listings_v2_sql(destination_id: str, category: str, items: list[dict[str, Any]]) -> str:
    delete = (
        f"DELETE FROM destination_featured_listings "
        f"WHERE destination_id = {sql_literal(destination_id)}::uuid "
        f"AND category = {sql_literal(category)} AND locked = false;\n"
    )
    inserts = []
    for i, item in enumerate(items):
        name = item.get("name") or ""
        if not name:
            continue
        price_text = item.get("price_text_local") or item.get("price_text")
        has_price = bool(has_specific_price_text(price_text) or item.get("amount_local_low") is not None or item.get("amount_local_high") is not None)
        if price_text and not has_specific_price_text(price_text) and item.get("amount_local_low") is None and item.get("amount_local_high") is None:
            price_text = None
        currency_code = item.get("currency_code") if has_price else None
        if has_nonlocal_currency_marker(price_text, currency_code):
            price_text = None
            item["amount_local_low"] = None
            item["amount_local_high"] = None
            currency_code = None
            has_price = False
        has_source_uid = bool(item.get("source_listing_uid"))
        review_state = "ai_generated" if has_price and has_source_uid else "needs_review"
        inserts.append(f"""
INSERT INTO destination_featured_listings (
    destination_id, category, name, description, price_text, price_text_local,
    amount_local_low, amount_local_high, currency_code, price_period, tags, area,
    source_listing_uid, wikidata_qid, sort_order, parser_version, confidence_score, review_state
) VALUES (
    {sql_literal(destination_id)}::uuid,
    {sql_literal(category)},
    {sql_literal(name)},
    {sql_literal(clean_llm_text(item.get('description')))},
    {sql_literal(price_text)},
    {sql_literal(price_text)},
    {sql_literal(item.get('amount_local_low'))},
    {sql_literal(item.get('amount_local_high'))},
    {sql_literal(currency_code)},
    {sql_literal(item.get('price_period'))},
    {sql_literal(item.get('tags') or [], 'tags')},
    {sql_literal(item.get('area'))},
    {sql_literal(item.get('source_listing_uid'))},
    {sql_literal(item.get('wikidata_qid'))},
    {i},
    'guide_v2',
    {sql_literal(item.get('confidence_score'))},
    {sql_literal(review_state)}
);
""")
    return delete + "\n".join(inserts)


def enrich_guide_v2_quality(data: dict[str, Any]) -> dict[str, Any]:
    quality = data.get("quality") if isinstance(data.get("quality"), dict) else {}
    reasons = list(quality.get("needs_review_reasons") or [])

    prose = data.get("prose_sections") if isinstance(data.get("prose_sections"), list) else []
    expected_keys = ["why_go", "getting_in", "getting_around", "when_to_go"]
    found_keys = {str(sec.get("section_key") or "") for sec in prose if isinstance(sec, dict)}
    if found_keys != set(expected_keys):
        reasons.append("missing_or_extra_required_prose_sections")
    if any(len(str(sec.get("body") or "")) < 140 for sec in prose if isinstance(sec, dict)):
        reasons.append("thin_prose_section")

    listings = data.get("featured_listings") if isinstance(data.get("featured_listings"), dict) else {}
    listing_items = [item for cat in ("sleep", "eat", "see", "do", "buy") for item in (listings.get(cat) or []) if isinstance(item, dict)]
    if len(listing_items) < 8:
        reasons.append("too_few_featured_listings")
    priced = [item for item in listing_items if item.get("price_text_local") or item.get("price_text") or item.get("amount_local_low") is not None or item.get("amount_local_high") is not None]
    if listing_items and len(priced) / len(listing_items) < 0.25:
        reasons.append("low_listing_price_coverage")

    trips = data.get("nearby_trips") or data.get("day_trips") or []
    if not trips:
        reasons.append("no_nearby_trips_or_onward_routes")

    if reasons:
        quality["needs_review_reasons"] = sorted(set(str(r) for r in reasons if r))
    data["quality"] = quality
    return quality


def upsert_day_trip_sql(destination_id: str, target_name: str, summary: str, duration_text: str, transport_modes: str, cost_text: str, source_text: str | None) -> str:
    # transport_modes is text[] — wrap a comma-separated value in array literal
    modes_array = "{" + (transport_modes or "").replace('"', '') + "}"
    return f"""
INSERT INTO destination_day_trips (origin_destination_id, target_name, trip_type, summary, duration_text, transport_modes, cost_text, source_text, source_section_key, confidence_score)
VALUES ({sql_literal(destination_id)}::uuid, {sql_literal(target_name)}, 'day_trip', {sql_literal(summary)}, {sql_literal(duration_text)}, {sql_literal(modes_array)}::text[], {sql_literal(cost_text)}, {sql_literal(source_text)}, NULL, NULL)
ON CONFLICT (origin_destination_id, target_name) DO UPDATE SET
    trip_type = EXCLUDED.trip_type,
    summary = EXCLUDED.summary,
    duration_text = EXCLUDED.duration_text,
    transport_modes = EXCLUDED.transport_modes,
    cost_text = EXCLUDED.cost_text,
    source_text = EXCLUDED.source_text,
    updated_at = now();
"""


def upsert_nearby_trip_v2_sql(destination_id: str, trip: dict[str, Any]) -> str:
    name = (trip.get("target_name") or "").strip()
    transport_modes = trip.get("transport_modes") or ""
    modes_array = "{" + str(transport_modes).replace('"', '').replace(", ", ",") + "}"
    cost_text = trip.get("cost_text_local") or trip.get("cost_text")
    return f"""
INSERT INTO destination_day_trips (
    origin_destination_id, target_name, trip_type, trip_kind, summary, distance_text,
    duration_text, transport_modes, cost_text, amount_local_low, amount_local_high,
    currency_code, tags, source_text, source_section_key, confidence_score, parser_version
) VALUES (
    {sql_literal(destination_id)}::uuid,
    {sql_literal(name)},
    'day_trip',
    {sql_literal(trip.get('trip_kind') or 'unknown')},
    {sql_literal(clean_llm_text(trip.get('summary')))},
    {sql_literal(trip.get('distance_text'))},
    {sql_literal((trip.get('duration_text') or '').strip())},
    {sql_literal(modes_array)}::text[],
    {sql_literal(cost_text)},
    {sql_literal(trip.get('amount_local_low'))},
    {sql_literal(trip.get('amount_local_high'))},
    {sql_literal(trip.get('currency_code'))},
    {sql_literal(trip.get('tags') or [], 'tags')},
    {sql_literal(trip.get('source_text'))},
    NULL,
    {sql_literal(trip.get('confidence_score'))},
    'guide_v2'
)
ON CONFLICT (origin_destination_id, target_name) DO UPDATE SET
    trip_kind = CASE WHEN destination_day_trips.locked THEN destination_day_trips.trip_kind ELSE EXCLUDED.trip_kind END,
    summary = CASE WHEN destination_day_trips.locked THEN destination_day_trips.summary ELSE EXCLUDED.summary END,
    distance_text = CASE WHEN destination_day_trips.locked THEN destination_day_trips.distance_text ELSE EXCLUDED.distance_text END,
    duration_text = CASE WHEN destination_day_trips.locked THEN destination_day_trips.duration_text ELSE EXCLUDED.duration_text END,
    transport_modes = CASE WHEN destination_day_trips.locked THEN destination_day_trips.transport_modes ELSE EXCLUDED.transport_modes END,
    cost_text = CASE WHEN destination_day_trips.locked THEN destination_day_trips.cost_text ELSE EXCLUDED.cost_text END,
    amount_local_low = EXCLUDED.amount_local_low,
    amount_local_high = EXCLUDED.amount_local_high,
    currency_code = EXCLUDED.currency_code,
    tags = EXCLUDED.tags,
    source_text = EXCLUDED.source_text,
    parser_version = 'guide_v2',
    updated_at = now();
"""
