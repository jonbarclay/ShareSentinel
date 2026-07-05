"""Transcribe + AI-analyse the already-downloaded review videos.

The formerly-public-sites backfill skipped audio/video (the worker would have
routed them to the transcription pipeline, but the backfill never enqueued
them). We already downloaded the 131 surviving videos to
``review_downloads/video/`` for human review. This script runs each of those
LOCAL files through ShareSentinel's real transcription + analysis components —
no re-download from Graph — and writes a CSV of the AI categorisation.

Reuses, unchanged:
  * ``WhisperClient``        — uploads the local file to the transcriber service
  * ``TranscriptExtractor``  — wraps the transcript into an ExtractionResult
  * the configured AI provider (``create_ai_provider``) + prompt templates

For each video: Whisper transcript (+ keyframes) -> transcript / transcript_
multimodal AnalysisRequest -> AI analysis. Videos with no usable transcript
(silent / music-only) fall back to filename-only analysis so they still get a
categorisation. Results stream to a CSV (one row flushed per video) and each
transcript is saved as a sidecar ``.transcript.txt``.

This is a standalone review artdefact: it does NOT create events/verdicts in
the database (the videos were never enqueued). Run inside the worker container:

    docker exec sharesentinel-worker python -m scripts.transcribe_videos_for_review --limit 2
    docker exec sharesentinel-worker python -m scripts.transcribe_videos_for_review
"""

import argparse
import asyncio
import csv
import json
import logging
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("transcribe_videos_for_review")

from app.config import Config
from app.ai.base_provider import AnalysisRequest
from app.extraction.transcript_extractor import TranscriptExtractor
from app.extraction.whisper_client import WhisperClient
from app.main import create_ai_provider

VIDEO_ROOT = "/app/review_downloads/video"
MANIFEST = "/app/review_downloads/review_manifest.csv"
OUTPUT_CSV = "/app/review_downloads/video/video_ai_analysis.csv"

OUTPUT_COLUMNS = [
    "site", "file_name", "transcript_status", "transcript_source", "language",
    "duration_seconds", "transcript_chars", "analysis_mode", "frames_used",
    "flagged", "escalation_tier", "risk_score", "categories_detected",
    "overall_context", "data_recency", "ai_summary", "recommendation",
    "ai_provider", "ai_model", "est_cost_usd", "transcript_file",
    "local_path", "object_id", "web_url",
]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Transcribe + AI-analyse review videos")
    p.add_argument("--manifest", default=MANIFEST, help="review_manifest.csv path")
    p.add_argument("--out", default=OUTPUT_CSV, help="Output CSV path")
    p.add_argument("--limit", type=int, default=0, help="Process at most N videos (0 = all)")
    p.add_argument("--skip-frames", action="store_true",
                   help="Ignore keyframes (transcript-only, cheaper AI calls)")
    return p.parse_args()


def load_video_targets(manifest_path: str) -> list[dict]:
    """Return downloaded video rows from the review manifest."""
    targets: list[dict] = []
    with open(manifest_path, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r.get("category") == "video" and r.get("status") == "downloaded":
                lp = r.get("local_path", "")
                if lp and Path(lp).exists():
                    targets.append(r)
                else:
                    logger.warning("Listed video missing on disk: %s", lp)
    return targets


def _cats_to_str(categories) -> str:
    return "; ".join(c.id for c in categories) if categories else ""


async def process_video(
    rec: dict, config: Config, ai_provider, whisper: WhisperClient, use_frames: bool,
) -> dict:
    """Transcribe one local video and run AI analysis. Returns a CSV row dict."""
    local_path = Path(rec["local_path"])
    site = rec.get("site", "")
    file_name = rec.get("file_name", local_path.name)
    object_id = rec.get("object_id", "")
    web_url = rec.get("web_url", "")
    file_size = local_path.stat().st_size if local_path.exists() else 0

    row = {c: "" for c in OUTPUT_COLUMNS}
    row.update({
        "site": site, "file_name": file_name, "local_path": str(local_path),
        "object_id": object_id, "web_url": web_url,
    })

    # --- 1. Transcribe the local file ---
    result = await whisper.transcribe(local_path)

    transcript_text = (result or {}).get("text", "") if result else ""
    duration = int((result or {}).get("duration") or 0) if result else 0
    language = (result or {}).get("language", "") if result else ""
    frames = (result or {}).get("frames", []) if result else []

    transcript_file = ""
    if transcript_text:
        # Save sidecar transcript next to the video.
        tpath = local_path.with_suffix(local_path.suffix + ".transcript.txt")
        try:
            tpath.write_text(transcript_text, encoding="utf-8")
            transcript_file = str(tpath)
        except Exception:
            logger.warning("Could not write transcript file for %s", file_name, exc_info=True)

    # --- 2. Build the analysis request ---
    common = dict(
        file_name=file_name,
        file_path=object_id,
        file_size=file_size,
        sharing_user="",
        sharing_type="FormerlyPublicSite",
        sharing_permission="View",
        event_time="",
        filename_flagged=False,
        filename_flag_keywords=[],
    )

    if transcript_text:
        extraction = TranscriptExtractor.from_text(
            transcript_text, source="whisper", duration_seconds=duration or None,
        )
        if extraction.success and extraction.text_content:
            wanted_frames = frames if (use_frames and config.video_frame_extraction_enabled) else []
            file_meta = dict(extraction.metadata)
            if wanted_frames:
                file_meta["frame_count"] = len(wanted_frames)
                file_meta["frame_timestamps"] = [f["timestamp_seconds"] for f in wanted_frames]
            mode = "transcript_multimodal" if wanted_frames else "transcript"
            request = AnalysisRequest(
                mode=mode,
                text_content=extraction.text_content,
                was_sampled=extraction.was_sampled,
                sampling_description=extraction.sampling_description,
                file_metadata=file_meta,
                images=[f["image_bytes"] for f in wanted_frames] if wanted_frames else None,
                image_mime_types=[f["mime_type"] for f in wanted_frames] if wanted_frames else None,
                **common,
            )
            transcript_status = "transcribed"
        else:
            request = AnalysisRequest(mode="filename_only",
                                      file_metadata={"classification_reason": "transcript_empty_after_parse"},
                                      **common)
            transcript_status = "no_usable_transcript"
    else:
        request = AnalysisRequest(mode="filename_only",
                                  file_metadata={"classification_reason": "no_transcript"},
                                  **common)
        transcript_status = "no_transcript"

    # --- 3. AI analysis ---
    resp = await ai_provider.analyze(request)

    row.update({
        "transcript_status": transcript_status,
        "transcript_source": "whisper" if transcript_text else "",
        "language": language,
        "duration_seconds": duration or "",
        "transcript_chars": len(transcript_text),
        "analysis_mode": request.mode,
        "frames_used": len(request.images) if request.images else 0,
        "flagged": "TRUE" if resp.escalation_tier in ("tier_1", "tier_2") else "FALSE",
        "escalation_tier": resp.escalation_tier,
        "risk_score": resp.risk_score,
        "categories_detected": _cats_to_str(resp.categories),
        "overall_context": resp.context or "",
        "data_recency": resp.data_recency or "",
        "ai_summary": resp.summary or "",
        "recommendation": resp.recommendation or "",
        "ai_provider": resp.provider or "",
        "ai_model": resp.model or "",
        "est_cost_usd": f"{resp.estimated_cost_usd:.6f}",
        "transcript_file": transcript_file,
    })
    return row


async def main() -> None:
    args = parse_args()
    config = Config.from_env()

    whisper = WhisperClient(service_url=config.whisper_service_url)
    if not await whisper.health_check():
        print("ERROR: transcriber service is not healthy at", config.whisper_service_url)
        sys.exit(1)

    targets = load_video_targets(args.manifest)
    if args.limit > 0:
        targets = targets[: args.limit]
    logger.info("Videos to process: %d", len(targets))
    if not targets:
        print("No downloaded videos found in manifest.")
        return

    ai_provider = create_ai_provider(config)
    use_frames = not args.skip_frames

    # Stream results to CSV: write + flush per video so partial progress
    # survives interruption (transcription of 131 videos is long-running).
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    flagged = 0
    transcribed = 0
    errors = 0
    total_cost = 0.0

    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()
        f.flush()

        for i, rec in enumerate(targets, 1):
            name = rec.get("file_name", "?")
            logger.info("[%d/%d] %s", i, len(targets), name)
            try:
                row = await process_video(rec, config, ai_provider, whisper, use_frames)
            except Exception:
                errors += 1
                logger.exception("[%d/%d] FAILED %s", i, len(targets), name)
                row = {c: "" for c in OUTPUT_COLUMNS}
                row.update({
                    "site": rec.get("site", ""), "file_name": name,
                    "local_path": rec.get("local_path", ""),
                    "object_id": rec.get("object_id", ""),
                    "web_url": rec.get("web_url", ""),
                    "transcript_status": "error", "analysis_mode": "error",
                })
            writer.writerow(row)
            f.flush()

            if row.get("transcript_status") == "transcribed":
                transcribed += 1
            if row.get("flagged") == "TRUE":
                flagged += 1
            try:
                total_cost += float(row.get("est_cost_usd") or 0)
            except ValueError:
                pass
            logger.info(
                "    -> mode=%s tier=%s score=%s cats=[%s]",
                row.get("analysis_mode"), row.get("escalation_tier"),
                row.get("risk_score"), row.get("categories_detected"),
            )

    print()
    print("=" * 56)
    print("  Video transcript analysis complete")
    print("=" * 56)
    print(f"  Videos processed:           {len(targets):>8,}")
    print(f"  Transcribed (with speech):  {transcribed:>8,}")
    print(f"  Flagged (Tier 1/2):         {flagged:>8,}")
    print(f"  Errors:                     {errors:>8,}")
    print(f"  Est. AI cost:               ${total_cost:>9.4f}")
    print(f"  Output CSV:                 {args.out}")
    print("=" * 56)


if __name__ == "__main__":
    asyncio.run(main())
