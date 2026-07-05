"""Full-site breach review of two formerly-PUBLIC SharePoint sites.

Two SharePoint sites — UVU Ombuds Office and REFUND DECISIONS — were configured
PUBLIC and therefore accessible to every internal user WITHOUT an explicit share.
Because nothing was "shared", these items never produced ShareSentinel sharing
events and are absent from `events`/`verdicts`. Legal counsel needs EVERY file
and EVERY channel message reviewed for data that would require FERPA/HIPAA breach
notification, specifically the presence — in COMPLETE, REAL form — of:

  1. a FULL Social Security Number,
  2. a FULL Driver's License number OR a picture/scan of a driver's license,
  3. a FULL financial account number, OR
  4. HEALTH / PHI information about an identifiable person.

This script:
  (A) enumerates LIVE via Microsoft Graph every file in every drive of both
      sites, plus every Teams channel message (+ replies) of the connected team;
  (B) downloads + extracts each file (text / OCR / image), or renders each
      message to text, and analyzes it with the dedicated `targeted_pii` prompt
      (OpenAI GPT-5.4 by default), now extended to cover HEALTH/PHI; and
  (C) SAVES the original file (or the message text) for legal counsel whenever
      any critical item is found, and records a masked determination in the
      `site_breach_review` table plus a CSV for legal.

It is a standalone investigation: it writes ONLY to `site_breach_review` (+ CSV)
and the legal save dir. It NEVER writes to production `verdicts`, never enrolls
sharing links into the lifecycle tracker, and never notifies analysts.

Usage (inside the worker container):
    docker exec sharesentinel-worker python -m scripts.site_breach_review --dry-run
    docker exec sharesentinel-worker python -m scripts.site_breach_review --limit 5
    docker exec sharesentinel-worker python -m scripts.site_breach_review \
        --model gpt-5.4 --reasoning-effort medium --concurrency 6
"""

import argparse
import asyncio
import csv
import html
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)  # the enumeration is GET-heavy; quiet per-request logs
logger = logging.getLogger("site_breach_review")

import asyncpg
import httpx

from app.ai.base_provider import AnalysisRequest
from app.ai.openai_provider import OpenAIProvider
from app.ai.prompt_manager import PromptManager
from app.config import Config
from app.extraction import get_extractor
from app.extraction.image_preprocessor import preprocess_image, render_pdf_pages_as_images
from app.extraction.ocr_extractor import OcrExtractor
from app.extraction.transcript_extractor import TranscriptExtractor
from app.extraction.whisper_client import WhisperClient
from app.graph_api.auth import GraphAuth
from app.graph_api.client import GraphClient
from app.pipeline.classifier import Action, FileClassifier
from scripts.download_for_review import safe_name
from scripts.targeted_pii_review import _flag, mask_numbers

GRAPH = "https://graph.microsoft.com/v1.0"
TMPFS = "/tmp/sharesentinel/site_breach"
OUTPUT_ROOT = "/app/review_downloads"
SAVE_ROOT = "/app/review_downloads/legal_pii"

SITES = [
    {"key": "UVUOmbudsOffice", "host": "uvu365.sharepoint.com",
     "path": "/sites/UVUOmbudsOffice", "display": "UVU Ombuds Office"},
    {"key": "REFUNDDECISIONS", "host": "uvu365.sharepoint.com",
     "path": "/sites/REFUNDDECISIONS", "display": "REFUND DECISIONS"},
]
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024  # mirror the 50MB pipeline cap

# Reprocess-mode handling for items the normal pass could not extract.
AV_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm", ".m4v",
           ".mp3", ".wav", ".m4a", ".aac", ".flac"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".webp"}
# Types Graph cannot convert to PDF (or that hold no extractable text) — leave for manual review.
NO_CONVERT_EXTS = AV_EXTS | IMAGE_EXTS | {".svg", ".zip", ".7z", ".rar", ".exe", ".dll"}

CSV_COLS = [
    "review_status", "any_critical_found", "site", "source", "analysis_mode_used",
    "download_status", "file_name", "relative_path",
    "full_ssn_found", "ssn_confidence", "ssn_evidence",
    "full_dl_number_found", "dl_number_confidence", "dl_number_evidence",
    "full_dl_image_found", "dl_image_confidence", "dl_image_evidence",
    "full_financial_found", "financial_confidence", "financial_evidence",
    "full_health_found", "health_confidence", "health_evidence",
    "is_coursework", "saved_path", "targeted_reasoning",
    "ai_model", "estimated_cost_usd", "web_url", "item_key",
]


# ----------------------------------------------------------------------
# Graph enumeration helpers (raw httpx; the client lacks site/drive/message APIs)
# ----------------------------------------------------------------------

async def _get(client: httpx.AsyncClient, auth: GraphAuth, url: str) -> tuple[int, dict]:
    resp = await client.get(url, headers={"Authorization": f"Bearer {auth.get_access_token()}"})
    ctype = resp.headers.get("content-type", "")
    return resp.status_code, (resp.json() if ctype.startswith("application/json") else {})


async def _paged(client: httpx.AsyncClient, auth: GraphAuth, url: str):
    """Yield every item across all @odata.nextLink pages of a Graph collection."""
    while url:
        sc, body = await _get(client, auth, url)
        if sc != 200:
            raise RuntimeError(f"Graph {sc} for {url.split('?')[0]}")
        for it in body.get("value", []):
            yield it
        url = body.get("@odata.nextLink")


async def enumerate_site_files(client, auth, site: dict) -> list[dict]:
    """Every FILE (not folder) across every drive of *site*."""
    sc, sresp = await _get(client, auth, f"{GRAPH}/sites/{site['host']}:{site['path']}")
    if sc != 200:
        raise RuntimeError(f"site resolve {sc} for {site['key']}")
    site_id = sresp["id"]
    out: list[dict] = []
    async for d in _paged(client, auth, f"{GRAPH}/sites/{site_id}/drives?$select=id,name,driveType"):
        drive_id, dname = d["id"], d.get("name", "")
        # BFS over folders.
        stack = [f"{GRAPH}/drives/{drive_id}/root/children"
                 "?$top=200&$select=id,name,folder,file,size,webUrl,parentReference"]
        while stack:
            async for it in _paged(client, auth, stack.pop()):
                if it.get("folder"):
                    stack.append(f"{GRAPH}/drives/{drive_id}/items/{it['id']}/children"
                                 "?$top=200&$select=id,name,folder,file,size,webUrl,parentReference")
                    continue
                if not it.get("file"):
                    continue  # notebooks/other non-file driveItems
                if it.get("name", "").startswith("~$"):
                    continue  # Office lock/owner temp files — not real content
                parent_path = (it.get("parentReference") or {}).get("path", "")
                rel = parent_path.split("root:", 1)[-1] if "root:" in parent_path else parent_path
                out.append({
                    "site": site["key"], "source": "file",
                    "drive_id": drive_id, "item_id_graph": it["id"],
                    "file_name": it.get("name", ""),
                    "relative_path": f"{dname}{rel}/{it.get('name', '')}",
                    "file_size_bytes": it.get("size", 0) or 0,
                    "web_url": it.get("webUrl", ""),
                    "item_key": f"file:{drive_id}:{it['id']}",
                })
    return out


def _msg_text(msg: dict) -> str:
    body = (msg.get("body") or {}).get("content", "") or ""
    if (msg.get("body") or {}).get("contentType") == "html":
        body = re.sub(r"<[^>]+>", " ", body)
        body = html.unescape(body)
    return re.sub(r"[ \t]+", " ", body).strip()


async def enumerate_team_messages(client, auth, site: dict) -> list[dict]:
    """Every channel message + reply for *site*'s connected Team (if any)."""
    sc, grp = await _get(
        client, auth,
        f"{GRAPH}/groups?$filter=displayName eq '{site['display']}'"
        "&$select=id,displayName,resourceProvisioningOptions")
    groups = grp.get("value", []) if sc == 200 else []
    out: list[dict] = []
    for g in groups:
        if "Team" not in (g.get("resourceProvisioningOptions") or []):
            continue
        team_id = g["id"]
        async for ch in _paged(client, auth, f"{GRAPH}/teams/{team_id}/channels?$select=id,displayName"):
            ch_id, ch_name = ch["id"], ch.get("displayName", "")
            msg_url = f"{GRAPH}/teams/{team_id}/channels/{ch_id}/messages?$top=50"
            async for m in _paged(client, auth, msg_url):
                items = [(m, None)]
                # Replies live on a sub-collection.
                async for r in _paged(
                    client, auth,
                    f"{GRAPH}/teams/{team_id}/channels/{ch_id}/messages/{m['id']}/replies?$top=50"):
                    items.append((r, m["id"]))
                for msg, parent in items:
                    text = _msg_text(msg)
                    if not text and not (msg.get("attachments")):
                        continue  # system/empty messages
                    sender = (((msg.get("from") or {}).get("user") or {}).get("displayName")
                              or "unknown")
                    when = msg.get("createdDateTime", "")
                    label = (f"Teams reply in {ch_name} from {sender} @ {when}" if parent
                             else f"Teams message in {ch_name} from {sender} @ {when}")
                    out.append({
                        "site": site["key"], "source": "message",
                        "drive_id": "", "item_id_graph": "",
                        "file_name": label,
                        "relative_path": f"{ch_name} channel",
                        "file_size_bytes": len(text.encode("utf-8")),
                        "web_url": msg.get("webUrl", ""),
                        "item_key": (f"msg:{ch_id}:{parent}:{msg['id']}" if parent
                                     else f"msg:{ch_id}:{msg['id']}"),
                        "sender": sender, "when": when, "text": text,
                    })
    return out


# ----------------------------------------------------------------------
# Parsing the targeted (now 4-finding) determination
# ----------------------------------------------------------------------

def parse_targeted(raw_text: str) -> dict | None:
    import json
    cleaned = re.sub(r"^```json\s*|^```\s*|\s*```$", "", raw_text.strip()).strip()
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
    hlt = _flag(parsed.get("full_health_found"))
    any_found = bool(parsed.get("any_critical_found", False)) or any(
        f["found"] for f in (ssn, dln, dli, fin, hlt))
    return {
        "reasoning": mask_numbers(str(parsed.get("reasoning", "")))[:2000],
        "any_critical_found": any_found,
        "ssn": ssn, "dln": dln, "dli": dli, "fin": fin, "hlt": hlt,
        "is_coursework": bool(parsed.get("is_coursework_or_fabricated", False)),
    }


def derive_review_status(t: dict | None) -> str:
    if t is None:
        return "needs_manual_review"
    if not t["any_critical_found"]:
        return "no_critical"
    high = any(t[k]["found"] and t[k]["confidence"] == "high"
               for k in ("ssn", "dln", "dli", "fin", "hlt"))
    return "likely_match" if high else "needs_manual_review"


# ----------------------------------------------------------------------
# Per-candidate processing
# ----------------------------------------------------------------------

def _common_kwargs(rec: dict) -> dict:
    return dict(
        file_name=rec.get("file_name") or "",
        file_path=rec.get("relative_path") or "",
        file_size=rec.get("file_size_bytes") or 0,
        sharing_user=rec.get("sender") or "",
        sharing_type="public_site",
        sharing_permission="all_internal_users",
        event_time=rec.get("when") or "",
    )


def _extract_pdf_request(pdf_path: Path, rec: dict) -> AnalysisRequest | None:
    """Text → OCR → page-image fallback for a (possibly converted) PDF."""
    size = pdf_path.stat().st_size
    extractor = get_extractor(".pdf")
    extraction = extractor.extract(pdf_path, size) if extractor else None
    if extraction and extraction.success and extraction.text_content:
        return AnalysisRequest(mode="targeted_pii", text_content=extraction.text_content,
                               was_sampled=extraction.was_sampled,
                               sampling_description=extraction.sampling_description,
                               **_common_kwargs(rec))
    ocr = OcrExtractor().extract(pdf_path, size)
    if ocr.success and ocr.text_content:
        return AnalysisRequest(mode="targeted_pii", text_content=ocr.text_content, **_common_kwargs(rec))
    images = render_pdf_pages_as_images(pdf_path)
    if images:
        return AnalysisRequest(mode="targeted_pii_multimodal",
                               images=[i.image_bytes for i in images],
                               image_mime_types=[i.mime_type for i in images], **_common_kwargs(rec))
    return None


async def build_request(rec: dict, graph: GraphClient, config: Config, work_dir: Path,
                        result: dict, whisper: WhisperClient | None = None,
                        reprocess: bool = False) -> AnalysisRequest | None:
    """Download/extract a file (or wrap a message) into an AnalysisRequest.

    With ``reprocess=True`` the extra recovery paths run for items the normal
    pass could not read: audio/video are transcribed (Whisper), and any other
    document is converted server-side to PDF via Graph and extracted.
    """
    if rec["source"] == "message":
        result["download_status"] = "downloaded"
        result["analysis_mode_used"] = "targeted_pii"
        return AnalysisRequest(mode="targeted_pii", text_content=rec.get("text", ""),
                               **_common_kwargs(rec))

    fname = rec["file_name"] or "download"
    size = rec.get("file_size_bytes") or 0
    ext = Path(fname).suffix.lower()
    classification = FileClassifier().classify(fname, "File", size, config)

    # ── Reprocess recovery: audio/video → Whisper transcript ──────────
    if reprocess and ext in AV_EXTS:
        if whisper is None:
            result["analysis_mode_used"] = "filename_only"
            result["download_status"] = "skipped"
            return None
        dest = work_dir / safe_name(fname)
        await graph.download_file(rec["drive_id"], rec["item_id_graph"], dest)
        result["download_status"] = "downloaded"
        result["_downloaded_path"] = dest
        wres = await whisper.transcribe(dest)
        text = ((wres or {}).get("text") or "").strip()
        if text:
            extraction = TranscriptExtractor.from_text(
                text, source="whisper", duration_seconds=(wres or {}).get("duration") or None)
            if extraction.success and extraction.text_content:
                result["_transcript_text"] = extraction.text_content
                return AnalysisRequest(mode="targeted_pii", text_content=extraction.text_content,
                                       was_sampled=extraction.was_sampled,
                                       sampling_description=extraction.sampling_description,
                                       **_common_kwargs(rec))
        result["analysis_mode_used"] = "filename_only"  # silent / no usable transcript
        return None

    # ── Normal path (in-place extractors; skipped for oversized/unknown) ──
    if (classification.action not in (Action.FILENAME_ONLY, Action.ARCHIVE_MANIFEST)
            and size <= MAX_DOWNLOAD_BYTES):
        dest = work_dir / safe_name(fname)
        await graph.download_file(rec["drive_id"], rec["item_id_graph"], dest)
        result["download_status"] = "downloaded"
        result["_downloaded_path"] = dest
        if classification.action == Action.MULTIMODAL:
            pre = preprocess_image(dest)
            return AnalysisRequest(mode="targeted_pii_multimodal", images=[pre.image_bytes],
                                   image_mime_types=[pre.mime_type], **_common_kwargs(rec))
        if classification.action == Action.FULL_ANALYSIS:
            extractor = get_extractor(ext)
            extraction = extractor.extract(dest, size) if extractor else None
            if extraction and extraction.success and extraction.text_content:
                return AnalysisRequest(mode="targeted_pii", text_content=extraction.text_content,
                                       was_sampled=extraction.was_sampled,
                                       sampling_description=extraction.sampling_description,
                                       **_common_kwargs(rec))
            if ext == ".pdf":
                req = _extract_pdf_request(dest, rec)
                if req is not None:
                    return req

    # ── Reprocess recovery: convert document → PDF via Graph, then extract ──
    if reprocess and ext not in NO_CONVERT_EXTS and size <= MAX_DOWNLOAD_BYTES:
        # Keep the original on disk so we can save it for legal if flagged.
        orig = work_dir / safe_name(fname)
        if not orig.exists():
            try:
                await graph.download_file(rec["drive_id"], rec["item_id_graph"], orig)
                result["download_status"] = "downloaded"
                result["_downloaded_path"] = orig
            except Exception as exc:  # noqa: BLE001
                logger.warning("[%s] original download failed: %s", rec["item_key"], exc)
        converted = work_dir / (safe_name(fname) + ".converted.pdf")
        try:
            await graph.download_file_converted(rec["drive_id"], rec["item_id_graph"], converted, "pdf")
        except Exception as exc:  # noqa: BLE001 — 406/501 = not convertible
            logger.warning("[%s] convert→pdf failed: %s", rec["item_key"], exc)
            converted = None
        if converted and converted.exists() and converted.stat().st_size > 0:
            req = _extract_pdf_request(converted, rec)
            if req is not None:
                return req

    result["analysis_mode_used"] = "filename_only"
    return None


async def process_one(rec: dict, provider: OpenAIProvider, graph: GraphClient,
                      config: Config, run_id: str, save_dir: Path | None,
                      whisper: WhisperClient | None = None, reprocess: bool = False) -> dict:
    result = {
        "site": rec["site"], "source": rec["source"], "item_key": rec["item_key"],
        "file_name": rec.get("file_name") or "", "drive_id": rec.get("drive_id") or "",
        "item_id_graph": rec.get("item_id_graph") or "", "web_url": rec.get("web_url") or "",
        "relative_path": rec.get("relative_path") or "",
        "file_size_bytes": rec.get("file_size_bytes") or 0,
        "analysis_mode_used": "", "download_status": "skipped", "targeted": None,
        "ai_model": "", "ai_provider": "", "estimated_cost_usd": 0.0,
        "saved_path": "", "error": "", "review_status": "needs_manual_review",
    }
    work_dir = Path(TMPFS) / re.sub(r"[^A-Za-z0-9_.-]", "_", rec["item_key"])[:120]
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        request = await build_request(rec, graph, config, work_dir, result, whisper, reprocess)
        if request is None:
            return result  # download_status / analysis_mode_used already set
        result["analysis_mode_used"] = request.mode
        response = await provider.analyze(request)
        result["ai_model"] = response.model
        result["ai_provider"] = response.provider
        result["estimated_cost_usd"] = response.estimated_cost_usd or 0.0
        if not response.success:
            result["error"] = response.error or "ai_error"
            return result
        result["targeted"] = parse_targeted(response.raw_response)
        result["review_status"] = derive_review_status(result["targeted"])

        # Save the original for legal whenever any critical item is found (any confidence).
        if save_dir is not None and result["targeted"] and result["targeted"]["any_critical_found"]:
            dest_dir = save_dir / run_id / rec["site"]
            dest_dir.mkdir(parents=True, exist_ok=True)
            kid = re.sub(r"[^A-Za-z0-9_.-]", "_", rec["item_key"])[-32:]
            if rec["source"] == "message":
                dest = dest_dir / f"{kid}_{safe_name(rec.get('file_name') or 'message')}.txt"
                dest.write_text(
                    f"Site: {rec['site']}\nChannel item: {rec.get('file_name')}\n"
                    f"URL: {rec.get('web_url')}\n{'-'*60}\n{rec.get('text', '')}\n",
                    encoding="utf-8")
            else:
                src = result.get("_downloaded_path")
                dest = dest_dir / f"{kid}_{safe_name(rec.get('file_name') or 'file')}"
                shutil.copy2(src, dest)
                # For audio/video, also save the transcript that produced the finding.
                if result.get("_transcript_text"):
                    (dest_dir / f"{kid}_{safe_name(rec.get('file_name') or 'file')}.transcript.txt").write_text(
                        result["_transcript_text"], encoding="utf-8")
            result["saved_path"] = str(dest)
            logger.info("[%s] SAVED for legal -> %s", rec["item_key"], dest)
    except Exception as exc:  # noqa: BLE001 — one failure must never abort the batch
        logger.warning("[%s] failed: %s", rec["item_key"], exc)
        result["download_status"] = f"error:{type(exc).__name__}"
        result["error"] = str(exc)[:200]
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
    return result


# ----------------------------------------------------------------------
# Persistence
# ----------------------------------------------------------------------

async def upsert_result(pool: asyncpg.Pool, run_id: str, r: dict) -> None:
    t = r.get("targeted")
    def fb(k, f):
        return (t[k][f] if t else None)
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO site_breach_review (
                run_id, site, source, item_key, file_name, drive_id, item_id_graph,
                web_url, relative_path, file_size_bytes, analysis_mode_used, download_status,
                any_critical_found,
                full_ssn_found, ssn_confidence, ssn_evidence,
                full_dl_number_found, dl_number_confidence, dl_number_evidence,
                full_dl_image_found, dl_image_confidence, dl_image_evidence,
                full_financial_found, financial_confidence, financial_evidence,
                full_health_found, health_confidence, health_evidence,
                is_coursework, ai_provider, ai_model, targeted_reasoning,
                review_status, estimated_cost_usd, saved_path
            ) VALUES (
                $1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,
                $14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,$25,
                $26,$27,$28,$29,$30,$31,$32,$33,$34,$35
            )
            ON CONFLICT (item_key, run_id) DO UPDATE SET
                analysis_mode_used = EXCLUDED.analysis_mode_used,
                download_status = EXCLUDED.download_status,
                review_status = EXCLUDED.review_status,
                any_critical_found = EXCLUDED.any_critical_found,
                full_ssn_found = EXCLUDED.full_ssn_found, ssn_confidence = EXCLUDED.ssn_confidence, ssn_evidence = EXCLUDED.ssn_evidence,
                full_dl_number_found = EXCLUDED.full_dl_number_found, dl_number_confidence = EXCLUDED.dl_number_confidence, dl_number_evidence = EXCLUDED.dl_number_evidence,
                full_dl_image_found = EXCLUDED.full_dl_image_found, dl_image_confidence = EXCLUDED.dl_image_confidence, dl_image_evidence = EXCLUDED.dl_image_evidence,
                full_financial_found = EXCLUDED.full_financial_found, financial_confidence = EXCLUDED.financial_confidence, financial_evidence = EXCLUDED.financial_evidence,
                full_health_found = EXCLUDED.full_health_found, health_confidence = EXCLUDED.health_confidence, health_evidence = EXCLUDED.health_evidence,
                is_coursework = EXCLUDED.is_coursework, targeted_reasoning = EXCLUDED.targeted_reasoning,
                ai_provider = EXCLUDED.ai_provider, ai_model = EXCLUDED.ai_model,
                estimated_cost_usd = EXCLUDED.estimated_cost_usd, saved_path = EXCLUDED.saved_path
            """,
            run_id, r["site"], r["source"], r["item_key"], r["file_name"], r["drive_id"],
            r["item_id_graph"], r["web_url"], r["relative_path"], r["file_size_bytes"],
            r["analysis_mode_used"], r["download_status"],
            (t["any_critical_found"] if t else None),
            fb("ssn", "found"), fb("ssn", "confidence"), fb("ssn", "evidence"),
            fb("dln", "found"), fb("dln", "confidence"), fb("dln", "evidence"),
            fb("dli", "found"), fb("dli", "confidence"), fb("dli", "evidence"),
            fb("fin", "found"), fb("fin", "confidence"), fb("fin", "evidence"),
            fb("hlt", "found"), fb("hlt", "confidence"), fb("hlt", "evidence"),
            (t["is_coursework"] if t else None),
            r["ai_provider"], r["ai_model"], (t["reasoning"] if t else ""),
            r["review_status"], r["estimated_cost_usd"], r["saved_path"],
        )


def csv_row(r: dict) -> dict:
    t = r.get("targeted")
    def g(k, f):
        return (t[k][f] if t else "")
    return {
        "review_status": r["review_status"],
        "any_critical_found": (t["any_critical_found"] if t else ""),
        "site": r["site"], "source": r["source"],
        "analysis_mode_used": r["analysis_mode_used"], "download_status": r["download_status"],
        "file_name": r["file_name"], "relative_path": r["relative_path"],
        "full_ssn_found": g("ssn", "found"), "ssn_confidence": g("ssn", "confidence"),
        "ssn_evidence": g("ssn", "evidence"),
        "full_dl_number_found": g("dln", "found"), "dl_number_confidence": g("dln", "confidence"),
        "dl_number_evidence": g("dln", "evidence"),
        "full_dl_image_found": g("dli", "found"), "dl_image_confidence": g("dli", "confidence"),
        "dl_image_evidence": g("dli", "evidence"),
        "full_financial_found": g("fin", "found"), "financial_confidence": g("fin", "confidence"),
        "financial_evidence": g("fin", "evidence"),
        "full_health_found": g("hlt", "found"), "health_confidence": g("hlt", "confidence"),
        "health_evidence": g("hlt", "evidence"),
        "is_coursework": (t["is_coursework"] if t else ""),
        "saved_path": r["saved_path"], "targeted_reasoning": (t["reasoning"] if t else ""),
        "ai_model": r["ai_model"], "estimated_cost_usd": f"{r['estimated_cost_usd']:.6f}",
        "web_url": r["web_url"], "item_key": r["item_key"],
    }


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Full-site FERPA/HIPAA breach review (GPT-5.4)")
    p.add_argument("--dry-run", action="store_true", help="Enumerate + report only; no downloads or AI")
    p.add_argument("--limit", type=int, default=0, help="Max candidates to process (0 = all)")
    p.add_argument("--source", choices=["all", "file", "message"], default="all")
    p.add_argument("--model", default="gpt-5.4", help="OpenAI model (default gpt-5.4)")
    p.add_argument("--reasoning-effort", default="medium", help="minimal|low|medium|high")
    p.add_argument("--concurrency", type=int, default=6)
    p.add_argument("--out", default=OUTPUT_ROOT)
    p.add_argument("--save-dir", default=SAVE_ROOT)
    p.add_argument("--no-save", action="store_true", help="Verification + CSV only; do not save matched items")
    p.add_argument("--retry-errors-from", default=None, metavar="RUN_ID",
                   help="Reprocess only the transient-download-error (error:ReadError/ConnectError/"
                        "TimeoutError) rows from a prior run_id, upserting back into that SAME run_id.")
    p.add_argument("--reprocess-manual-from", default=None, metavar="RUN_ID",
                   help="Reprocess the needs_manual_review rows from a prior run_id with the extra "
                        "recovery paths (Whisper transcription for audio/video, Graph convert→PDF for "
                        "legacy/odd documents), upserting back into that SAME run_id.")
    return p.parse_args()


TRANSIENT_ERROR_STATUSES = ("error:ReadError", "error:ConnectError",
                            "error:TimeoutError", "error:HTTPStatusError")


_CAND_COLS = ("SELECT site, source, item_key, file_name, drive_id, item_id_graph, web_url, "
              "relative_path, file_size_bytes FROM site_breach_review")


async def load_error_candidates(pool: asyncpg.Pool, run_id: str) -> list[dict]:
    """Rebuild candidate dicts from the stored rows of a prior run's transient errors."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"{_CAND_COLS} WHERE run_id=$1 AND download_status = ANY($2::text[])",
            run_id, list(TRANSIENT_ERROR_STATUSES))
    return [dict(r) for r in rows]


async def load_manual_candidates(pool: asyncpg.Pool, run_id: str) -> list[dict]:
    """Rebuild candidate dicts from a prior run's needs_manual_review file rows."""
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"{_CAND_COLS} WHERE run_id=$1 AND review_status='needs_manual_review' AND source='file'",
            run_id)
    return [dict(r) for r in rows]


async def enumerate_all(auth: GraphAuth, source: str) -> list[dict]:
    cands: list[dict] = []
    async with httpx.AsyncClient(timeout=120) as client:
        for site in SITES:
            if source in ("all", "file"):
                files = await enumerate_site_files(client, auth, site)
                logger.info("%s: %d files", site["key"], len(files))
                cands += files
            if source in ("all", "message"):
                msgs = await enumerate_team_messages(client, auth, site)
                logger.info("%s: %d messages", site["key"], len(msgs))
                cands += msgs
    return cands


async def main() -> None:
    args = parse_args()
    config = Config.from_env()
    if not config.database_url:
        raise SystemExit("ERROR: DATABASE_URL not set.")
    run_id = (args.retry_errors_from or args.reprocess_manual_from
              or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    reprocess = bool(args.reprocess_manual_from)

    auth = GraphAuth(tenant_id=config.azure_tenant_id, client_id=config.azure_client_id,
                     client_secret=config.azure_client_secret,
                     certificate_path=config.azure_certificate_path or None,
                     certificate_password=config.azure_certificate_password or None)

    if args.retry_errors_from:
        load_pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=2)
        rows = await load_error_candidates(load_pool, args.retry_errors_from)
        await load_pool.close()
        logger.info("Retry mode: %d transient-error rows from run %s", len(rows), args.retry_errors_from)
    elif args.reprocess_manual_from:
        load_pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=2)
        rows = await load_manual_candidates(load_pool, args.reprocess_manual_from)
        await load_pool.close()
        logger.info("Reprocess-manual mode: %d needs_manual_review file rows from run %s "
                    "(Whisper + convert→PDF recovery enabled)", len(rows), args.reprocess_manual_from)
    else:
        logger.info("Enumerating sites via Graph (run_id=%s) ...", run_id)
        rows = await enumerate_all(auth, args.source)
    if args.limit > 0:
        rows = rows[: args.limit]
    n_files = sum(1 for r in rows if r["source"] == "file")
    n_msgs = sum(1 for r in rows if r["source"] == "message")
    total_bytes = sum(r.get("file_size_bytes") or 0 for r in rows if r["source"] == "file")
    logger.info("Candidates: %d (files=%d, messages=%d) | ~%.2f GB of files",
                len(rows), n_files, n_msgs, total_bytes / 1024**3)

    if args.dry_run:
        print(f"\n[dry-run] {len(rows)} candidates: files={n_files}, messages={n_msgs}")
        print(f"  approx file download size: {total_bytes / 1024**3:.2f} GB")
        oversized = sum(1 for r in rows if r["source"] == "file" and (r.get("file_size_bytes") or 0) > MAX_DOWNLOAD_BYTES)
        print(f"  files over 50MB (filename-only): {oversized}")
        for site in SITES:
            sf = sum(1 for r in rows if r["site"] == site["key"] and r["source"] == "file")
            sm = sum(1 for r in rows if r["site"] == site["key"] and r["source"] == "message")
            print(f"  {site['key']}: {sf} files, {sm} messages")
        print("  No downloads or AI calls were made.")
        return

    pm = PromptManager(template_dir=config.prompt_template_dir)
    if "targeted_pii" not in pm._templates:
        raise SystemExit("ERROR: targeted_pii prompt mode not loaded — check the template file.")
    provider = OpenAIProvider(api_key=config.openai_api_key, model=args.model,
                              prompt_manager=pm, temperature=0,
                              reasoning_effort=args.reasoning_effort)
    graph = GraphClient(auth=auth)
    whisper = None
    if reprocess:
        whisper = WhisperClient(service_url=config.whisper_service_url)
        if not await whisper.health_check():
            logger.warning("Transcriber service unhealthy at %s — audio/video will fall back "
                           "to filename-only", config.whisper_service_url)
    Path(TMPFS).mkdir(parents=True, exist_ok=True)
    save_dir = None if args.no_save else Path(args.save_dir)
    if save_dir is not None:
        save_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Matched items will be saved for legal under: %s/%s", save_dir, run_id)

    pool = await asyncpg.create_pool(config.database_url, min_size=1, max_size=4)
    sem = asyncio.Semaphore(max(1, args.concurrency))
    results: list[dict] = []
    counters = {"likely_match": 0, "no_critical": 0, "needs_manual_review": 0, "errors": 0}
    rlock = asyncio.Lock()

    async def worker(idx: int, rec: dict) -> None:
        try:
            async with sem:
                r = await process_one(rec, provider, graph, config, run_id, save_dir,
                                      whisper=whisper, reprocess=reprocess)
            try:
                await upsert_result(pool, run_id, r)
            except Exception as exc:  # noqa: BLE001
                logger.error("[%s] upsert failed: %s", rec.get("item_key"), exc)
                r["error"] = f"upsert:{type(exc).__name__}"
        except Exception as exc:  # noqa: BLE001
            logger.exception("[%s] unexpected: %s", rec.get("item_key"), exc)
            r = {"site": rec.get("site", ""), "source": rec.get("source", ""),
                 "item_key": rec.get("item_key", ""), "file_name": rec.get("file_name", ""),
                 "drive_id": "", "item_id_graph": "", "web_url": rec.get("web_url", ""),
                 "relative_path": rec.get("relative_path", ""), "file_size_bytes": 0,
                 "analysis_mode_used": "", "download_status": f"error:{type(exc).__name__}",
                 "targeted": None, "ai_model": "", "ai_provider": "", "estimated_cost_usd": 0.0,
                 "saved_path": "", "error": str(exc)[:200], "review_status": "needs_manual_review"}
        async with rlock:
            results.append(r)
            counters[r["review_status"]] = counters.get(r["review_status"], 0) + 1
            if r.get("error"):
                counters["errors"] += 1
        logger.info("[%d/%d] %s | %s | %s | %s | $%.4f", idx, len(rows), r["review_status"],
                    r["source"], r.get("analysis_mode_used") or "-",
                    (r.get("file_name") or "")[:48], r.get("estimated_cost_usd", 0.0) or 0.0)

    await asyncio.gather(*(worker(i, r) for i, r in enumerate(rows, 1)), return_exceptions=True)
    total_cost = sum((r.get("estimated_cost_usd", 0.0) or 0.0) for r in results)

    conf_rank = {"high": 0, "medium": 1, "low": 2, "": 3}
    def sort_key(r):
        t = r.get("targeted")
        any_crit = 0 if (t and t["any_critical_found"]) else 1
        return (any_crit, conf_rank.get(t["ssn"]["confidence"] if t else "", 3), r["file_name"] or "")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / f"site_breach_review_{run_id}.csv"
    with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS)
        w.writeheader()
        for r in sorted(results, key=sort_key):
            w.writerow(csv_row(r))

    saved = sum(1 for r in results if r.get("saved_path"))
    print("\n" + "=" * 64)
    print("  Full-site FERPA/HIPAA Breach Review complete")
    print("=" * 64)
    print(f"  Run ID:               {run_id}")
    print(f"  Model:                {args.model} (effort={args.reasoning_effort})")
    print(f"  Candidates:           {len(results):>8,}  (files={n_files}, messages={n_msgs})")
    print(f"  Likely match (high):  {counters.get('likely_match', 0):>8,}")
    print(f"  No critical found:    {counters.get('no_critical', 0):>8,}")
    print(f"  Needs manual review:  {counters.get('needs_manual_review', 0):>8,}")
    print(f"  Errors:               {counters.get('errors', 0):>8,}")
    print(f"  Items saved (legal):  {saved:>8,}")
    print(f"  Estimated cost:       ${total_cost:>10.4f}")
    print(f"  CSV:                  {csv_path}")
    if save_dir is not None and saved:
        print(f"  Saved items dir:      {save_dir / run_id}")
    print("=" * 64)
    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
