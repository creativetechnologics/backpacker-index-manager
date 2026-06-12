from __future__ import annotations

import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from pydantic import BaseModel, Field

import db_writer
import deepseek_importer as di


class ManualFareImportRequest(BaseModel):
    content: str = Field(default="", description="CSV content with one fare observation per row")
    dry_run: bool = False


REQUIRED_COLUMNS = {
    "source_key",
    "source_name",
    "origin_iata",
    "destination_iata",
    "travel_month",
    "cabin",
    "currency",
    "cash_typical",
    "observed_at",
    "confidence_score",
}


def _db() -> di.PsqlClient:
    return di.PsqlClient(db_writer._staging_psql_command(), "staging")


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _iata(value: Any) -> str | None:
    text = _clean(value).upper()
    if len(text) == 3 and text.isalpha():
        return text
    return None


def _int(value: Any) -> int | None:
    try:
        return int(_clean(value))
    except (TypeError, ValueError):
        return None


def _money(value: Any) -> Decimal | None:
    text = _clean(value).replace(",", "")
    if not text:
        return None
    try:
        amount = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if amount < 0:
        return None
    return amount


def _iso_observed_at(value: Any) -> str | None:
    text = _clean(value)
    if not text:
        return datetime.now(timezone.utc).isoformat()
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).astimezone(timezone.utc).isoformat()
    except ValueError:
        return None


def _observation_key(row: dict[str, Any]) -> str:
    payload = "|".join([
        row["source_key"], row["origin_iata"], row["destination_iata"],
        str(row.get("depart_date") or ""), str(row.get("return_date") or ""),
        str(row["travel_month"]), row["cabin"], row["currency"],
        str(row.get("cash_low") or ""), str(row["cash_typical"]), str(row.get("cash_high") or ""),
        row["observed_at"],
    ])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_rows(content: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    text = content.strip("\ufeff\n\r ")
    if not text:
        return [], [{"row": 0, "error": "CSV content is empty"}]

    reader = csv.DictReader(io.StringIO(text))
    headers = {h.strip() for h in (reader.fieldnames or []) if h}
    missing = sorted(REQUIRED_COLUMNS - headers)
    if missing:
        return [], [{"row": 0, "error": f"Missing required columns: {', '.join(missing)}"}]

    valid: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, raw in enumerate(reader, start=2):
        source_key = _clean(raw.get("source_key")) or "manual_fare_baseline"
        source_name = _clean(raw.get("source_name")) or "Manual fare baseline"
        origin = _iata(raw.get("origin_iata"))
        dest = _iata(raw.get("destination_iata"))
        month = _int(raw.get("travel_month"))
        confidence = _int(raw.get("confidence_score"))
        currency = _clean(raw.get("currency")).upper()
        cabin = (_clean(raw.get("cabin")) or "economy").lower()
        observed_at = _iso_observed_at(raw.get("observed_at"))
        cash_low = _money(raw.get("cash_low"))
        cash_typical = _money(raw.get("cash_typical"))
        cash_high = _money(raw.get("cash_high"))

        row_errors = []
        if not origin: row_errors.append("origin_iata must be a 3-letter IATA code")
        if not dest: row_errors.append("destination_iata must be a 3-letter IATA code")
        if origin and dest and origin == dest: row_errors.append("origin_iata and destination_iata must differ")
        if not month or month < 1 or month > 12: row_errors.append("travel_month must be 1-12")
        if not currency or len(currency) != 3: row_errors.append("currency must be a 3-letter code")
        if cash_typical is None: row_errors.append("cash_typical is required and must be numeric")
        if confidence is None or confidence < 1 or confidence > 10: row_errors.append("confidence_score must be 1-10")
        if observed_at is None: row_errors.append("observed_at must be ISO-8601")

        if row_errors:
            errors.append({"row": index, "error": "; ".join(row_errors), "raw": raw})
            continue

        normalized = {
            "source_key": source_key,
            "source_name": source_name,
            "source_url": _clean(raw.get("source_url")) or None,
            "origin_iata": origin,
            "destination_iata": dest,
            "travel_month": month,
            "cabin": cabin,
            "currency": currency,
            "cash_low": cash_low,
            "cash_typical": cash_typical,
            "cash_high": cash_high,
            "observed_at": observed_at,
            "confidence_score": confidence,
            "notes": _clean(raw.get("notes")) or None,
        }
        normalized["observation_key"] = _observation_key(normalized)
        valid.append(normalized)

    return valid, errors


def preview_manual_import(req: ManualFareImportRequest) -> dict[str, Any]:
    rows, errors = _parse_rows(req.content)
    return {
        "ok": len(errors) == 0,
        "valid_count": len(rows),
        "invalid_count": len(errors),
        "errors": errors[:25],
        "sample": rows[:5],
    }


def _sql_decimal(value: Decimal | None) -> str:
    return "NULL" if value is None else str(value)


def _source_sql(row: dict[str, Any]) -> str:
    return f"""
INSERT INTO route_price_sources (source_key, source_name, source_type, source_url, display_name, confidence_base, notes)
VALUES ({di.sql_literal(row['source_key'])}, {di.sql_literal(row['source_name'])}, 'manual', {di.sql_literal(row.get('source_url'))}, {di.sql_literal(row['source_name'])}, {int(row['confidence_score'])}, 'Manual fare baseline imported by Manager')
ON CONFLICT (source_key) DO UPDATE SET
  source_name = EXCLUDED.source_name,
  source_url = COALESCE(EXCLUDED.source_url, route_price_sources.source_url),
  display_name = EXCLUDED.display_name,
  updated_at = now();
"""


def _observation_sql(row: dict[str, Any]) -> str:
    return f"""
INSERT INTO flight_fare_observations (
  observation_key, source_id, origin_iata, destination_iata, travel_month, cabin,
  currency, cash_low, cash_typical, cash_high, cash_usd_low, cash_usd_typical, cash_usd_high,
  source_url, raw_response, confidence_score, observed_at
)
SELECT
  {di.sql_literal(row['observation_key'])}, rps.id, {di.sql_literal(row['origin_iata'])}, {di.sql_literal(row['destination_iata'])},
  {int(row['travel_month'])}, {di.sql_literal(row['cabin'])}, {di.sql_literal(row['currency'])},
  {_sql_decimal(row.get('cash_low'))}, {_sql_decimal(row['cash_typical'])}, {_sql_decimal(row.get('cash_high'))},
  {_sql_decimal(row.get('cash_low')) if row['currency'] == 'USD' else 'NULL'},
  {_sql_decimal(row['cash_typical']) if row['currency'] == 'USD' else 'NULL'},
  {_sql_decimal(row.get('cash_high')) if row['currency'] == 'USD' else 'NULL'},
  {di.sql_literal(row.get('source_url'))}, {di.sql_literal({'notes': row.get('notes')})}, {int(row['confidence_score'])}, {di.sql_literal(row['observed_at'])}::timestamptz
FROM route_price_sources rps
WHERE rps.source_key = {di.sql_literal(row['source_key'])}
ON CONFLICT (observation_key) DO UPDATE SET
  cash_low = EXCLUDED.cash_low,
  cash_typical = EXCLUDED.cash_typical,
  cash_high = EXCLUDED.cash_high,
  cash_usd_low = EXCLUDED.cash_usd_low,
  cash_usd_typical = EXCLUDED.cash_usd_typical,
  cash_usd_high = EXCLUDED.cash_usd_high,
  raw_response = EXCLUDED.raw_response,
  confidence_score = EXCLUDED.confidence_score;
"""


def _baseline_sql(origin: str, dest: str, month: int, cabin: str) -> str:
    key = f"cash|{origin}|{dest}|{month}|{cabin}"
    return f"""
WITH obs AS (
  SELECT * FROM flight_fare_observations
  WHERE origin_iata = {di.sql_literal(origin)}
    AND destination_iata = {di.sql_literal(dest)}
    AND travel_month = {int(month)}
    AND cabin = {di.sql_literal(cabin)}
    AND cash_usd_typical IS NOT NULL
), agg AS (
  SELECT
    percentile_cont(0.25) WITHIN GROUP (ORDER BY cash_usd_typical)::numeric(12,2) AS low,
    percentile_cont(0.50) WITHIN GROUP (ORDER BY cash_usd_typical)::numeric(12,2) AS typical,
    percentile_cont(0.75) WITHIN GROUP (ORDER BY cash_usd_typical)::numeric(12,2) AS high,
    count(*)::int AS sample_count,
    array_agg(DISTINCT source_id) AS source_ids,
    max(observed_at) AS last_observed_at,
    greatest(1, least(10, round(avg(confidence_score))::int)) AS confidence_score
  FROM obs
)
INSERT INTO route_seasonal_price_baselines (
  baseline_key, baseline_type, origin_iata, destination_iata, travel_month, cabin, currency,
  cash_usd_low, cash_usd_typical, cash_usd_high, sample_count, source_ids, source_summary,
  confidence_score, freshness_label, last_observed_at, updated_at
)
SELECT
  {di.sql_literal(key)}, 'cash', {di.sql_literal(origin)}, {di.sql_literal(dest)}, {int(month)}, {di.sql_literal(cabin)}, 'USD',
  low, typical, high, sample_count, source_ids,
  jsonb_build_object('source', 'manual_fare_baseline', 'sample_count', sample_count),
  confidence_score,
  CASE WHEN last_observed_at >= now() - interval '45 days' THEN 'recent' ELSE 'stale' END,
  last_observed_at, now()
FROM agg
WHERE sample_count > 0
ON CONFLICT (baseline_key) DO UPDATE SET
  cash_usd_low = EXCLUDED.cash_usd_low,
  cash_usd_typical = EXCLUDED.cash_usd_typical,
  cash_usd_high = EXCLUDED.cash_usd_high,
  sample_count = EXCLUDED.sample_count,
  source_ids = EXCLUDED.source_ids,
  source_summary = EXCLUDED.source_summary,
  confidence_score = EXCLUDED.confidence_score,
  freshness_label = EXCLUDED.freshness_label,
  last_observed_at = EXCLUDED.last_observed_at,
  updated_at = now();
"""


def apply_manual_import(req: ManualFareImportRequest) -> dict[str, Any]:
    rows, errors = _parse_rows(req.content)
    if req.dry_run:
        return preview_manual_import(req)
    if errors:
        return {"ok": False, "inserted": 0, "errors": errors[:25]}
    if not rows:
        return {"ok": False, "inserted": 0, "errors": [{"row": 0, "error": "No valid rows"}]}

    affected = sorted({(r["origin_iata"], r["destination_iata"], r["travel_month"], r["cabin"]) for r in rows})
    sql_parts = ["BEGIN;"]
    for row in rows:
        sql_parts.append(_source_sql(row))
        sql_parts.append(_observation_sql(row))
    for origin, dest, month, cabin in affected:
        sql_parts.append(_baseline_sql(origin, dest, month, cabin))
    sql_parts.append("COMMIT;")

    _db().run("\n".join(sql_parts))
    return {"ok": True, "inserted_or_updated": len(rows), "baselines_updated": len(affected), "errors": []}


def flight_agent_health() -> dict[str, Any]:
    db = _db()
    has_tables = all(db.columns(table) for table in [
        "route_price_sources", "flight_fare_observations", "route_seasonal_price_baselines", "flight_refresh_work_queue"
    ])
    return {"ok": db.test(), "schema_ready": has_tables}


def queue_preview(limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(int(limit or 50), 200))
    sql = f"""
SELECT COALESCE(json_agg(row_to_json(q)), '[]'::json) FROM (
  SELECT origin_iata, destination_iata, travel_month, cabin, tier, priority_score, reason, status, due_at, attempts, last_error
  FROM flight_refresh_work_queue
  ORDER BY due_at ASC, priority_score DESC
  LIMIT {limit}
) q;
"""
    out = _db().scalar(sql) or "[]"
    try:
        return {"data": json.loads(out)}
    except json.JSONDecodeError:
        return {"data": []}
