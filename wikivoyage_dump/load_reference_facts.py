#!/usr/bin/env python3
"""Load standardized country/currency/language/plug/exchange-rate facts.

Sources:
- https://restcountries.com/v3.1/all
- https://plugtypes.com/api/countries
- https://open.er-api.com/v6/latest/USD

Writes reference tables from migration 022 and backfills destination_canonical_facts
where destination.country matches a country name/alias.
"""
from __future__ import annotations

import argparse
import json
import urllib.request
from datetime import date
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import deepseek_importer as di
from deepseek_importer import PsqlClient, load_config, sql_literal

CACHE_DIR = Path(__file__).resolve().parent / "reference_cache"

RESTCOUNTRIES_URL = "https://restcountries.com/v3.1/all?fields=name,cca2,cca3,currencies,languages,region,subregion,capital,timezones,idd"
PLUGTYPES_URL = "https://plugtypes.com/api/countries"
RATES_URL = "https://open.er-api.com/v6/latest/USD"

ALIASES = {
    "Bahamas": "BS",
    "Bolivia": "BO",
    "Bosnia and Herzegovina": "BA",
    "Britain": "GB",
    "Brunei": "BN",
    "Cape Verde": "CV",
    "Czech Republic": "CZ",
    "Democratic Republic of the Congo": "CD",
    "Georgia (country)": "GE",
    "Iran": "IR",
    "Ivory Coast": "CI",
    "Laos": "LA",
    "Micronesia": "FM",
    "Moldova": "MD",
    "Netherlands": "NL",
    "North Korea": "KP",
    "Palestinian territories": "PS",
    "Republic of Ireland": "IE",
    "Republic of the Congo": "CG",
    "Russia": "RU",
    "South Korea": "KR",
    "Syria": "SY",
    "Tanzania": "TZ",
    "United States": "US",
    "USA": "US",
    "Vietnam": "VN",
}


def fetch_json(url: str, cache_name: str, refresh: bool = False) -> Any:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / cache_name
    if path.exists() and not refresh:
        return json.loads(path.read_text(encoding="utf-8"))
    req = urllib.request.Request(url, headers={"User-Agent": "BackpackerIndex/0.1"})
    with urllib.request.urlopen(req, timeout=45) as res:  # noqa: S310 - fixed public URLs
        payload = res.read().decode("utf-8")
    path.write_text(payload, encoding="utf-8")
    return json.loads(payload)


def plug_rows(raw: Any) -> dict[str, dict[str, Any]]:
    if isinstance(raw, dict) and isinstance(raw.get("data"), list):
        items = raw["data"]
    elif isinstance(raw, list):
        items = raw
    else:
        items = []
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        code = str(item.get("code") or item.get("iso2") or item.get("countryCode") or "").upper()
        if not code:
            continue
        out[code] = item
    return out


def plug_values(item: dict[str, Any] | None) -> tuple[list[str], int | None, int | None]:
    if not item:
        return [], None, None
    plugs = item.get("plugs") or item.get("plugTypes") or item.get("types") or []
    if isinstance(plugs, dict):
        plugs = plugs.get("types") or []
    if isinstance(plugs, str):
        plugs = [p.strip() for p in plugs.replace("Type", "").replace("/", ",").split(",")]
    voltage = item.get("voltage") or item.get("voltageV")
    frequency = item.get("frequency") or item.get("frequencyHz")
    try:
        voltage_i = int(str(voltage).split("/")[0].replace("V", "").strip()) if voltage else None
    except ValueError:
        voltage_i = None
    try:
        frequency_i = int(str(frequency).replace("Hz", "").strip()) if frequency else None
    except ValueError:
        frequency_i = None
    return [str(p).replace("Type", "").strip().upper() for p in plugs if str(p).strip()], voltage_i, frequency_i


def build_sql(rest: list[dict[str, Any]], plugs: dict[str, dict[str, Any]], rates: dict[str, Any]) -> str:
    statements = ["BEGIN;"]
    currencies: dict[str, dict[str, str | None]] = {}
    languages: dict[str, str] = {}
    country_name_to_iso: dict[str, str] = dict(ALIASES)

    for c in rest:
        iso2 = c.get("cca2")
        if not iso2:
            continue
        iso2 = str(iso2).upper()
        name = (c.get("name") or {}).get("common") or iso2
        official = (c.get("name") or {}).get("official")
        curr = c.get("currencies") or {}
        langs = c.get("languages") or {}
        currency_codes = sorted(curr.keys())
        language_codes = sorted(langs.keys())
        calling = c.get("idd") or {}
        roots = [calling.get("root")] if calling.get("root") else []
        suffixes = calling.get("suffixes") or []
        calling_codes = [r + s for r in roots for s in suffixes[:5]]

        country_name_to_iso[name] = iso2
        if official:
            country_name_to_iso[official] = iso2

        for code, meta in curr.items():
            currencies[code] = {"name": meta.get("name"), "symbol": meta.get("symbol")}
        for code, lang_name in langs.items():
            languages[code] = lang_name

        statements.append(f"""
INSERT INTO countries (iso2, iso3, name, official_name, region, subregion, capital, currency_codes, language_codes, timezones, calling_codes, raw, source_url, updated_at)
VALUES ({sql_literal(iso2)}, {sql_literal(c.get('cca3'))}, {sql_literal(name)}, {sql_literal(official)}, {sql_literal(c.get('region'))}, {sql_literal(c.get('subregion'))}, {sql_literal((c.get('capital') or [None])[0])}, {sql_literal(currency_codes, 'source_priority')}, {sql_literal(language_codes, 'language_codes')}, {sql_literal(c.get('timezones') or [], 'timezone_ids')}, {sql_literal(calling_codes, 'source_priority')}, {sql_literal(c)}, {sql_literal(RESTCOUNTRIES_URL)}, now())
ON CONFLICT (iso2) DO UPDATE SET
  iso3 = EXCLUDED.iso3, name = EXCLUDED.name, official_name = EXCLUDED.official_name,
  region = EXCLUDED.region, subregion = EXCLUDED.subregion, capital = EXCLUDED.capital,
  currency_codes = EXCLUDED.currency_codes, language_codes = EXCLUDED.language_codes,
  timezones = EXCLUDED.timezones, calling_codes = EXCLUDED.calling_codes,
  raw = EXCLUDED.raw, source_url = EXCLUDED.source_url, fetched_at = now(), updated_at = now();
""")

    for code, meta in sorted(currencies.items()):
        statements.append(f"""
INSERT INTO currencies (code, name, symbol, raw, updated_at)
VALUES ({sql_literal(code)}, {sql_literal(meta.get('name'))}, {sql_literal(meta.get('symbol'))}, {sql_literal(meta)}, now())
ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, symbol = EXCLUDED.symbol, raw = EXCLUDED.raw, fetched_at = now(), updated_at = now();
""")

    for code, name in sorted(languages.items()):
        statements.append(f"""
INSERT INTO languages (code, name, updated_at)
VALUES ({sql_literal(code)}, {sql_literal(name)}, now())
ON CONFLICT (code) DO UPDATE SET name = EXCLUDED.name, fetched_at = now(), updated_at = now();
""")

    statements.extend(build_rates_statements(rates))

    # Backfill destination canonical facts by exact destination.country/alias.
    for country_name, iso2 in sorted(country_name_to_iso.items()):
        country = next((c for c in rest if str(c.get("cca2") or "").upper() == iso2), None)
        if not country:
            continue
        curr_codes = sorted((country.get("currencies") or {}).keys())
        lang_codes = sorted((country.get("languages") or {}).keys())
        tz = country.get("timezones") or []
        plug_types, voltage, frequency = plug_values(plugs.get(iso2))
        statements.append(f"""
INSERT INTO destination_canonical_facts (destination_id, country_iso2, currency_code, language_codes, timezone_ids, plug_types, voltage, frequency_hz, canonical_source, updated_at)
SELECT d.id, {sql_literal(iso2)}, {sql_literal(curr_codes[0] if curr_codes else None)}, {sql_literal(lang_codes, 'language_codes')}, {sql_literal(tz, 'timezone_ids')}, {sql_literal(plug_types, 'plug_types')}, {sql_literal(voltage)}, {sql_literal(frequency)}, 'reference_data', now()
FROM destinations d
WHERE lower(d.country) = lower({sql_literal(country_name)})
ON CONFLICT (destination_id) DO UPDATE SET
  country_iso2 = COALESCE(destination_canonical_facts.country_iso2, EXCLUDED.country_iso2),
  currency_code = COALESCE(destination_canonical_facts.currency_code, EXCLUDED.currency_code),
  language_codes = CASE WHEN destination_canonical_facts.language_codes = '{{}}'::text[] THEN EXCLUDED.language_codes ELSE destination_canonical_facts.language_codes END,
  timezone_ids = CASE WHEN destination_canonical_facts.timezone_ids = '{{}}'::text[] THEN EXCLUDED.timezone_ids ELSE destination_canonical_facts.timezone_ids END,
  plug_types = CASE WHEN destination_canonical_facts.plug_types = '{{}}'::text[] THEN EXCLUDED.plug_types ELSE destination_canonical_facts.plug_types END,
  voltage = COALESCE(destination_canonical_facts.voltage, EXCLUDED.voltage),
  frequency_hz = COALESCE(destination_canonical_facts.frequency_hz, EXCLUDED.frequency_hz),
  updated_at = now();
""")

    statements.append("COMMIT;")
    return "\n".join(statements)


def rate_day(rates: dict[str, Any]) -> str:
    raw = rates.get("time_last_update_utc")
    if raw:
        try:
            return parsedate_to_datetime(str(raw)).date().isoformat()
        except (TypeError, ValueError, OverflowError):
            pass
    return str(date.today())


def build_rates_statements(rates: dict[str, Any]) -> list[str]:
    statements: list[str] = []
    day = rate_day(rates)
    rate_map = rates.get("rates") or {}
    for quote, rate in sorted(rate_map.items()):
        try:
            r = float(rate)
        except (TypeError, ValueError):
            continue
        statements.append(f"""
INSERT INTO exchange_rates (base_currency, quote_currency, rate, rate_date, source_url, fetched_at)
VALUES ('USD', {sql_literal(quote)}, {r}, {sql_literal(day)}, {sql_literal(RATES_URL)}, now())
ON CONFLICT (base_currency, quote_currency, rate_date) DO UPDATE SET rate = EXCLUDED.rate, source_url = EXCLUDED.source_url, fetched_at = now();
""")
        if r:
            statements.append(f"""
INSERT INTO exchange_rates (base_currency, quote_currency, rate, rate_date, source_url, fetched_at)
VALUES ({sql_literal(quote)}, 'USD', {1 / r}, {sql_literal(day)}, {sql_literal(RATES_URL)}, now())
ON CONFLICT (base_currency, quote_currency, rate_date) DO UPDATE SET rate = EXCLUDED.rate, source_url = EXCLUDED.source_url, fetched_at = now();
""")
    return statements


def build_rates_sql(rates: dict[str, Any]) -> str:
    return "\n".join(["BEGIN;", *build_rates_statements(rates), "COMMIT;"])


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db-target", choices=["local", "staging"], default="staging")
    p.add_argument("--refresh", action="store_true")
    p.add_argument("--execute", action="store_true")
    p.add_argument("--rates-only", action="store_true", help="Only refresh exchange_rates; do not touch countries, currencies, languages, or destination facts.")
    p.add_argument("--sql-out", default="")
    args = p.parse_args()

    rates = fetch_json(RATES_URL, "open-er-api-usd.json", args.refresh)
    if args.rates_only:
        rest = []
        sql = build_rates_sql(rates)
    else:
        rest = fetch_json(RESTCOUNTRIES_URL, "restcountries-all.json", args.refresh)
        plug_raw = fetch_json(PLUGTYPES_URL, "plugtypes-countries.json", args.refresh)
        sql = build_sql(rest, plug_rows(plug_raw), rates)

    if args.sql_out:
        Path(args.sql_out).write_text(sql, encoding="utf-8")
    print(f"Prepared reference load: countries={len(rest)} rates={len((rates.get('rates') or {}))} rates_only={args.rates_only} execute={args.execute}")
    if not args.execute:
        print("Dry run only. Pass --execute to write DB.")
        return 0

    config = load_config()
    psql = config.get(f"{args.db_target}_psql") or (di.DEFAULT_STAGING_PSQL if args.db_target == "staging" else di.DEFAULT_LOCAL_PSQL)
    PsqlClient(psql, args.db_target).run(sql)
    print("Reference facts loaded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
