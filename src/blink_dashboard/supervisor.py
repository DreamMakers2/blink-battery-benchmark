"""Windows-friendly lifecycle management for the adapter and dashboard."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path
import secrets
import sys
import tempfile
import time
import webbrowser

import aiohttp

from .config import AppConfig


LOGGER = logging.getLogger(__name__)


class StartupError(RuntimeError):
    """Raised when a managed local service cannot become ready."""


def load_or_create_dashboard_token(path: Path) -> str:
    """Load or atomically create the bearer used only for explicit LAN mode."""

    try:
        existing = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        existing = ""
    if len(existing) >= 32:
        return existing
    token = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f"{path.name}.tmp-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            descriptor = -1
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        temporary_name = ""
        with contextlib.suppress(OSError):
            path.chmod(0o600)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_name:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(temporary_name)
    return token


async def dashboard_is_running(url: str) -> bool:
    """Return whether a compatible dashboard already owns the configured port."""

    timeout = aiohttp.ClientTimeout(total=1)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{url.rstrip('/')}/healthz") as response:
                if response.status != 200:
                    return False
                payload = await response.json(content_type=None)
                return payload.get("service") == "blink-battery-dashboard"
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        return False


async def wait_for_adapter(
    base_url: str,
    process: asyncio.subprocess.Process,
    *,
    timeout_seconds: float | None = None,
    poll_interval_seconds: float = 0.4,
) -> dict[str, object]:
    """Wait for the interactive or retrying adapter to expose its API.

    The production default has no wall-clock deadline: a running adapter may be
    waiting for a user MFA response or retrying a temporary Blink outage.  A
    finite timeout remains injectable for tests and explicit callers.  Child
    exit and task cancellation still end the wait immediately.
    """

    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    timeout = aiohttp.ClientTimeout(total=2)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while deadline is None or time.monotonic() < deadline:
            return_code = process.returncode
            if return_code is not None:
                raise StartupError(f"Blink adapter exited during setup (code {return_code}).")
            try:
                async with session.get(f"{base_url.rstrip('/')}/") as response:
                    payload = await response.json(content_type=None)
                    if response.status == 200 and payload.get("status") in {"ok", "degraded"}:
                        return payload
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                pass
            await asyncio.sleep(poll_interval_seconds)
    raise StartupError(f"Blink setup did not finish within {timeout_seconds:g} seconds.")


async def start_adapter(project_root: Path, config: AppConfig) -> asyncio.subprocess.Process:
    """Start the pinned adapter with inherited console input for credential prompts."""

    command = [
        sys.executable,
        "-m",
        "blink_dashboard.adapter_main",
        "--project-root",
        str(project_root),
        "--http-port",
        str(_port_from_url(config.blink.http_base_url)),
        "--tcp-port",
        str(config.blink.stream_port),
        "--stream-start-timeout",
        str(config.blink.stream_connect_timeout_seconds),
    ]
    LOGGER.info("Starting loopback Blink adapter")
    return await asyncio.create_subprocess_exec(*command, cwd=project_root)


def _port_from_url(url: str) -> int:
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise StartupError("blink.http_base_url must be an HTTP loopback URL")
    return parsed.port or 80


async def stop_process(process: asyncio.subprocess.Process | None) -> None:
    """Terminate a child and escalate only if it does not exit promptly."""

    if process is None or process.returncode is not None:
        return
    process.terminate()
    try:
        await asyncio.wait_for(process.wait(), timeout=8)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()


def open_dashboard(url: str) -> None:
    """Open the dashboard without treating browser integration as fatal."""

    try:
        if not webbrowser.open(url, new=2):
            LOGGER.warning("No default browser accepted the dashboard URL: %s", url)
    except Exception:
        LOGGER.warning("Could not open the default browser; visit %s", url, exc_info=True)


@contextlib.asynccontextmanager
async def managed_adapter(project_root: Path, config: AppConfig):
    process = await start_adapter(project_root, config)
    try:
        await wait_for_adapter(config.blink.http_base_url, process)
        yield process
    finally:
        await stop_process(process)


__all__ = [
    "StartupError",
    "dashboard_is_running",
    "load_or_create_dashboard_token",
    "managed_adapter",
    "open_dashboard",
    "start_adapter",
    "stop_process",
    "wait_for_adapter",
]
