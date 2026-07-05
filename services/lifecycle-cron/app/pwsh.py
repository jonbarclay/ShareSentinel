"""Shared helper for running the repo's PowerShell scripts from asyncio code.

Used by main.py (CWS loops) and site_policy_scanner.py (site inventory).
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SCRIPTS_DIR = Path("/app/scripts")


async def run_pwsh_script(
    script_name: str,
    args: list[str],
    log_prefix: str,
) -> tuple[int, str, str]:
    """Run a PowerShell script under /app/scripts, returning (rc, stdout, stderr).

    Streams stdout to the python logger so long-running PS jobs are visible in
    `docker compose logs lifecycle-cron`.
    """
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        raise FileNotFoundError(f"PowerShell script not found: {script_path}")

    cmd = ["pwsh", "-NoProfile", "-File", str(script_path), *args]
    logger.info("[%s] launching: %s", log_prefix, " ".join(cmd))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []

    async def _drain(stream: asyncio.StreamReader | None, sink: list[str], level: int) -> None:
        if stream is None:
            return
        while True:
            line_b = await stream.readline()
            if not line_b:
                break
            line = line_b.decode("utf-8", errors="replace").rstrip()
            sink.append(line)
            logger.log(level, "[%s] %s", log_prefix, line)

    await asyncio.gather(
        _drain(proc.stdout, stdout_chunks, logging.INFO),
        _drain(proc.stderr, stderr_chunks, logging.WARNING),
    )
    rc = await proc.wait()
    logger.info("[%s] exited rc=%d", log_prefix, rc)
    return rc, "\n".join(stdout_chunks), "\n".join(stderr_chunks)
