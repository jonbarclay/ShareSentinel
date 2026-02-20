"""Benchmark Gemini against known false-positive events.

Bypasses hash reuse and the normal pipeline — directly downloads, extracts,
and sends to Gemini.  Prints a comparison table of original vs Gemini verdict.

Usage (inside worker container):
    python -m scripts.benchmark_gemini
"""

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

# -- bootstrap ----------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("benchmark_gemini")

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

# Target FP events: those that still escalate as tier_1
TARGET_EVENTS = [
    {
        "event_id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "file_name": "2025 W2.pdf",
        "object_id": "https://contoso-my.sharepoint.com/personal/10000001_contoso_com/Documents/Office Lens/2025 W2.pdf",
        "site_url": "https://contoso-my.sharepoint.com/personal/10000001_contoso_com/",
        "user_id": "10000001@contoso.com",
        "sharing_type": "Organization",
        "sharing_scope": "Organization",
        "sharing_permission": "View",
        "operation": "CompanySharingLinkCreated",
        "workload": "OneDrive",
        "item_type": "File",
        "original_categories": ["pii_financial", "pii_contact"],
        "original_tier": "tier_1",
    },
    {
        "event_id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
        "file_name": "Book.xlsx",
        "object_id": "https://contoso-my.sharepoint.com/personal/10000002_contoso_com/Documents/Reports/Book.xlsx",
        "site_url": "https://contoso-my.sharepoint.com/personal/10000002_contoso_com/",
        "user_id": "10000002@contoso.com",
        "sharing_type": "Organization",
        "sharing_scope": "Organization",
        "sharing_permission": "View",
        "operation": "CompanySharingLinkCreated",
        "workload": "OneDrive",
        "item_type": "File",
        "original_categories": ["hipaa"],
        "original_tier": "tier_1",
    },
    {
        "event_id": "c3d4e5f6-a7b8-9012-cdef-123456789012",
        "file_name": "Alumni Contact List.xlsx",
        "object_id": "https://contoso.sharepoint.com/sites/InstitutionalAdvancement/Shared Documents/Alumni Contact List.xlsx",
        "site_url": "https://contoso.sharepoint.com/sites/InstitutionalAdvancement/",
        "user_id": "10000003@contoso.com",
        "sharing_type": "Organization",
        "sharing_scope": "Organization",
        "sharing_permission": "View",
        "operation": "CompanySharingLinkCreated",
        "workload": "SharePoint",
        "item_type": "File",
        "original_categories": ["ferpa", "pii_contact"],
        "original_tier": "tier_1",
    },
]

TMPFS = "/tmp/sharesentinel/benchmark"


class FakeJob:
    def __init__(self, data: dict):
        self._d = data
    def __getattr__(self, name: str):
        return self._d.get(name)


class _NoOpRepo:
    """Stub that silently ignores all method calls (for benchmark mode)."""
    def __getattr__(self, name):
        async def noop(*a, **kw):
            return None
        return noop


async def benchmark_one(
    ev: dict,
    gemini: GeminiProvider,
    graph_client: GraphClient,
    config: Config,
) -> dict:
    """Download, extract, and analyze one event via Gemini. Returns result dict."""
    eid = ev["event_id"]
    fname = ev["file_name"]
    result = {
        "file_name": fname,
        "original_categories": ev["original_categories"],
        "original_tier": ev["original_tier"],
    }

    job = FakeJob(ev)
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

        if classification.action == Action.FILENAME_ONLY:
            result["gemini_mode"] = "filename_only"
            result["gemini_error"] = "skipped (filename_only)"
            return result

        # Download
        downloader = FileDownloader()
        drive_id = metadata.get("drive_id", "")
        item_id = metadata.get("item_id", "")
        downloaded = await downloader.download(
            drive_id, item_id, eid, fname, graph_client, config,
        )

        # Extract
        ext = Path(fname).suffix.lower()
        from app.ai.base_provider import AnalysisRequest

        common_kwargs = dict(
            file_name=fname,
            file_path=metadata.get("parent_path", ""),
            file_size=file_size,
            sharing_user=ev.get("user_id", ""),
            sharing_type=ev.get("sharing_type", ""),
            sharing_permission=ev.get("sharing_permission", ""),
        )

        request = None
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
                # Try OCR
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
        result["gemini_context"] = response.context
        result["gemini_summary"] = response.summary[:120] if response.summary else ""
        result["gemini_cost"] = response.estimated_cost_usd
        result["gemini_success"] = response.success

        if not response.success:
            result["gemini_error"] = response.error

    except Exception as exc:
        result["gemini_error"] = str(exc)[:200]
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    return result


async def main() -> None:
    config = Config.from_env()

    # Build Gemini provider with second-look model
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

    for i, ev in enumerate(TARGET_EVENTS, 1):
        logger.info("--- [%d/%d] %s ---", i, len(TARGET_EVENTS), ev["file_name"])
        r = await benchmark_one(ev, gemini, graph_client, config)
        results.append(r)
        cost = r.get("gemini_cost", 0) or 0
        total_cost += cost
        logger.info(
            "  Original: %s (%s)  |  Gemini: %s (%s)  cost=$%.4f",
            r["original_tier"],
            r["original_categories"],
            r.get("gemini_tier", "ERROR"),
            r.get("gemini_categories", r.get("gemini_error", "?")),
            cost,
        )

    # Print comparison table
    print("\n" + "=" * 100)
    print("GEMINI SECOND-LOOK BENCHMARK RESULTS")
    print(f"Model: {config.second_look_model}  |  Total cost: ${total_cost:.4f}")
    print("=" * 100)
    print(f"{'File':<45} {'Orig Tier':<10} {'Orig Cats':<30} {'Gemini Tier':<12} {'Gemini Cats':<30}")
    print("-" * 100)

    correct_downgrades = 0
    for r in results:
        orig_tier = r["original_tier"]
        gemini_tier = r.get("gemini_tier", "ERROR")
        orig_cats = ",".join(r["original_categories"])
        gemini_cats = ",".join(r.get("gemini_categories", [r.get("gemini_error", "err")]))
        fname = r["file_name"][:44]
        downgraded = "  <-- DOWNGRADED" if gemini_tier == "none" and orig_tier != "none" else ""
        if downgraded:
            correct_downgrades += 1
        print(f"{fname:<45} {orig_tier:<10} {orig_cats:<30} {gemini_tier:<12} {gemini_cats:<30}{downgraded}")

    print("-" * 100)
    print(f"Would-be downgrades: {correct_downgrades}/{len(results)} events")
    print(f"Total Gemini cost: ${total_cost:.4f}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
