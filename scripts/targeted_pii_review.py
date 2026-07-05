"""Targeted legal PII verification — full SSN / Driver's License / financial account.

Legal counsel reviewed the files ShareSentinel already flagged Tier 1/Tier 2 and
asked for a NARROW, targeted re-check: which files actually contain ANY of three
CRITICAL items in COMPLETE, REAL form —

  1. a FULL Social Security Number,
  2. a FULL Driver's License number OR a picture/scan of a driver's license, and
  3. a FULL financial account number.

This script:
  (A) mines the existing AI verdicts for every candidate that MIGHT contain one of
      these (broad max-recall net over categories, pii_types, and reasoning/summary
      text), so we are certain to capture files we already know about; and
  (B) re-downloads and re-analyzes EACH candidate with a dedicated `targeted_pii`
      prompt (OpenAI GPT-5.4 by default) that answers only the three questions and
      returns a structured, MASKED determination.

It is a standalone side investigation: it writes to the dedicated
``targeted_pii_review`` table (+ a CSV for legal) and NEVER overwrites production
``verdicts`` rows or triggers analyst notifications.

Usage (inside the worker container):
    docker exec sharesentinel-worker python -m scripts.targeted_pii_review --dry-run
    docker exec sharesentinel-worker python -m scripts.targeted_pii_review --limit 5
    docker exec sharesentinel-worker python -m scripts.targeted_pii_review \
        --model gpt-5.4 --reasoning-effort medium --concurrency 6
"""

import argparse
import asyncio
import csv
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("targeted_pii_review")

import asyncpg

from app.ai.base_provider import AnalysisRequest
from app.ai.openai_provider import OpenAIProvider
from app.ai.prompt_manager import PromptManager
from app.config import Config
from app.extraction import get_extractor
from app.extraction.image_preprocessor import preprocess_image, render_pdf_pages_as_images
from app.extraction.ocr_extractor import OcrExtractor
from app.graph_api.auth import GraphAuth
from app.graph_api.client import GraphClient
from app.pipeline.classifier import Action, FileClassifier
from scripts.download_for_review import safe_name, site_label

TMPFS = "/tmp/sharesentinel/targeted_pii"
OUTPUT_ROOT = "/app/review_downloads"
SAVE_ROOT = "/app/review_downloads/legal_pii"
TARGET_CATEGORIES = ("pii_government_id", "pii_financial")
TARGET_PII_TYPES = ("ssn", "drivers_license", "financial_account", "itin", "passport", "government_id")
STRONG_PII_TYPES = ("ssn", "drivers_license", "financial_account")

CSV_COLS = [
    "review_status", "any_critical_found", "candidate_class", "analysis_mode_used",
    "download_status", "file_name",
    "full_ssn_found", "ssn_confidence", "ssn_evidence",
    "full_dl_number_found", "dl_number_confidence", "dl_number_evidence",
    "full_dl_image_found", "dl_image_confidence", "dl_image_evidence",
    "full_financial_found", "financial_confidence", "financial_evidence",
    "is_coursework", "saved_path", "targeted_reasoning", "prior_evidence",
    "ai_model", "estimated_cost_usd", "web_url", "object_id", "event_id", "verdict_id",
]


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Targeted full SSN/DL/financial-account verification")
    p.add_argument("--dry-run", action="store_true",
                   help="Resolve candidates and print the breakdown; no downloads or AI calls")
    p.add_argument("--limit", type=int, default=0, help="Max candidates to process (0 = all)")
    p.add_argument("--class", dest="klass", choices=["all", "known_match", "needs_reverify"],
                   default="all", help="Which candidate class to process (default all)")
    p.add_argument("--text-net", choices=["broad", "tight", "off"], default="broad",
                   help="Recall of the reasoning/summary text net (default broad = max recall)")
    p.add_argument("--model", default="gpt-5.4", help="OpenAI model for verification (default gpt-5.4)")
    p.add_argument("--reasoning-effort", default=None,
                   help="reasoning_effort for GPT-5 family (minimal|low|medium|high)")
    p.add_argument("--concurrency", type=int, default=6, help="Concurrent candidates (default 6)")
    p.add_argument("--out", default=OUTPUT_ROOT, help="Output directory for the CSV")
    p.add_argument("--save-dir", default=SAVE_ROOT,
                   help="Persistent dir where original files are saved when a critical item is found")
    p.add_argument("--no-save", action="store_true",
                   help="Do NOT save matched files to disk (verification + CSV only)")
    p.add_argument("--retry-errors-from", default=None, metavar="RUN_ID",
                   help="Reprocess only the TRANSIENT-error rows (ReadError/ConnectError/"
                        "HTTPStatusError) from a prior run_id. Files that were permanently "
                        "gone (FileNotFoundError) are not retried.")
    return p.parse_args()


TRANSIENT_ERROR_STATUSES = ("error:ReadError", "error:ConnectError",
                            "error:HTTPStatusError", "error:TimeoutError")


def _text_net_sql(text_net: str) -> str:
    if text_net == "off":
        return ""
    broad = [
        "social security", "driver%licen", "account number",
        "routing number", "credit card", "bank account",
    ]
    tight = [
        "social security number", "driver%licen",
        "routing number", "credit card number", "bank account number",
    ]
    terms = broad if text_net == "broad" else tight
    clauses = []
    for t in terms:
        clauses.append(f"v.reasoning ILIKE '%{t}%'")
        clauses.append(f"v.summary ILIKE '%{t}%'")
    return "\n     OR " + "\n     OR ".join(clauses)


def build_candidate_query(text_net: str) -> str:
    return f"""
        WITH cand AS (
          SELECT
            v.id AS verdict_id, v.event_id, v.analysis_mode, v.escalation_tier, v.second_look_tier,
            v.categories_detected, v.category_assessments, v.second_look_categories,
            v.pii_types_found, v.reasoning, v.summary, v.risk_score,
            e.file_name, e.drive_id, e.item_id_graph, e.object_id, e.web_url, e.sharing_link_url,
            e.user_id, e.site_url, e.relative_path, e.operation, e.workload, e.item_type,
            e.sharing_type, e.sharing_scope, e.sharing_permission, e.file_size_bytes, e.event_time,
            EXISTS (
              SELECT 1 FROM jsonb_array_elements(
                CASE WHEN jsonb_typeof(v.category_assessments)='array'
                     THEN v.category_assessments ELSE '[]'::jsonb END) a
              WHERE a->>'id' IN ('pii_government_id','pii_financial')
                AND a->>'confidence' = 'high')                        AS high_conf_target,
            (jsonb_typeof(v.pii_types_found)='array'
             AND v.pii_types_found ?| array['ssn','drivers_license','financial_account']) AS strong_pii
          FROM verdicts v
          JOIN events e ON e.event_id = v.event_id
          WHERE v.categories_detected      @> '["pii_government_id"]'
             OR v.categories_detected      @> '["pii_financial"]'
             OR v.second_look_categories   @> '["pii_government_id"]'
             OR v.second_look_categories   @> '["pii_financial"]'
             OR (jsonb_typeof(v.pii_types_found)='array'
                 AND v.pii_types_found ?| array['ssn','drivers_license','financial_account',
                     'itin','passport','government_id']){_text_net_sql(text_net)}
        )
        SELECT *,
          CASE WHEN high_conf_target AND strong_pii AND analysis_mode <> 'filename_only'
               THEN 'known_match' ELSE 'needs_reverify' END AS candidate_class
        FROM cand
        ORDER BY (escalation_tier='tier_1' OR second_look_tier='tier_1') DESC,
                 risk_score DESC NULLS LAST
    """


def dedupe_by_event(rows: list[dict]) -> list[dict]:
    """One row per event_id, keeping the first (highest tier / risk) occurrence."""
    seen: set[str] = set()
    out: list[dict] = []
    for r in rows:
        eid = r["event_id"]
        if eid in seen:
            continue
        seen.add(eid)
        out.append(r)
    return out


# Mask any run of >=7 digits (allowing - and spaces) down to the last 4.
_NUM_RUN_RE = re.compile(r"[0-9][0-9 \-]{5,}[0-9]")


def mask_numbers(text: str | None) -> str:
    if not text:
        return ""

    def _repl(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) < 7:
            return m.group(0)
        return "***" + digits[-4:]

    return _NUM_RUN_RE.sub(_repl, str(text))


def _coerce_jsonb(value) -> object:
    """asyncpg returns JSONB columns as str; decode defensively."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _flag(obj) -> dict:
    """Normalize a {found, confidence, evidence} block from the model."""
    if not isinstance(obj, dict):
        return {"found": False, "confidence": "low", "evidence": ""}
    found = bool(obj.get("found", False))
    conf = str(obj.get("confidence", "low")).lower().strip()
    if conf not in ("high", "medium", "low"):
        conf = "low"
    evidence = mask_numbers(str(obj.get("evidence", "")))[:500]
    return {"found": found, "confidence": conf, "evidence": evidence}


def parse_targeted(raw_text: str) -> dict | None:
    """Parse the targeted JSON from the raw model output (3-attempt strategy)."""
    cleaned = raw_text.strip()
    cleaned = re.sub(r"^```json\s*", "", cleaned)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    parsed = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if m:
            try:
                parsed = json.loads(m.group())
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, dict):
        return None

    ssn = _flag(parsed.get("full_ssn_found"))
    dln = _flag(parsed.get("full_dl_number_found"))
    dli = _flag(parsed.get("full_dl_image_found"))
    fin = _flag(parsed.get("full_financial_found"))
    any_found = bool(parsed.get("any_critical_found", False)) or any(
        f["found"] for f in (ssn, dln, dli, fin)
    )
    return {
        "reasoning": mask_numbers(str(parsed.get("reasoning", "")))[:2000],
        "any_critical_found": any_found,
        "ssn": ssn, "dln": dln, "dli": dli, "fin": fin,
        "is_coursework": bool(parsed.get("is_coursework_or_fabricated", False)),
    }


def prior_evidence_text(rec: dict) -> str:
    """Assemble the existing AI evidence so legal still has a lead when re-verify fails."""
    parts: list[str] = []
    assessments = _coerce_jsonb(rec.get("category_assessments"))
    if isinstance(assessments, list):
        for a in assessments:
            if isinstance(a, dict) and a.get("id") in TARGET_CATEGORIES:
                ev = a.get("evidence", "")
                conf = a.get("confidence", "")
                if ev:
                    parts.append(f"[{a.get('id')}/{conf}] {ev}")
    if rec.get("reasoning"):
        parts.append(f"reasoning: {rec['reasoning']}")
    if rec.get("summary"):
        parts.append(f"summary: {rec['summary']}")
    return mask_numbers(" | ".join(parts))[:2000]


def derive_review_status(targeted: dict | None, download_status: str) -> str:
    if targeted is None:
        return "needs_manual_review"
    if not targeted["any_critical_found"]:
        return "no_critical"
    high = any(
        targeted[k]["found"] and targeted[k]["confidence"] == "high"
        for k in ("ssn", "dln", "dli", "fin")
    )
    return "likely_match" if high else "needs_manual_review"


# ----------------------------------------------------------------------
# Per-candidate processing
# ----------------------------------------------------------------------

async def process_one(
    rec: dict,
    provider: OpenAIProvider,
    graph_client: GraphClient,
    config: Config,
    run_id: str,
    save_dir: Path | None,
) -> dict:
    eid = rec["event_id"]
    fname = rec["file_name"] or ""
    event_time = rec.get("event_time")
    result = {
        "run_id": None,
        "verdict_id": rec["verdict_id"],
        "event_id": eid,
        "file_name": fname,
        "drive_id": rec.get("drive_id") or "",
        "item_id_graph": rec.get("item_id_graph") or "",
        "web_url": rec.get("web_url") or "",
        "object_id": rec.get("object_id") or "",
        "sharing_link_url": rec.get("sharing_link_url") or "",
        "candidate_class": rec["candidate_class"],
        "analysis_mode_used": "",
        "download_status": "skipped",
        "targeted": None,
        "ai_model": "",
        "ai_provider": "",
        "estimated_cost_usd": 0.0,
        "prior_evidence": prior_evidence_text(rec),
        "saved_path": "",
        "error": "",
    }

    common_kwargs = dict(
        file_name=fname,
        file_path=rec.get("relative_path") or "",
        file_size=rec.get("file_size_bytes") or 0,
        sharing_user=rec.get("user_id") or "",
        sharing_type=rec.get("sharing_type") or "",
        sharing_permission=rec.get("sharing_permission") or "",
        event_time=event_time.isoformat() if hasattr(event_time, "isoformat") else (event_time or ""),
    )

    # Folders have no single file content to verify.
    if (rec.get("item_type") or "File") == "Folder":
        result["analysis_mode_used"] = "filename_only"
        result["download_status"] = "skipped"
        result["review_status"] = "needs_manual_review"
        return result

    file_size = rec.get("file_size_bytes") or 0
    drive_id = rec.get("drive_id") or ""
    item_id = rec.get("item_id_graph") or ""

    # No stored Graph identifiers → cannot re-download → manual review.
    if not (drive_id and item_id):
        result["analysis_mode_used"] = "filename_only"
        result["download_status"] = "skipped"
        result["review_status"] = "needs_manual_review"
        return result

    work_dir = Path(TMPFS) / re.sub(r"[^A-Za-z0-9_.-]", "_", eid)
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        # Non-invasive: download straight from the stored drive_id/item_id_graph.
        # We deliberately do NOT use MetadataPrescreen here — it enrolls sharing
        # links into the lifecycle tracker as a side effect, which a legal
        # investigation must not do.
        classification = FileClassifier().classify(fname, "File", file_size, config)

        request: AnalysisRequest | None = None
        if classification.action in (Action.FILENAME_ONLY, Action.ARCHIVE_MANIFEST):
            # Filename alone cannot prove a FULL number → leave for manual review.
            result["analysis_mode_used"] = "filename_only"
            result["download_status"] = "skipped"
            result["review_status"] = "needs_manual_review"
            return result

        downloaded = work_dir / safe_name(fname or "download")
        await graph_client.download_file(drive_id, item_id, downloaded)
        result["download_status"] = "downloaded"
        ext = Path(fname).suffix.lower()

        if classification.action == Action.MULTIMODAL:
            pre = preprocess_image(downloaded)
            request = AnalysisRequest(
                mode="targeted_pii_multimodal",
                images=[pre.image_bytes], image_mime_types=[pre.mime_type], **common_kwargs,
            )
        elif classification.action == Action.FULL_ANALYSIS:
            extractor = get_extractor(ext)
            extraction = extractor.extract(downloaded, file_size) if extractor else None
            if extraction and extraction.success and extraction.text_content:
                request = AnalysisRequest(
                    mode="targeted_pii", text_content=extraction.text_content,
                    was_sampled=extraction.was_sampled,
                    sampling_description=extraction.sampling_description, **common_kwargs,
                )
            elif ext == ".pdf":
                ocr = OcrExtractor().extract(downloaded, file_size)
                if ocr.success and ocr.text_content:
                    request = AnalysisRequest(
                        mode="targeted_pii", text_content=ocr.text_content, **common_kwargs,
                    )
                else:
                    images = render_pdf_pages_as_images(downloaded)
                    if images:
                        request = AnalysisRequest(
                            mode="targeted_pii_multimodal",
                            images=[i.image_bytes for i in images],
                            image_mime_types=[i.mime_type for i in images], **common_kwargs,
                        )

        if request is None:
            # Downloaded but could not extract usable content.
            result["analysis_mode_used"] = "filename_only"
            result["review_status"] = "needs_manual_review"
            return result

        result["analysis_mode_used"] = request.mode
        response = await provider.analyze(request)
        result["ai_model"] = response.model
        result["ai_provider"] = response.provider
        result["estimated_cost_usd"] = response.estimated_cost_usd or 0.0

        if not response.success:
            result["error"] = response.error or "ai_error"
            result["review_status"] = "needs_manual_review"
            return result

        result["targeted"] = parse_targeted(response.raw_response)
        result["review_status"] = derive_review_status(result["targeted"], result["download_status"])

        # Save the ORIGINAL file for legal counsel whenever a critical item was
        # found (any confidence). This is a deliberate, legal-requested exception
        # to the "files never touch persistent disk" rule — handle the output
        # directory as sensitive. Done before the tmpfs work_dir is removed.
        if save_dir is not None and result["targeted"] and result["targeted"]["any_critical_found"]:
            owner = site_label(rec.get("object_id") or "") or (rec.get("user_id") or "unknown")
            dest_dir = save_dir / run_id / owner
            dest_dir.mkdir(parents=True, exist_ok=True)
            # Prefix with the event_id so concurrent saves never collide.
            dest = dest_dir / f"{eid[:12]}_{safe_name(fname or Path(downloaded).name)}"
            shutil.copy2(downloaded, dest)
            result["saved_path"] = str(dest)
            logger.info("[%s] SAVED for legal review -> %s", eid, dest)

    except Exception as exc:  # noqa: BLE001 — one failure must never abort the batch
        logger.warning("[%s] failed: %s", eid, exc)
        result["download_status"] = f"error:{type(exc).__name__}"
        result["error"] = str(exc)[:200]
        result["review_status"] = "needs_manual_review"
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return result


async def upsert_result(pool: asyncpg.Pool, run_id: str, r: dict) -> None:
    t = r.get("targeted")
    def fb(key, field):
        return (t[key][field] if t else None)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO targeted_pii_review (
                run_id, verdict_id, event_id, file_name, drive_id, item_id_graph,
                web_url, sharing_link_url, candidate_class, analysis_mode_used, download_status,
                any_critical_found,
                full_ssn_found, ssn_confidence, ssn_evidence,
                full_dl_number_found, dl_number_confidence, dl_number_evidence,
                full_dl_image_found, dl_image_confidence, dl_image_evidence,
                full_financial_found, financial_confidence, financial_evidence,
                is_coursework, ai_provider, ai_model, targeted_reasoning, prior_evidence,
                review_status, estimated_cost_usd, saved_path
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,
                $13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,
                $25,$26,$27,$28,$29,$30,$31,$32
            )
            ON CONFLICT (event_id, run_id) DO UPDATE SET
                review_status = EXCLUDED.review_status,
                any_critical_found = EXCLUDED.any_critical_found,
                saved_path = EXCLUDED.saved_path
            """,
            run_id, r["verdict_id"], r["event_id"], r["file_name"], r["drive_id"], r["item_id_graph"],
            r["web_url"], r["sharing_link_url"], r["candidate_class"], r["analysis_mode_used"],
            r["download_status"],
            (t["any_critical_found"] if t else None),
            fb("ssn", "found"), fb("ssn", "confidence"), fb("ssn", "evidence"),
            fb("dln", "found"), fb("dln", "confidence"), fb("dln", "evidence"),
            fb("dli", "found"), fb("dli", "confidence"), fb("dli", "evidence"),
            fb("fin", "found"), fb("fin", "confidence"), fb("fin", "evidence"),
            (t["is_coursework"] if t else None),
            r["ai_provider"], r["ai_model"], (t["reasoning"] if t else ""), r["prior_evidence"],
            r["review_status"], r["estimated_cost_usd"], r["saved_path"],
        )


def csv_row(r: dict) -> dict:
    t = r.get("targeted")
    def g(key, field):
        return (t[key][field] if t else "")
    return {
        "review_status": r.get("review_status", ""),
        "any_critical_found": (t["any_critical_found"] if t else ""),
        "candidate_class": r["candidate_class"],
        "analysis_mode_used": r["analysis_mode_used"],
        "download_status": r["download_status"],
        "file_name": r["file_name"],
        "full_ssn_found": g("ssn", "found"), "ssn_confidence": g("ssn", "confidence"),
        "ssn_evidence": g("ssn", "evidence"),
        "full_dl_number_found": g("dln", "found"), "dl_number_confidence": g("dln", "confidence"),
        "dl_number_evidence": g("dln", "evidence"),
        "full_dl_image_found": g("dli", "found"), "dl_image_confidence": g("dli", "confidence"),
        "dl_image_evidence": g("dli", "evidence"),
        "full_financial_found": g("fin", "found"), "financial_confidence": g("fin", "confidence"),
        "financial_evidence": g("fin", "evidence"),
        "is_coursework": (t["is_coursework"] if t else ""),
        "saved_path": r["saved_path"],
        "targeted_reasoning": (t["reasoning"] if t else ""),
        "prior_evidence": r["prior_evidence"],
        "ai_model": r["ai_model"], "estimated_cost_usd": f"{r['estimated_cost_usd']:.6f}",
        "web_url": r["web_url"], "object_id": r["object_id"],
        "event_id": r["event_id"], "verdict_id": r["verdict_id"],
    }


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

async def main() -> None:
    args = parse_args()
    config = Config.from_env()
    if not config.database_url:
        raise SystemExit("ERROR: DATABASE_URL not set.")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=4)
    async with pool.acquire() as conn:
        if args.retry_errors_from:
            # Retry only the transient-error event_ids from a prior run. Re-resolve
            # full candidate rows so process_one has everything it needs.
            target_ids = [r["event_id"] for r in await conn.fetch(
                "SELECT DISTINCT event_id FROM targeted_pii_review "
                "WHERE run_id = $1 AND download_status = ANY($2::text[])",
                args.retry_errors_from, list(TRANSIENT_ERROR_STATUSES),
            )]
            logger.info("Retry mode: %d transient-error event_ids from run %s",
                        len(target_ids), args.retry_errors_from)
            rows = [dict(r) for r in await conn.fetch(
                f"SELECT * FROM ({build_candidate_query('broad')}) q "
                f"WHERE q.event_id = ANY($1::text[])",
                target_ids,
            )] if target_ids else []
        else:
            rows = [dict(r) for r in await conn.fetch(build_candidate_query(args.text_net))]
    rows = dedupe_by_event(rows)

    if args.klass != "all":
        rows = [r for r in rows if r["candidate_class"] == args.klass]
    if args.limit > 0:
        rows = rows[: args.limit]

    known = sum(1 for r in rows if r["candidate_class"] == "known_match")
    needs = len(rows) - known
    logger.info("Candidates: %d (known_match=%d, needs_reverify=%d) | text-net=%s | run_id=%s%s",
                len(rows), known, needs, args.text_net, run_id,
                f" | RETRY of {args.retry_errors_from}" if args.retry_errors_from else "")

    if args.dry_run:
        downloadable = sum(1 for r in rows if (r.get("drive_id") and r.get("item_id_graph")))
        total_bytes = sum((r.get("file_size_bytes") or 0) for r in rows)
        print(f"\n[dry-run] {len(rows)} candidates "
              f"(known_match={known}, needs_reverify={needs})")
        print(f"  downloadable (drive_id+item_id present): {downloadable}/{len(rows)}")
        print(f"  approx total download size: {total_bytes / 1024**3:.2f} GB")
        print("  No downloads or AI calls were made.")
        await pool.close()
        return

    pm = PromptManager(template_dir=config.prompt_template_dir)
    if "targeted_pii" not in pm._templates:
        await pool.close()
        raise SystemExit("ERROR: targeted_pii prompt mode not loaded — check the template file.")

    provider = OpenAIProvider(
        api_key=config.openai_api_key,
        model=args.model,
        prompt_manager=pm,
        temperature=0,
        reasoning_effort=args.reasoning_effort,
    )
    graph_client = GraphClient(auth=GraphAuth(
        tenant_id=config.azure_tenant_id,
        client_id=config.azure_client_id,
        client_secret=config.azure_client_secret,
        certificate_path=config.azure_certificate_path or None,
        certificate_password=config.azure_certificate_password or None,
    ))
    Path(TMPFS).mkdir(parents=True, exist_ok=True)
    save_dir = None if args.no_save else Path(args.save_dir)
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Matched files will be saved for legal review under: %s/%s", save_dir, run_id)

    sem = asyncio.Semaphore(max(1, args.concurrency))
    results: list[dict] = []
    counters = {"likely_match": 0, "no_critical": 0, "needs_manual_review": 0, "errors": 0}
    total_cost = 0.0
    rlock = asyncio.Lock()

    async def worker(idx: int, rec: dict) -> None:
        # A single bad candidate (download, AI, or DB write) must never abort the
        # whole batch — contain every failure here.
        try:
            async with sem:
                r = await process_one(rec, provider, graph_client, config, run_id, save_dir)
            try:
                await upsert_result(pool, run_id, r)
            except Exception as exc:  # noqa: BLE001
                logger.error("[%s] upsert failed: %s", rec.get("event_id"), exc)
                r["error"] = f"upsert:{type(exc).__name__}"
                r["review_status"] = "needs_manual_review"
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] unexpected failure: %s", rec.get("event_id"), exc)
            r = {
                "review_status": "needs_manual_review", "analysis_mode_used": "",
                "download_status": f"error:{type(exc).__name__}", "candidate_class": rec.get("candidate_class", ""),
                "file_name": rec.get("file_name") or "", "verdict_id": rec.get("verdict_id"),
                "event_id": rec.get("event_id") or "", "object_id": rec.get("object_id") or "",
                "web_url": rec.get("web_url") or "", "ai_model": "", "ai_provider": "",
                "estimated_cost_usd": 0.0, "prior_evidence": "", "saved_path": "",
                "targeted": None, "error": str(exc)[:200],
            }
        async with rlock:
            results.append(r)
            status = r.get("review_status", "needs_manual_review")
            counters[status] = counters.get(status, 0) + 1
            if r.get("error"):
                counters["errors"] += 1
        logger.info("[%d/%d] %s | %s | %s | $%.4f",
                    idx, len(rows), r.get("review_status"), r.get("analysis_mode_used") or "-",
                    (r.get("file_name") or "")[:50], r.get("estimated_cost_usd", 0.0) or 0.0)

    await asyncio.gather(*(worker(i, r) for i, r in enumerate(rows, 1)),
                         return_exceptions=True)
    total_cost = sum((r.get("estimated_cost_usd", 0.0) or 0.0) for r in results)

    # Write CSV (sorted: any_critical_found first, then SSN confidence).
    conf_rank = {"high": 0, "medium": 1, "low": 2, "": 3}

    def sort_key(r: dict):
        t = r.get("targeted")
        any_crit = 0 if (t and t["any_critical_found"]) else 1
        ssn_conf = conf_rank.get(t["ssn"]["confidence"] if t else "", 3)
        return (any_crit, ssn_conf, r["file_name"] or "")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"targeted_pii_review_{run_id}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        for r in sorted(results, key=sort_key):
            w.writerow(csv_row(r))

    print("\n" + "=" * 64)
    print("  Targeted PII Verification complete")
    print("=" * 64)
    print(f"  Run ID:               {run_id}")
    print(f"  Model:                {args.model}")
    print(f"  Candidates:           {len(results):>8,}")
    print(f"  Likely match:         {counters.get('likely_match', 0):>8,}")
    print(f"  No critical found:    {counters.get('no_critical', 0):>8,}")
    print(f"  Needs manual review:  {counters.get('needs_manual_review', 0):>8,}")
    print(f"  Errors:               {counters.get('errors', 0):>8,}")
    saved = sum(1 for r in results if r.get("saved_path"))
    print(f"  Files saved (legal):  {saved:>8,}")
    print(f"  Estimated cost:       ${total_cost:>10.4f}")
    print(f"  CSV:                  {csv_path}")
    if save_dir is not None and saved:
        print(f"  Saved files dir:      {save_dir / run_id}")
    print("=" * 64)

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
