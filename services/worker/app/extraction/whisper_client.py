"""HTTP client for the Whisper transcription microservice.

Sends audio/video files to the ``transcriber`` container's ``POST /transcribe``
endpoint and returns the transcript text and optional keyframes.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# 4-hour timeout for long recordings
_TRANSCRIBE_TIMEOUT = httpx.Timeout(connect=10.0, read=14400.0, write=60.0, pool=30.0)


class WhisperClient:
    """Async HTTP client for the Whisper transcription service."""

    def __init__(self, service_url: str = "http://transcriber:8090") -> None:
        self._base_url = service_url.rstrip("/")

    async def transcribe(self, file_path: Path) -> Optional[dict]:
        """Upload a file to the transcriber and return the result.

        Parameters
        ----------
        file_path:
            Path to the audio or video file.

        Returns
        -------
        dict or None
            On success: ``{"text": "...", "duration": 123.4, "language": "en",
            "frames": [{"image_bytes": bytes, "mime_type": str,
            "timestamp_seconds": float}, ...]}``.
            On failure: None.
        """
        url = f"{self._base_url}/transcribe"
        file_name = file_path.name

        try:
            async with httpx.AsyncClient(timeout=_TRANSCRIBE_TIMEOUT) as client:
                with open(file_path, "rb") as f:
                    files = {"file": (file_name, f, "application/octet-stream")}
                    resp = await client.post(url, files=files)

                if resp.status_code != 200:
                    logger.warning(
                        "Whisper transcription failed HTTP %d for %s: %s",
                        resp.status_code, file_name, resp.text[:300],
                    )
                    return None

                result = resp.json()
                text = result.get("text", "")
                duration = result.get("duration")
                language = result.get("language", "unknown")

                if not text or len(text.strip()) < 50:
                    logger.info("Whisper returned empty/short transcript for %s", file_name)
                    return None

                # Decode base64 frames into bytes for the AI providers
                raw_frames = result.get("frames", [])
                decoded_frames = []
                for frame in raw_frames:
                    try:
                        decoded_frames.append({
                            "image_bytes": base64.b64decode(frame["image_base64"]),
                            "mime_type": frame.get("mime_type", "image/jpeg"),
                            "timestamp_seconds": frame.get("timestamp_seconds", 0),
                        })
                    except Exception:
                        logger.debug("Failed to decode frame from transcriber response")
                result["frames"] = decoded_frames

                logger.info(
                    "Whisper transcription complete for %s: %d chars, %.1fs duration, lang=%s, %d frames",
                    file_name, len(text), duration or 0, language, len(decoded_frames),
                )
                return result

        except httpx.TimeoutException:
            logger.warning("Whisper transcription timed out for %s", file_name)
            return None
        except httpx.ConnectError:
            logger.warning("Cannot connect to Whisper service at %s", self._base_url)
            return None
        except Exception:
            logger.exception("Unexpected error during Whisper transcription of %s", file_name)
            return None

    async def extract_frames(self, file_path: Path) -> Optional[dict]:
        """Upload a video file and extract keyframes only (no transcription).

        Parameters
        ----------
        file_path:
            Path to the video file.

        Returns
        -------
        dict or None
            On success: ``{"duration": 123.4, "frames": [{"image_bytes": bytes,
            "mime_type": str, "timestamp_seconds": float}, ...]}``.
            On failure: None.
        """
        url = f"{self._base_url}/extract_frames"
        file_name = file_path.name

        # Frame extraction itself is fast (ffmpeg seeks + Pillow compress) but
        # the upload may queue behind ongoing Whisper jobs on the single-worker transcriber
        timeout = httpx.Timeout(connect=10.0, read=120.0, write=300.0, pool=60.0)

        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                with open(file_path, "rb") as f:
                    files = {"file": (file_name, f, "application/octet-stream")}
                    resp = await client.post(url, files=files)

                if resp.status_code != 200:
                    logger.warning(
                        "Frame extraction failed HTTP %d for %s: %s",
                        resp.status_code, file_name, resp.text[:300],
                    )
                    return None

                result = resp.json()

                # Decode base64 frames into bytes
                raw_frames = result.get("frames", [])
                decoded_frames = []
                for frame in raw_frames:
                    try:
                        decoded_frames.append({
                            "image_bytes": base64.b64decode(frame["image_base64"]),
                            "mime_type": frame.get("mime_type", "image/jpeg"),
                            "timestamp_seconds": frame.get("timestamp_seconds", 0),
                        })
                    except Exception:
                        logger.debug("Failed to decode frame from extract_frames response")
                result["frames"] = decoded_frames

                logger.info(
                    "Frame extraction complete for %s: %d frames, %.1fs duration",
                    file_name, len(decoded_frames), result.get("duration", 0),
                )
                return result

        except httpx.TimeoutException:
            logger.warning("Frame extraction timed out for %s", file_name)
            return None
        except httpx.ConnectError:
            logger.warning("Cannot connect to transcriber service at %s", self._base_url)
            return None
        except Exception:
            logger.exception("Unexpected error during frame extraction of %s", file_name)
            return None

    async def health_check(self) -> bool:
        """Check if the transcriber service is healthy."""
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                resp = await client.get(f"{self._base_url}/health")
                return resp.status_code == 200
        except Exception:
            return False
