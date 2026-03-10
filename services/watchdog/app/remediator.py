"""Docker container restart via the Docker SDK."""

from __future__ import annotations

import asyncio
import logging

import docker

logger = logging.getLogger(__name__)


class ContainerRemediator:
    """Restarts Docker containers via the Docker socket."""

    def __init__(self) -> None:
        self._client: docker.DockerClient | None = None

    def _get_client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    async def restart_container(self, name: str, timeout: int = 30) -> bool:
        """Restart a container by name. Returns True on success.

        Uses run_in_executor to avoid blocking the event loop
        (same pattern as SMTP calls in the worker).
        """
        loop = asyncio.get_running_loop()
        try:
            success = await loop.run_in_executor(
                None, self._restart_sync, name, timeout
            )
            return success
        except Exception:
            logger.exception("Failed to restart container %s", name)
            return False

    def _restart_sync(self, name: str, timeout: int) -> bool:
        """Synchronous container restart (runs in thread pool)."""
        client = self._get_client()
        try:
            container = client.containers.get(name)
            logger.info(
                "Restarting container %s (status=%s, timeout=%ds)",
                name, container.status, timeout,
            )
            container.restart(timeout=timeout)
            logger.info("Container %s restarted successfully", name)
            return True
        except docker.errors.NotFound:
            logger.error("Container %s not found", name)
            return False
        except docker.errors.APIError:
            logger.exception("Docker API error restarting %s", name)
            return False

    async def get_container_status(self, name: str) -> str | None:
        """Get the current status of a container. Returns None if not found."""
        loop = asyncio.get_running_loop()
        try:
            return await loop.run_in_executor(
                None, self._get_status_sync, name
            )
        except Exception:
            logger.warning("Failed to get status for container %s", name, exc_info=True)
            return None

    def _get_status_sync(self, name: str) -> str | None:
        client = self._get_client()
        try:
            container = client.containers.get(name)
            return container.status
        except docker.errors.NotFound:
            return None
