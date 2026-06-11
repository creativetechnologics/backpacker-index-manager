#!/usr/bin/env python3
"""Prefill destinations from llm_ready_places and optionally clear stale parser fills."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import deepseek_importer as di
from deepseek_importer import PsqlClient, display_title, load_config, sql_literal

ROOT = Path(__file__).resolve().parent
READY_PATH = ROOT / "llm_ready_places.jsonl"
REST_CACHE = ROOT / "reference_cache" / "restcountries-all.json"


def load_rows(limit: int = 0) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with READY_PATH.open(encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            rows.append(json.loads(line))
            if limit and len(rows) >= limit:
                break
    return rows


def country_names() -> set[str]:
    if not REST_CACHE.exists():
        return set()
    countries = json.loads(REST_CACHE.read_text(encoding="utf-8"))
    names: set[str] = set()
    for row in countries:
        name = row.get("name") or {}
        if name.get("common"):
            names.add(name["common"])
        if name.get("official"):
            names.add(name["official"])
    names.update({"Georgia (country)", "Iran", "Russia", "Laos", "Vietnam", "Bahamas", "Micronesia"})
    return names


def infer_location(row: dict[str, Any], countries: set[str]) -> tuple[str | None, str | None, str | None]:
    title = row.get("title") or ""
    chain = ((row.get("_filter_debug") or {}).get("chain") or [])
    if title in countries:
        return title, None, chain[-1] if chain else None
    country = None
    country_idx = None
    for i, item in enumerate(chain):
        if item in countries:
            country = item
            country_idx = i
            break
    region = chain[country_idx - 1] if country_idx and country_idx > 0 else (chain[0] if chain else None)
    continent = chain[country_idx + 1] if country_idx is not None and country_idx + 1 < len(chain) else (chain[-1] if chain else None)
    return country, region, continent


def loc_type(title: str, countries: set[str] | None = None) -> str:
    if countries and title in countries:
        return "country"
    if "/" in title:
        return "district"
    lower = title.lower()
    if "national park" in lower or lower.endswith(" park"):
        return "park"
    return "city"


def source_url(title: str) -> str:
    return "https://en.wikivoyage.org/wiki/" + title.replace(" ", "_")


def prefill_sql(rows: list[dict[str, Any]]) -> str:
    countries = country_names()
    statements = ["BEGIN;"]
    for row in rows:
        title = row.get("title") or ""
        country, region, continent = infer_location(row, countries)
        image = None
        if row.get("page_image_filename"):
            image = "https://commons.wikimedia.org/wiki/Special:FilePath/" + str(row["page_image_filename"]).replace(" ", "_")
        statements.append(f"""
INSERT INTO destinations (
    name, slug, location_type, latitude, longitude, country, region, continent,
    source_urls, wikivoyage_page_id, attribution_statement, verification_state,
    confidence_score, image_url, updated_at
) SELECT
    {sql_literal(display_title(title))},
    {sql_literal(row.get('slug'))},
    {sql_literal(loc_type(title, countries))},
    {sql_literal(row.get('latitude'))},
    {sql_literal(row.get('longitude'))},
    {sql_literal(country)},
    {sql_literal(region)},
    {sql_literal(continent)},
    ARRAY[{sql_literal(source_url(title))}]::text[],
    {sql_literal(row.get('page_id'))},
    'Content adapted from Wikivoyage (CC BY-SA 4.0)',
    'staging',
    5,
    {sql_literal(image)},
    now()
WHERE NOT EXISTS (
    SELECT 1 FROM destinations existing
    WHERE existing.wikivoyage_page_id = {sql_literal(row.get('page_id'))}
      AND existing.slug <> {sql_literal(row.get('slug'))}
)
ON CONFLICT (slug) DO UPDATE SET
    wikivoyage_page_id = COALESCE(destinations.wikivoyage_page_id, EXCLUDED.wikivoyage_page_id),
    location_type = CASE WHEN destinations.location_type = 'city' AND EXCLUDED.location_type = 'country' THEN 'country' ELSE destinations.location_type END,
    latitude = COALESCE(destinations.latitude, EXCLUDED.latitude),
    longitude = COALESCE(destinations.longitude, EXCLUDED.longitude),
    country = COALESCE(destinations.country, EXCLUDED.country),
    region = COALESCE(destinations.region, EXCLUDED.region),
    continent = COALESCE(destinations.continent, EXCLUDED.continent),
    source_urls = CASE WHEN destinations.source_urls @> EXCLUDED.source_urls THEN destinations.source_urls ELSE destinations.source_urls || EXCLUDED.source_urls END,
    image_url = COALESCE(destinations.image_url, EXCLUDED.image_url),
    updated_at = now();
""")
    statements.append("COMMIT;")
    return "\n".join(statements)


def parent_sql(rows: list[dict[str, Any]]) -> str:
    statements = ["BEGIN;"]
    for row in rows:
        parent_id = row.get("parent_page_id")
        if not parent_id:
            continue
        statements.append(f"""
UPDATE destinations child
SET parent_id = parent.id, updated_at = now()
FROM destinations parent
WHERE child.wikivoyage_page_id = {sql_literal(row.get('page_id'))}
  AND parent.wikivoyage_page_id = {sql_literal(parent_id)}
  AND child.parent_id IS NULL;
""")
    statements.append("COMMIT;")
    return "\n".join(statements)


def cleanup_sql() -> str:
    # Only old parser/display fills. Preserve guide_v2, canonical/reference facts,
    # source docs, and run history.
    return """
BEGIN;
CREATE TEMP TABLE stale_wv_dest AS
SELECT d.id
FROM destinations d
LEFT JOIN destination_guide_meta gm ON gm.destination_id = d.id AND gm.parser_version = 'guide_v2'
WHERE d.wikivoyage_page_id IS NOT NULL
  AND gm.destination_id IS NULL;

DELETE FROM accommodations WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM local_transit WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM food_suggestions WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM activities WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM seasonal_weather WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_content_sections WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_practicalities WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_neighborhoods WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_safety_items WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_health_risks WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_water_safety WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_accessibility WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_payment_methods WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_cash_access WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_money_tips WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_connectivity_providers WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_internet_access WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_power_plugs WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_language_notes WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_religion_culture WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_etiquette_items WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_legal_notes WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_emergency_services WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_permits_fees WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_entry_requirements WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_driving_rules WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM vehicle_rental_options WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_apps WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_budget_items WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_events WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM seasonal_warnings WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_day_trips WHERE origin_destination_id IN (SELECT id FROM stale_wv_dest) AND locked = false;
DELETE FROM destination_traveler_group_notes WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_news_items WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_travel_advisories WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_prose_sections WHERE destination_id IN (SELECT id FROM stale_wv_dest) AND locked = false;
DELETE FROM destination_practical_facts WHERE destination_id IN (SELECT id FROM stale_wv_dest);
DELETE FROM destination_featured_listings WHERE destination_id IN (SELECT id FROM stale_wv_dest) AND locked = false;
DELETE FROM destination_practical_notes WHERE destination_id IN (SELECT id FROM stale_wv_dest) AND locked = false;
DELETE FROM destination_guide_meta WHERE destination_id IN (SELECT id FROM stale_wv_dest) AND parser_version IS DISTINCT FROM 'guide_v2';
DELETE FROM wikivoyage_listings WHERE destination_id IN (SELECT id FROM stale_wv_dest);

UPDATE destinations
SET overview = NULL,
    daily_cost = NULL,
    best_season = NULL,
    rainy_season = NULL,
    visa_info = NULL,
    budget_tier = NULL,
    tag = NULL,
    best_months = NULL,
    updated_at = now()
WHERE id IN (SELECT id FROM stale_wv_dest);
COMMIT;
"""


def db_client(target: str) -> PsqlClient:
    config = load_config()
    psql = config.get(f"{target}_psql") or (di.DEFAULT_STAGING_PSQL if target == "staging" else di.DEFAULT_LOCAL_PSQL)
    return PsqlClient(psql, target)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db-target", choices=["local", "staging"], default="staging")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--execute", action="store_true")
    p.add_argument("--cleanup-stale", action="store_true")
    p.add_argument("--sql-out", default="")
    args = p.parse_args()

    rows = load_rows(args.limit)
    sql = prefill_sql(rows) + "\n" + parent_sql(rows)
    if args.cleanup_stale:
        sql += "\n" + cleanup_sql()
    if args.sql_out:
        Path(args.sql_out).write_text(sql, encoding="utf-8")
    print(f"Prepared prefill rows={len(rows)} cleanup_stale={args.cleanup_stale} execute={args.execute}")
    if not args.execute:
        print("Dry run only. Pass --execute to write DB.")
        return 0
    db_client(args.db_target).run(sql)
    print("Prefill complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
