"""Whisper transcription microservice.

A FastAPI HTTP server that accepts audio/video files, extracts audio
with ffmpeg, transcribes with faster-whisper, and returns the text.
Optionally extracts keyframes from video files for multimodal AI analysis.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = FastAPI(title="ShareSentinel Transcriber")

# Configuration from environment
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "base.en")
WHISPER_DEVICE = os.environ.get("WHISPER_DEVICE", "cpu")
WHISPER_COMPUTE_TYPE = os.environ.get("WHISPER_COMPUTE_TYPE", "int8")
MODEL_CACHE_DIR = os.environ.get("MODEL_CACHE_DIR", "/models")
TMPFS_PATH = os.environ.get("TMPFS_PATH", "/tmp/transcriber")

# Audio extensions that don't need ffmpeg extraction
AUDIO_EXTENSIONS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".wma"}

# Video extensions eligible for keyframe extraction
VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".webm", ".m4v"}

# Frame extraction settings
MAX_KEYFRAMES = int(os.environ.get("MAX_KEYFRAMES", "3"))
MAX_IMAGE_DIMENSION = 1600
JPEG_QUALITY = 85

# Lazy-loaded model
_model = None


def _get_model():
    """Lazy-load the Whisper model."""
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        logger.info("Loading Whisper model: %s (device=%s, compute=%s)", WHISPER_MODEL, WHISPER_DEVICE, WHISPER_COMPUTE_TYPE)
        _model = WhisperModel(
            WHISPER_MODEL,
            device=WHISPER_DEVICE,
            compute_type=WHISPER_COMPUTE_TYPE,
            download_root=MODEL_CACHE_DIR,
        )
        logger.info("Whisper model loaded successfully")
    return _model


def _get_video_duration(video_path: Path) -> float | None:
    """Get video duration in seconds using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(video_path),
            ],
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            return float(result.stdout.decode().strip())
    except Exception:
        logger.debug("ffprobe duration query failed for %s", video_path.name, exc_info=True)
    return None


def _compress_frame(image_path: Path) -> dict | None:
    """Load a raw frame, resize/compress to JPEG, return as base64 dict."""
    try:
        with Image.open(image_path) as img:
            # Resize so longest edge <= MAX_IMAGE_DIMENSION
            w, h = img.size
            longest = max(w, h)
            if longest > MAX_IMAGE_DIMENSION:
                scale = MAX_IMAGE_DIMENSION / longest
                img = img.resize(
                    (int(w * scale), int(h * scale)),
                    Image.LANCZOS,
                )

            # Convert to RGB (handles RGBA, palette, etc.)
            if img.mode != "RGB":
                img = img.convert("RGB")

            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=JPEG_QUALITY)
            return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        logger.debug("Frame compression failed for %s", image_path, exc_info=True)
        return None


def _extract_keyframes(
    video_path: Path,
    duration: float,
    work_dir: Path,
    max_frames: int = MAX_KEYFRAMES,
) -> list[dict]:
    """Extract evenly-spaced keyframes from a video file.

    Seeks to 25%, 50%, 75% (for 3 frames) of the video duration,
    compresses each with Pillow, and returns base64-encoded JPEGs.
    Failures are non-fatal — returns as many frames as succeed.
    """
    if duration <= 0 or max_frames < 1:
        return []

    # Calculate evenly-spaced timestamps (e.g., 25%, 50%, 75% for 3 frames)
    positions = [(i + 1) / (max_frames + 1) for i in range(max_frames)]
    timestamps = [duration * p for p in positions]

    frames: list[dict] = []
    for idx, ts in enumerate(timestamps):
        frame_path = work_dir / f"frame_{idx}.jpg"
        try:
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-ss", f"{ts:.2f}",
                    "-i", str(video_path),
                    "-frames:v", "1",
                    "-q:v", "2",  # high quality raw frame
                    "-y",
                    str(frame_path),
                ],
                capture_output=True,
                timeout=30,
            )
            if result.returncode != 0 or not frame_path.exists():
                logger.debug("ffmpeg frame extraction failed at %.1fs", ts)
                continue

            b64_data = _compress_frame(frame_path)
            if b64_data:
                frames.append({
                    "timestamp_seconds": round(ts, 1),
                    "image_base64": b64_data,
                    "mime_type": "image/jpeg",
                })
        except subprocess.TimeoutExpired:
            logger.debug("Frame extraction timed out at %.1fs", ts)
        except Exception:
            logger.debug("Frame extraction failed at %.1fs", ts, exc_info=True)
        finally:
            frame_path.unlink(missing_ok=True)

    logger.info(
        "Extracted %d/%d keyframes from %s (duration=%.1fs)",
        len(frames), max_frames, video_path.name, duration,
    )
    return frames


def _extract_audio(input_path: Path, output_path: Path) -> bool:
    """Extract audio from a video file using ffmpeg.

    Converts to 16kHz mono WAV for optimal Whisper performance.
    """
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-i", str(input_path),
                "-vn",  # no video
                "-acodec", "pcm_s16le",  # 16-bit PCM
                "-ar", "16000",  # 16kHz sample rate
                "-ac", "1",  # mono
                "-y",  # overwrite
                str(output_path),
            ],
            capture_output=True,
            timeout=600,  # 10 minute timeout for extraction
        )
        if result.returncode != 0:
            logger.warning("ffmpeg failed: %s", result.stderr.decode()[-500:])
            return False
        return output_path.exists() and output_path.stat().st_size > 0
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg extraction timed out for %s", input_path.name)
        return False
    except Exception:
        logger.exception("ffmpeg extraction failed for %s", input_path.name)
        return False


@app.post("/transcribe")
async def transcribe(file: UploadFile = File(...)) -> JSONResponse:
    """Transcribe an uploaded audio or video file.

    Returns JSON with ``text``, ``duration``, and ``language``.
    """
    filename = file.filename or "upload"
    ext = Path(filename).suffix.lower()
    work_dir = Path(tempfile.mkdtemp(dir=TMPFS_PATH))

    try:
        # Save uploaded file
        input_path = work_dir / filename
        with open(input_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        file_size = input_path.stat().st_size
        logger.info("Received %s (%d bytes)", filename, file_size)

        is_video = ext in VIDEO_EXTENSIONS

        # Determine audio path
        if ext in AUDIO_EXTENSIONS:
            audio_path = input_path
        else:
            # Video file: extract audio with ffmpeg
            audio_path = work_dir / "audio.wav"
            if not _extract_audio(input_path, audio_path):
                return JSONResponse(
                    status_code=422,
                    content={"error": "Failed to extract audio from video file"},
                )
            # Keep original video for keyframe extraction (cleaned up in finally)

        # Transcribe with Whisper
        model = _get_model()
        segments, info = model.transcribe(
            str(audio_path),
            beam_size=5,
            vad_filter=True,
            vad_parameters=dict(
                min_silence_duration_ms=500,
            ),
        )

        # Collect all segment text
        text_parts: list[str] = []
        for segment in segments:
            text_parts.append(segment.text.strip())

        full_text = " ".join(text_parts)
        duration = info.duration

        logger.info(
            "Transcription complete: %s, %d chars, %.1fs duration, lang=%s (prob=%.2f)",
            filename, len(full_text), duration, info.language, info.language_probability,
        )

        # Extract keyframes from video files
        frames: list[dict] = []
        if is_video and input_path.exists() and duration > 0:
            frames = _extract_keyframes(input_path, duration, work_dir)

        response_data: dict = {
            "text": full_text,
            "duration": duration,
            "language": info.language,
            "language_probability": info.language_probability,
        }
        if frames:
            response_data["frames"] = frames

        return JSONResponse(content=response_data)

    except Exception:
        logger.exception("Transcription failed for %s", filename)
        return JSONResponse(
            status_code=500,
            content={"error": "Internal transcription error"},
        )
    finally:
        # Clean up work directory
        shutil.rmtree(work_dir, ignore_errors=True)


@app.post("/extract_frames")
async def extract_frames(file: UploadFile = File(...)) -> JSONResponse:
    """Extract keyframes from a video file without transcription.

    Returns JSON with ``frames`` list and ``duration``.
    For future Path A support where video is available but transcription
    was already obtained via Graph API.
    """
    filename = file.filename or "upload"
    ext = Path(filename).suffix.lower()

    if ext not in VIDEO_EXTENSIONS:
        return JSONResponse(
            status_code=422,
            content={"error": f"Not a supported video format: {ext}"},
        )

    work_dir = Path(tempfile.mkdtemp(dir=TMPFS_PATH))
    try:
        input_path = work_dir / filename
        with open(input_path, "wb") as f:
            shutil.copyfileobj(file.file, f)

        file_size = input_path.stat().st_size
        logger.info("Frame extraction request: %s (%d bytes)", filename, file_size)

        duration = _get_video_duration(input_path)
        if not duration or duration <= 0:
            return JSONResponse(
                status_code=422,
                content={"error": "Could not determine video duration"},
            )

        frames = _extract_keyframes(input_path, duration, work_dir)
        return JSONResponse(content={
            "frames": frames,
            "duration": duration,
        })
    except Exception:
        logger.exception("Frame extraction failed for %s", filename)
        return JSONResponse(
            status_code=500,
            content={"error": "Internal frame extraction error"},
        )
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "model": WHISPER_MODEL}


@app.on_event("startup")
async def startup():
    """Pre-create tmpfs directory and optionally pre-load the model."""
    Path(TMPFS_PATH).mkdir(parents=True, exist_ok=True)
    logger.info("Transcriber starting (model=%s)", WHISPER_MODEL)
    # Pre-load model to avoid first-request latency
    try:
        _get_model()
    except Exception:
        logger.warning("Failed to pre-load Whisper model", exc_info=True)
