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
