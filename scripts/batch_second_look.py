"""Batch second-look: run Gemini 3.1 Pro on ALL flagged events.

Queries the database for every verdict with notification_required=true and
second_look_performed=false, downloads the file, extracts content, sends to
Gemini, and writes the second_look columns back to the verdicts table.

Usage (inside worker container):
    python -m scripts.batch_second_look [--dry-run] [--limit N] [--mode text|multimodal|all]
"""

import asyncio
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("batch_second_look")

import asyncpg

from app.ai.base_provider import AnalysisRequest
from app.ai.gemini_provider import GeminiProvider
from app.ai.prompt_manager import PromptManager
from app.config import Config
from app.extraction import get_extractor
from app.extraction.image_preprocessor import preprocess_image, render_pdf_pages_as_images
from app.extraction.ocr_extractor import OcrExtractor
from app.graph_api.auth import GraphAuth
from app.graph_api.client import GraphClient
from app.pipeline.classifier import Action, FileClassifier
from app.pipeline.downloader import FileDownloader
from app.pipeline.metadata import MetadataPrescreen

TMPFS = "/tmp/sharesentinel/batch_sl"


class FakeJob:
    def __init__(self, data: dict):
        self._d = data
    def __getattr__(self, name: str):
        return self._d.get(name)


class _NoOpRepo:
    def __getattr__(self, name):
        async def noop(*a, **kw):
            return None
        return noop


async def fetch_flagged_events(pool: asyncpg.Pool, mode_filter: str) -> list[dict]:
    """Get all flagged events that haven't had a second look."""
    mode_clause = ""
    if mode_filter == "text":
        mode_clause = "AND v.analysis_mode = 'text'"
    elif mode_filter == "multimodal":
        mode_clause = "AND v.analysis_mode = 'multimodal'"
    elif mode_filter == "all":
        mode_clause = "AND v.analysis_mode NOT IN ('folder_flag')"
    else:
        mode_clause = "AND v.analysis_mode NOT IN ('folder_flag')"

    query = f"""
        SELECT e.event_id, e.file_name, e.object_id, e.site_url,
               e.user_id, e.sharing_type, e.sharing_scope,
               e.sharing_permission, e.operation, e.workload, e.item_type,
               v.analysis_mode, v.escalation_tier,
               v.categories_detected, v.id as verdict_id, v.summary as original_summary
        FROM events e
        JOIN verdicts v ON v.event_id = e.event_id
        WHERE v.notification_required = true
          AND v.second_look_performed = false
          {mode_clause}
        ORDER BY v.id
    """
    async with pool.acquire() as conn:
        rows = await conn.fetch(query)
    return [dict(r) for r in rows]


async def update_second_look(
    pool: asyncpg.Pool,
    verdict_id: int,
    provider: str,
    model: str,
    agreed: bool,
    categories: list[str],
    tier: str,
    summary: str,
    reasoning: str,
    cost_usd: float,
) -> None:
    """Write second_look results back to the verdicts table."""
    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE verdicts
            SET second_look_performed = true,
                second_look_provider = $1,
                second_look_model = $2,
                second_look_agreed = $3,
                second_look_categories = $4::jsonb,
                second_look_tier = $5,
                second_look_summary = $6,
                second_look_reasoning = $7,
                second_look_cost_usd = $8
            WHERE id = $9
            """,
            provider,
            model,
            agreed,
            json.dumps(categories),
            tier,
            summary,
            reasoning,
            cost_usd,
            verdict_id,
        )


async def analyze_one(
    ev: dict,
    gemini: GeminiProvider,
    graph_client: GraphClient,
    config: Config,
) -> dict:
    """Download, extract, and analyze one event via Gemini. Returns result dict."""
    eid = ev["event_id"]
    fname = ev["file_name"]
    result = {
        "event_id": eid,
        "verdict_id": ev["verdict_id"],
        "file_name": fname,
        "original_mode": ev["analysis_mode"],
        "original_tier": ev["escalation_tier"],
        "original_categories": ev["categories_detected"],
    }

    # For filename_only and hash_reuse, we still try to download and do full analysis
    job = FakeJob({
        "event_id": eid,
        "file_name": fname,
        "object_id": ev["object_id"],
        "site_url": ev["site_url"],
        "user_id": ev["user_id"],
        "sharing_type": ev["sharing_type"],
        "sharing_scope": ev["sharing_scope"],
        "sharing_permission": ev["sharing_permission"],
        "operation": ev["operation"],
        "workload": ev["workload"],
        "item_type": ev["item_type"],
    })
    work_dir = Path(TMPFS) / eid
    work_dir.mkdir(parents=True, exist_ok=True)

    try:
        # Metadata
        noop = _NoOpRepo()
        prescreener = MetadataPrescreen()
        metadata = await prescreener.fetch_metadata(
            job, graph_client, noop, noop,
            config.user_profile_cache_days, config.upn_domain,
        )

        # Classify
        classifier = FileClassifier()
        file_size = metadata.get("size", 0)
        classification = classifier.classify(fname, "File", file_size, config)

        common_kwargs = dict(
            file_name=fname,
            file_path=metadata.get("parent_path", ""),
            file_size=file_size,
            sharing_user=ev.get("user_id", ""),
            sharing_type=ev.get("sharing_type", ""),
            sharing_permission=ev.get("sharing_permission", ""),
        )

        request = None

        if classification.action in (Action.FILENAME_ONLY, Action.ARCHIVE_MANIFEST):
            # Still send to Gemini as filename_only
            request = AnalysisRequest(mode="filename_only", **common_kwargs)
        else:
            # Download
            drive_id = metadata.get("drive_id", "")
            item_id = metadata.get("item_id", "")
            downloader = FileDownloader()
            downloaded = await downloader.download(
                drive_id, item_id, eid, fname, graph_client, config,
            )

            ext = Path(fname).suffix.lower()

            if classification.action == Action.MULTIMODAL:
                preprocessed = preprocess_image(downloaded)
                request = AnalysisRequest(
                    mode="multimodal",
                    images=[preprocessed.image_bytes],
                    image_mime_types=[preprocessed.mime_type],
                    **common_kwargs,
                )
            elif classification.action == Action.FULL_ANALYSIS:
                extractor = get_extractor(ext)
                extraction_result = extractor.extract(downloaded, file_size) if extractor else None

                if extraction_result and extraction_result.success and extraction_result.text_content:
                    request = AnalysisRequest(
                        mode="text",
                        text_content=extraction_result.text_content,
                        was_sampled=extraction_result.was_sampled,
                        sampling_description=extraction_result.sampling_description,
                        **common_kwargs,
                    )
                elif ext == ".pdf":
                    ocr = OcrExtractor()
                    ocr_result = ocr.extract(downloaded, file_size)
                    if ocr_result.success and ocr_result.text_content:
                        request = AnalysisRequest(
                            mode="text",
                            text_content=ocr_result.text_content,
                            **common_kwargs,
                        )
                    else:
                        images = render_pdf_pages_as_images(downloaded)
                        if images:
                            request = AnalysisRequest(
                                mode="multimodal",
                                images=[img.image_bytes for img in images],
                                image_mime_types=[img.mime_type for img in images],
                                **common_kwargs,
                            )

        if request is None:
            request = AnalysisRequest(mode="filename_only", **common_kwargs)

        result["gemini_mode"] = request.mode

        # Analyze with Gemini
        response = await gemini.analyze(request)

        result["gemini_categories"] = [c.id for c in response.categories]
        result["gemini_tier"] = response.escalation_tier
        result["gemini_summary"] = response.summary[:200] if response.summary else ""
        result["gemini_reasoning"] = response.reasoning or ""
        result["gemini_cost"] = response.estimated_cost_usd or 0
        result["gemini_success"] = response.success
        result["gemini_agreed"] = response.should_escalate
        result["gemini_provider"] = response.provider
        result["gemini_model"] = response.model

        if not response.success:
            result["gemini_error"] = response.error

    except Exception as exc:
        logger.exception("[%s] Failed: %s", eid, exc)
        result["gemini_error"] = str(exc)[:200]
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return result


async def main() -> None:
    dry_run = "--dry-run" in sys.argv
    limit = None
    mode_filter = "all"

    for i, arg in enumerate(sys.argv):
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])
        if arg == "--mode" and i + 1 < len(sys.argv):
            mode_filter = sys.argv[i + 1]

    config = Config.from_env()

    # DB connection
    pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=3)

    # Fetch events
    events = await fetch_flagged_events(pool, mode_filter)
    if limit:
        events = events[:limit]

    logger.info("Found %d flagged events for second look (mode=%s, limit=%s)", len(events), mode_filter, limit)

    if dry_run:
        print(f"\n{'#':<4} {'VID':<6} {'Mode':<14} {'Tier':<8} {'Categories':<30} {'File':<50}")
        print("-" * 112)
        for i, ev in enumerate(events, 1):
            cats = ev["categories_detected"]
            if isinstance(cats, str):
                cats = json.loads(cats)
            cat_str = ",".join(cats) if cats else "[]"
            print(f"{i:<4} {ev['verdict_id']:<6} {ev['analysis_mode']:<14} {ev['escalation_tier']:<8} {cat_str:<30} {ev['file_name'][:50]:<50}")
        print(f"\n[dry-run] {len(events)} events would be processed. No API calls made.")
        await pool.close()
        return

    # Build Gemini provider
    prompt_manager = PromptManager(template_dir=config.prompt_template_dir)
    gemini = GeminiProvider(
        api_key=config.gemini_api_key,
        model=config.second_look_model,
        prompt_manager=prompt_manager,
        max_tokens=config.ai_max_tokens,
        temperature=0.0,
        project=config.vertex_project,
        location=config.vertex_location,
    )
    logger.info("Using Gemini model: %s", config.second_look_model)

    # Build Graph client
    graph_auth = GraphAuth(
        tenant_id=config.azure_tenant_id,
        client_id=config.azure_client_id,
        client_secret=config.azure_client_secret,
        certificate_path=config.azure_certificate_path or None,
        certificate_password=config.azure_certificate_password or None,
    )
    graph_client = GraphClient(auth=graph_auth)

    Path(TMPFS).mkdir(parents=True, exist_ok=True)

    results = []
    total_cost = 0.0
    downgrades = 0
    errors = 0

    for i, ev in enumerate(events, 1):
        logger.info("--- [%d/%d] %s (verdict %d) ---", i, len(events), ev["file_name"], ev["verdict_id"])

        r = await analyze_one(ev, gemini, graph_client, config)
        results.append(r)

        if "gemini_error" in r:
            errors += 1
            logger.error("  ERROR: %s", r["gemini_error"])
            continue

        cost = r.get("gemini_cost", 0) or 0
        total_cost += cost
        agreed = r.get("gemini_agreed", True)

        if not agreed:
            downgrades += 1

        # Write back to DB
        await update_second_look(
            pool,
            verdict_id=r["verdict_id"],
            provider=r.get("gemini_provider", "gemini"),
            model=r.get("gemini_model", config.second_look_model),
            agreed=agreed,
            categories=r.get("gemini_categories", []),
            tier=r.get("gemini_tier", "none"),
            summary=r.get("gemini_summary", ""),
            reasoning=r.get("gemini_reasoning", ""),
            cost_usd=cost,
        )

        logger.info(
            "  Original: %s (%s) | Gemini: %s (%s) | %s | cost=$%.4f",
            r["original_tier"],
            r["original_categories"],
            r.get("gemini_tier", "?"),
            r.get("gemini_categories", "?"),
            "AGREED" if agreed else "DOWNGRADED",
            cost,
        )

        # Brief pause between API calls to avoid rate limiting
        if i < len(events):
            await asyncio.sleep(0.5)

    # Summary
    processed = len(results) - errors
    print(f"\n{'=' * 110}")
    print("BATCH SECOND-LOOK RESULTS")
    print(f"Model: {config.second_look_model}  |  Processed: {processed}  |  Errors: {errors}  |  Total cost: ${total_cost:.4f}")
    print(f"{'=' * 110}")
    print(f"{'#':<4} {'File':<40} {'Orig Tier':<10} {'Orig Cats':<25} {'Gemini Tier':<12} {'Gemini Cats':<25} {'Result':<12}")
    print("-" * 110)

    for i, r in enumerate(results, 1):
        if "gemini_error" in r:
            print(f"{i:<4} {r['file_name'][:39]:<40} {r['original_tier']:<10} {str(r['original_categories'])[:24]:<25} {'ERROR':<12} {r['gemini_error'][:24]:<25} {'ERROR':<12}")
            continue

        orig_cats = r["original_categories"]
        if isinstance(orig_cats, str):
            orig_cats = json.loads(orig_cats)
        orig_cat_str = ",".join(orig_cats) if orig_cats else "[]"
        gemini_cat_str = ",".join(r.get("gemini_categories", []))
        agreed = r.get("gemini_agreed", True)
        verdict = "AGREED" if agreed else "DOWNGRADED"

        print(f"{i:<4} {r['file_name'][:39]:<40} {r['original_tier']:<10} {orig_cat_str[:24]:<25} {r.get('gemini_tier', '?'):<12} {gemini_cat_str[:24]:<25} {verdict:<12}")

    print("-" * 110)
    print(f"Downgrades: {downgrades}/{processed}  |  Errors: {errors}  |  Total cost: ${total_cost:.4f}")
    print()

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
