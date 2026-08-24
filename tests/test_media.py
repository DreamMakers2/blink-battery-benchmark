from io import BytesIO
import asyncio

import pytest
from PIL import Image

from blink_dashboard.media import HlsStreamConsumer, atomic_write_jpeg, utc_now_iso, verify_jpeg


def make_jpeg() -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (8, 8), "orange").save(buffer, format="JPEG")
    return buffer.getvalue()


def test_verify_jpeg_accepts_decodable_jpeg() -> None:
    verify_jpeg(make_jpeg())


@pytest.mark.parametrize("payload", [b"", b"not jpeg", b"\xff\xd8truncated"])
def test_verify_jpeg_rejects_invalid_payload(payload: bytes) -> None:
    with pytest.raises(ValueError):
        verify_jpeg(payload)


async def test_atomic_write_jpeg_keeps_last_good_image(tmp_path) -> None:
    target = tmp_path / "latest.jpg"
    good = make_jpeg()
    await atomic_write_jpeg(target, good)
    with pytest.raises(ValueError):
        await atomic_write_jpeg(target, b"broken")
    assert target.read_bytes() == good


def test_hls_readiness_requires_recent_stream_data(tmp_path) -> None:
    consumer = HlsStreamConsumer(
        host="127.0.0.1", port=5000, hls_dir=tmp_path, reconnect_delay=0.001
    )
    consumer.manifest.write_text("#EXTM3U\n", encoding="utf-8")
    assert not consumer.is_fresh()
    consumer.last_data_at = utc_now_iso()
    assert consumer.is_fresh()
    consumer.last_data_at = "2000-01-01T00:00:00Z"
    assert not consumer.is_fresh()


def test_hls_consumer_requires_positive_read_timeout(tmp_path) -> None:
    with pytest.raises(ValueError, match="read_timeout must be greater than zero"):
        HlsStreamConsumer(host="127.0.0.1", port=5000, hls_dir=tmp_path, read_timeout=0)


async def test_hls_consumer_reports_initial_ffmpeg_start_failure(monkeypatch, tmp_path) -> None:
    consumer = HlsStreamConsumer(
        host="127.0.0.1", port=5000, hls_dir=tmp_path, reconnect_delay=0.001
    )
    stop_event = asyncio.Event()
    errors = []

    async def fail_start():
        raise FileNotFoundError("ffmpeg missing")

    def on_error(category, message):
        errors.append((category, message))
        stop_event.set()

    monkeypatch.setattr(consumer, "_start_ffmpeg", fail_start)
    await asyncio.wait_for(
        consumer.run(stop_event, on_error=on_error),
        timeout=1,
    )

    assert errors == [("ffmpeg_failure", "ffmpeg missing")]


async def test_hls_consumer_reports_silent_stream_inactivity(monkeypatch, tmp_path) -> None:
    class FakeStdin:
        def close(self):
            return None

        async def wait_closed(self):
            return None

    class FakeProcess:
        returncode = None
        stdin = FakeStdin()
        stderr = None

        async def wait(self):
            return 0

    class SilentReader:
        async def read(self, _size):
            await asyncio.Event().wait()

    class FakeWriter:
        def close(self):
            return None

        async def wait_closed(self):
            return None

    consumer = HlsStreamConsumer(
        host="127.0.0.1",
        port=5000,
        hls_dir=tmp_path,
        read_timeout=0.001,
        reconnect_delay=0.001,
    )
    stop_event = asyncio.Event()
    errors = []

    async def start_ffmpeg():
        consumer.process = FakeProcess()

    async def open_connection(_host, _port):
        return SilentReader(), FakeWriter()

    def on_error(category, message):
        errors.append((category, message))
        stop_event.set()

    monkeypatch.setattr(consumer, "_start_ffmpeg", start_ffmpeg)
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    await asyncio.wait_for(
        consumer.run(stop_event, on_error=on_error),
        timeout=1,
    )

    assert errors == [("stream_inactivity", "No stream data received for 0.001 seconds")]


async def test_hls_consumer_restarts_exited_ffmpeg(monkeypatch, tmp_path) -> None:
    class FakeStdin:
        def write(self, _chunk):
            return None

        async def drain(self):
            return None

        def close(self):
            return None

        async def wait_closed(self):
            return None

    class FakeProcess:
        def __init__(self, returncode):
            self.returncode = returncode
            self.stdin = FakeStdin()
            self.stderr = None

        async def wait(self):
            return self.returncode or 0

        def terminate(self):
            self.returncode = 1

        def kill(self):
            self.returncode = 1

    class FakeReader:
        async def read(self, _size):
            return b"stream bytes"

    class FakeWriter:
        def close(self):
            return None

        async def wait_closed(self):
            return None

    consumer = HlsStreamConsumer(
        host="127.0.0.1", port=5000, hls_dir=tmp_path, reconnect_delay=0.001
    )
    starts = 0

    async def start_ffmpeg():
        nonlocal starts
        starts += 1
        consumer.process = FakeProcess(1 if starts == 1 else None)

    async def open_connection(_host, _port):
        return FakeReader(), FakeWriter()

    monkeypatch.setattr(consumer, "_start_ffmpeg", start_ffmpeg)
    monkeypatch.setattr(asyncio, "open_connection", open_connection)
    stop_event = asyncio.Event()
    categories = []

    def on_bytes(_count, _timestamp):
        stop_event.set()

    await asyncio.wait_for(
        consumer.run(
            stop_event,
            on_bytes=on_bytes,
            on_error=lambda category, _message: categories.append(category),
        ),
        timeout=1,
    )
    assert starts == 2
    assert "ffmpeg_failure" in categories
