"""Staging-DB writer for the multi-lane worker.

The lane worker calls the LLM and gets back a parsed JSON payload.
This module takes that payload and writes it to the staging database
using the same SQL pipeline that ``deepseek_importer.apply_article``
uses, minus the LLM call (which we already did).

It builds a single transaction containing:
  1. Upsert destination
  2. Insert source document
  3. Begin extraction run
  4. Load SQL for the LLM-extracted payload
  5. Finish run

Failure modes:
  - DB write is disabled (FILL_DB_WRITE_ENABLED=0): returns skipped
  - SQL error mid-transaction: returns error, the lane keeps going
  - Connection error: returns error, the lane keeps going
"""
from __future__ import annotations

import os
from typing import Any

# Reuse the existing importer's SQL builders and PsqlClient. We import
# lazily so that ``import db_writer`` doesn't pull in heavy deps when
# the user has FILL_DB_WRITE_ENABLED=0.
def _load_importer():
    import deepseek_importer as di
    return di


def _staging_psql_command() -> str:
    """Return the shell command used to invoke psql against staging.

    The default mirrors the historical ``deepseek_importer`` behavior:
    SSH to the Flynn Pi and docker exec into the staging postgres
    container. Override via ``FILL_STAGING_PSQL`` if your layout
    differs.
    """
    return os.environ.get(
        "FILL_STAGING_PSQL",
        "ssh gtbarnes@flynn.local docker exec -i bp-staging-db psql -U backpacker -d backpacker_index -v ON_ERROR_STOP=1",
    )


def write_article(
    candidate,  # deepseek_importer.Candidate
    title: str,
    wikitext: str,
    revision_id: int | None,
    data: dict[str, Any],
    usage: dict[str, Any],
    model_name: str,
    timeout_s: float = 60.0,
) -> dict[str, Any]:
    """Upsert destination + insert source + record run + load payload.

    Returns:
      {"written": True, "destination_id": "...", "run_id": "..."}
      {"written": False, "skipped": True, "reason": "..."}
      {"written": False, "error": "..."}
    """
    if os.environ.get("FILL_DB_WRITE_ENABLED", "1") == "0":
        return {"written": False, "skipped": True, "reason": "FILL_DB_WRITE_ENABLED=0"}

    di = _load_importer()
    try:
        db = di.PsqlClient(_staging_psql_command(), "staging")
    except Exception as exc:
        return {"written": False, "error": f"could not init PsqlClient: {exc}"}

    # Upsert destination
    try:
        dest_id = db.scalar(
            "\\t on\n\\a on\n" + di.upsert_destination_sql(candidate, revision_id)
        )
    except Exception as exc:
        return {"written": False, "error": f"upsert_destination: {exc}"}
    if not dest_id:
        return {"written": False, "error": "upsert_destination returned no id"}

    # Insert source document
    try:
        source_doc_id = db.scalar(
            "\\t on\n\\a on\n"
            + di.insert_source_document_sql(candidate, dest_id, wikitext, revision_id)
        )
    except Exception as exc:
        return {"written": False, "error": f"insert_source_document: {exc}", "destination_id": dest_id}
    if not source_doc_id:
        return {"written": False, "error": "insert_source_document returned no id",
                "destination_id": dest_id}

    # Begin extraction run
    try:
        run_id = db.scalar(
            "\\t on\n\\a on\n"
            + di.begin_run_sql(
                candidate, dest_id, wikitext, revision_id,
                model_name, "wikivoyage-deepseek-v1", "deepseek_wikivoyage_v1",
            )
        )
    except Exception as exc:
        return {"written": False, "error": f"begin_run: {exc}",
                "destination_id": dest_id, "source_document_id": source_doc_id}
    if not run_id:
        return {"written": False, "error": "begin_run returned no id",
                "destination_id": dest_id, "source_document_id": source_doc_id}

    # Build load SQL and finish-run SQL
    try:
        load_sql = di.build_load_sql(db, dest_id, source_doc_id, data)
        listings = di.extract_listing_templates(wikitext)
        det_sql = di.wikivoyage_listings_sql(dest_id, candidate, listings)

        # Promote deterministic listings to featured_listings for any
        # category the LLM left empty. This catches London-sized
        # articles where the main article has no listings (they are in
        # district sub-articles) but the raw wikitext still has listing
        # templates the LLM didn't extract.
        promote_sql = ""
        fl = data.get("featured_listings")
        if isinstance(fl, dict):
            for cat in ("sleep", "eat", "see", "do", "buy"):
                llm_items = fl.get(cat)
                if isinstance(llm_items, list) and len(llm_items) == 0:
                    # Map Wikivoyage listing_type to our categories
                    type_to_cat = {"sleep": "sleep", "eat": "eat", "drink": "eat",
                                  "see": "see", "do": "do", "buy": "buy",
                                  "listing": "see", "go": "do",
                                  "marker": "see", "other": "see"}
                    det_for_cat = [
                        x for x in listings
                        if type_to_cat.get(x.get("listing_type", "listing")) == cat
                    ][:15]
                    if det_for_cat:
                        promoted = []
                        for x in det_for_cat:
                            raw = x.get("raw_template") or {}
                            name = raw.get("name") or x.get("name", "")
                            if not name:
                                continue
                            promoted.append({
                                "name": name,
                                "description": (raw.get("content") or "")[:500],
                                "price_text": raw.get("price"),
                                "address": raw.get("address"),
                                "url": raw.get("url") or raw.get("website"),
                                "source_listing_uid": x.get("listing_uid"),
                            })
                        if promoted:
                            promote_sql += di.replace_featured_listings_v2_sql(dest_id, cat, promoted) + "\n"

        # Write guide_meta from the LLM's ``destination`` and ``quality``
        # top-level data. This populates taglines, best_for_tags, and
        # sets parser_version='guide_v2' which the public API checks.
        dest_meta = data.get("destination") if isinstance(data.get("destination"), dict) else {}
        quality = data.get("quality") if isinstance(data.get("quality"), dict) else {}
        guide_meta_sql = di.upsert_guide_meta_sql(dest_id, dest_meta, quality)

        # Populate destinations.overview from the first content section
        # body (or from the LLM's destination.summary).  The React SPA
        # shows this as the lead text. Without it, every page says
        # "still gathering field notes" at the top, even with a full guide.
        first_body = ""
        for section in (data.get("content_sections") or []):
            if isinstance(section, dict) and (section.get("body") or section.get("summary") or section.get("body_text") or section.get("content")):
                first_body = (section.get("body") or section.get("summary") or section.get("body_text") or section.get("content") or "")
                break
        if not first_body:
            first_body = dest_meta.get("summary") or ""
        overview_sql = ""
        if first_body:
            overview_sql = f"UPDATE destinations SET overview = {di.sql_literal(first_body[:500])}, updated_at = NOW() WHERE id = '{dest_id}'::uuid;\n"

        finish_sql = di.finish_run_sql(run_id, "done", data, usage)
        tx = (
            "BEGIN;\n"
            + det_sql
            + "\n"
            + promote_sql
            + (load_sql + "\n" if load_sql else "")
            + guide_meta_sql
            + "\n"
            + overview_sql
            + finish_sql
            + "\nCOMMIT;\n"
        )
        db.run(tx)
    except Exception as exc:
        # Mark the run as failed but don't undo the destination row.
        try:
            db.run(
                "BEGIN;\n"
                + di.finish_run_sql(run_id, "failed", data, usage, str(exc)[:4000])
                + "\nCOMMIT;\n"
            )
        except Exception:
            pass
        return {"written": False, "error": f"load+commit: {exc}",
                "destination_id": dest_id, "source_document_id": source_doc_id,
                "run_id": run_id}

    return {
        "written": True,
        "destination_id": dest_id,
        "source_document_id": source_doc_id,
        "run_id": run_id,
    }
