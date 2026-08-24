"""Snapshot validation and local HLS conversion."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from PIL import Image, UnidentifiedImageError

LOGGER = logging.getLogger(__name__)

AsyncCallback = Callable[..., Awaitable[None] | None]


def utc_now_iso() -> str:
    """Return a compact UTC timestamp suitable for JSON and persistence."""
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def verify_jpeg(payload: bytes) -> None:
    """Raise ValueError unless *payload* is a complete, decodable JPEG."""
    if len(payload) < 4 or not payload.startswith(b"\xff\xd8") or not payload.endswith(b"\xff\xd9"):
        raise ValueError("response does not contain complete JPEG markers")
    try:
        with Image.open(BytesIO(payload)) as image:
            if image.format != "JPEG":
                raise ValueError(f"expected JPEG, received {image.format or 'unknown'}")
            image.verify()
    except (UnidentifiedImageError, OSError) as exc:
        raise ValueError("response is not a decodable JPEG") from exc


async def atomic_write_jpeg(path: Path, payload: bytes) -> str:
    """Validate and atomically replace the latest JPEG, returning its timestamp."""
    await asyncio.to_thread(verify_jpeg, payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    await asyncio.to_thread(temporary.write_bytes, payload)
    await asyncio.to_thread(os.replace, temporary, path)
    return utc_now_iso()


async def _invoke(callback: AsyncCallback | None, *args: object) -> None:
    if callback is None:
        return
    result = callback(*args)
    if inspect.isawaitable(result):
        await result


class HlsStreamConsumer:
    """Consume one local MPEG-TS socket and tee it into FFmpeg HLS output."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        hls_dir: Path,
        ffmpeg: str = "ffmpeg",
        connect_timeout: float = 10,
        read_timeout: float = 20,
        reconnect_delay: float = 2,
        segment_seconds: int = 2,
        list_size: int = 6,
    ) -> None:
        self.host = host
        self.port = port
        self.hls_dir = hls_dir
        self.ffmpeg = ffmpeg
        self.connect_timeout = connect_timeout
        if read_timeout <= 0:
            raise ValueError("read_timeout must be greater than zero")
        self.read_timeout = read_timeout
        self.reconnect_delay = reconnect_delay
        self.segment_seconds = segment_seconds
        self.list_size = list_size
        self.process: asyncio.subprocess.Process | None = None
        self.last_data_at: str | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._stderr_task: asyncio.Task[None] | None = None

    @property
    def manifest(self) -> Path:
        return self.hls_dir / "index.m3u8"

    @property
    def ready(self) -> bool:
        try:
            return self.manifest.is_file() and self.manifest.stat().st_size > 0
        except OSError:
            return False

    def is_fresh(self, max_age_seconds: float = 30) -> bool:
        """Return whether HLS output exists and stream bytes arrived recently."""

        if not self.ready or self.last_data_at is None:
            return False
        try:
            received = datetime.fromisoformat(self.last_data_at.replace("Z", "+00:00"))
        except ValueError:
            return False
        age = (datetime.now(timezone.utc) - received.astimezone(timezone.utc)).total_seconds()
        return 0 <= age <= max_age_seconds

    def clear_output(self) -> None:
        """Remove only generated HLS artifacts from the configured HLS directory."""
        self.hls_dir.mkdir(parents=True, exist_ok=True)
        for candidate in self.hls_dir.iterdir():
            if candidate.is_file() and candidate.suffix.lower() in {".ts", ".m3u8", ".tmp"}:
                candidate.unlink(missing_ok=True)

    async def _start_ffmpeg(self) -> None:
        if self.process and self.process.returncode is None:
            return
        self.clear_output()
        segment_pattern = str(self.hls_dir / "segment-%05d.ts")
        command = [
            self.ffmpeg,
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts+nobuffer",
            "-f",
            "mpegts",
            "-i",
            "pipe:0",
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            str(self.segment_seconds),
            "-hls_list_size",
            str(self.list_size),
            "-hls_flags",
            "delete_segments+append_list+omit_endlist+independent_segments",
            "-hls_segment_filename",
            segment_pattern,
            str(self.manifest),
        ]
        self.process = await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.create_task(
            self._drain_ffmpeg_stderr(self.process), name="ffmpeg-stderr"
        )

    @staticmethod
    async def _drain_ffmpeg_stderr(process: asyncio.subprocess.Process) -> None:
        if process.stderr is None:
            return
        while line := await process.stderr.readline():
            LOGGER.warning("FFmpeg: %s", line.decode(errors="replace").rstrip())

    async def run(
        self,
        stop_event: asyncio.Event,
        *,
        on_bytes: AsyncCallback | None = None,
        on_reconnect: AsyncCallback | None = None,
        on_error: AsyncCallback | None = None,
    ) -> None:
        """Run until *stop_event* is set, reconnecting without a second stream client."""
        reconnects = 0
        try:
            while not stop_event.is_set():
                waiting_for_data = False
                try:
                    await self._start_ffmpeg()
                    self._reader, self._writer = await asyncio.wait_for(
                        asyncio.open_connection(self.host, self.port), self.connect_timeout
                    )
                    waiting_for_data = True
                    if reconnects:
                        await _invoke(on_reconnect)
                    reconnects += 1
                    while not stop_event.is_set():
                        chunk = await asyncio.wait_for(
                            self._reader.read(64 * 1024), timeout=self.read_timeout
                        )
                        if not chunk:
                            raise ConnectionError("Blink stream closed")
                        if (
                            not self.process
                            or self.process.returncode is not None
                            or not self.process.stdin
                        ):
                            raise RuntimeError("FFmpeg exited while the stream was active")
                        self.process.stdin.write(chunk)
                        await self.process.stdin.drain()
                        self.last_data_at = utc_now_iso()
                        await _invoke(on_bytes, len(chunk), self.last_data_at)
                except asyncio.CancelledError:
                    raise
                except asyncio.TimeoutError:
                    ffmpeg_failed = self.process is None or self.process.returncode is not None
                    if ffmpeg_failed:
                        category = "ffmpeg_failure"
                        message = "FFmpeg exited while waiting for stream data"
                    elif waiting_for_data:
                        category = "stream_inactivity"
                        message = f"No stream data received for {self.read_timeout:g} seconds"
                    else:
                        category = "stream_disconnect"
                        message = (
                            f"Stream connection timed out after {self.connect_timeout:g} seconds"
                        )
                    await _invoke(on_error, category, message)
                    await self._recover_after_failure(stop_event, ffmpeg_failed)
                except Exception as exc:  # reconnect path is intentionally broad
                    ffmpeg_failed = self.process is None or self.process.returncode is not None
                    category = "ffmpeg_failure" if ffmpeg_failed else "stream_disconnect"
                    await _invoke(on_error, category, str(exc))
                    await self._recover_after_failure(stop_event, ffmpeg_failed)
        finally:
            await self.stop()

    async def _recover_after_failure(self, stop_event: asyncio.Event, ffmpeg_failed: bool) -> None:
        await self._close_socket()
        if ffmpeg_failed:
            await self._stop_ffmpeg()
        if not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), self.reconnect_delay)
            except asyncio.TimeoutError:
                pass

    async def _close_socket(self) -> None:
        if self._writer:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
        self._reader = None
        self._writer = None

    async def stop(self) -> None:
        await self._close_socket()
        await self._stop_ffmpeg()

    async def _stop_ffmpeg(self) -> None:
        process, self.process = self.process, None
        stderr_task, self._stderr_task = self._stderr_task, None
        if not process:
            if stderr_task is not None:
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task
            return
        if process.stdin:
            process.stdin.close()
            with contextlib.suppress(Exception):
                await process.stdin.wait_closed()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except asyncio.TimeoutError:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if stderr_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await stderr_task
