"""Benchmark Gemini against known false-positive events.

Bypasses hash reuse and the normal pipeline — directly downloads, extracts,
and sends to Gemini.  Prints a comparison table of original vs Gemini verdict.

Usage (inside worker container):
    python -m scripts.benchmark_gemini
"""

import asyncio
import json
import logging
import os
import shutil
import sys
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
from app.pipeline.downloader import DownloadError, FileDownloader
from app.pipeline.metadata import MetadataPrescreen

# Target FP events: those that still escalate as tier_1
TARGET_EVENTS = [
    {
        "event_id": "d0bf328c-4ea0-45ec-bcf1-cd161099c228",
        "file_name": "2025 W2.pdf",
        "object_id": "https://uvu365-my.sharepoint.com/personal/11092536_uvu_edu/Documents/Office Lens/2025 W2.pdf",
        "site_url": "https://uvu365-my.sharepoint.com/personal/11092536_uvu_edu/",
        "user_id": "11092536@uvu.edu",
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
        "event_id": "df22503d-36bb-4fc9-bfea-2d8df17a323c",
        "file_name": "Book.xlsx",
        "object_id": "https://uvu365-my.sharepoint.com/personal/11015885_uvu_edu/Documents/{My Files}/SUPERVISORY/2026 S L/Book.xlsx",
        "site_url": "https://uvu365-my.sharepoint.com/personal/11015885_uvu_edu/",
        "user_id": "11015885@uvu.edu",
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
        "event_id": "656bf91d-dfe8-4b31-b4a4-b4a07b14272d",
        "file_name": "Meldeschein Humboldt Universität_aspen_clyde.docx",
        "object_id": "https://uvu365-my.sharepoint.com/personal/10947082_uvu_edu/Documents/Microsoft Teams Chat Files/Meldeschein Humboldt Universität_aspen_clyde.docx",
        "site_url": "https://uvu365-my.sharepoint.com/personal/10947082_uvu_edu/",
        "user_id": "10947082@uvu.edu",
        "sharing_type": "Organization",
        "sharing_scope": "Organization",
        "sharing_permission": "View",
        "operation": "CompanySharingLinkCreated",
        "workload": "OneDrive",
        "item_type": "File",
        "original_categories": ["pii_government_id", "pii_contact"],
        "original_tier": "tier_1",
    },
    # Include the ferpa ones that were previously FPs (now handled by override)
    # to see what Gemini would say
    {
        "event_id": "87038d10-3285-4d12-8a45-e848bc3c82ed",
        "file_name": "202620 SR Welcome.xlsx",
        "object_id": "https://uvu365-my.sharepoint.com/personal/10865264_uvu_edu/Documents/202620 SR Welcome.xlsx",
        "site_url": "https://uvu365-my.sharepoint.com/personal/10865264_uvu_edu/",
        "user_id": "10865264@uvu.edu",
        "sharing_type": "Organization",
        "sharing_scope": "Organization",
        "sharing_permission": "View",
        "operation": "CompanySharingLinkCreated",
        "workload": "OneDrive",
        "item_type": "File",
        "original_categories": ["ferpa"],
        "original_tier": "tier_1",
    },
    {
        "event_id": "94eaca99-1306-4603-a325-10ad1ac15bd0",
        "file_name": "SCET Alumni Panelist Options - 3980.xlsx",
        "object_id": "https://uvu365.sharepoint.com/sites/InstitutionalAdvancementHomeBase/Data-Analysis/Institutional Advancement Lists/SCET Alumni Panelist Options - 3980.xlsx",
        "site_url": "https://uvu365.sharepoint.com/sites/InstitutionalAdvancementHomeBase/",
        "user_id": "unknown@unknown.com",
        "sharing_type": "Organization",
        "sharing_scope": "Organization",
        "sharing_permission": "View",
        "operation": "CompanySharingLinkCreated",
        "workload": "SharePoint",
        "item_type": "File",
        "original_categories": ["ferpa", "pii_contact", "directory_info"],
        "original_tier": "tier_1",
    },
    {
        "event_id": "27fe8ba5-2b84-4c89-87b3-0a82e93c6aa9",
        "file_name": "SOE Alumni Past 10 Yrs - 3972.xlsx",
        "object_id": "https://uvu365.sharepoint.com/sites/InstitutionalAdvancementHomeBase/Data-Analysis/Institutional Advancement Lists/SOE Alumni Past 10 Yrs - 3972.xlsx",
        "site_url": "https://uvu365.sharepoint.com/sites/InstitutionalAdvancementHomeBase/",
        "user_id": "unknown@unknown.com",
        "sharing_type": "Organization",
        "sharing_scope": "Organization",
        "sharing_permission": "View",
        "operation": "CompanySharingLinkCreated",
        "workload": "SharePoint",
        "item_type": "File",
        "original_categories": ["ferpa", "pii_contact"],
        "original_tier": "tier_1",
    },
    {
        "event_id": "8336af21-17d3-4739-afc5-46dcf94543e8",
        "file_name": "Contrato de arredamiento de España.docx",
        "object_id": "https://uvu365-my.sharepoint.com/personal/10987636_uvu_edu/Documents/SPAN 4120/Contrato de arredamiento de España.docx",
        "site_url": "https://uvu365-my.sharepoint.com/personal/10987636_uvu_edu/",
        "user_id": "10987636@uvu.edu",
        "sharing_type": "Organization",
        "sharing_scope": "Organization",
        "sharing_permission": "View",
        "operation": "CompanySharingLinkCreated",
        "workload": "OneDrive",
        "item_type": "File",
        "original_categories": ["pii_government_id", "pii_contact"],
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
