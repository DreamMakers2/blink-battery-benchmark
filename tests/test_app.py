from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
import pytest

from blink_dashboard.app import ACCESS_COOKIE, DashboardApplication, create_web_app
from blink_dashboard.config import load_config


ROOT = Path(__file__).resolve().parents[1]


def jpeg_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), color=(255, 130, 61)).save(output, format="JPEG")
    return output.getvalue()


class FakeAdapterClient:
    def __init__(self) -> None:
        self.closed = False

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        self.closed = True

    async def health(self):
        return {
            "status": "ok",
            "authentication_ready": True,
            "camera": {"name": "Front Door", "camera_id": "7"},
        }

    async def battery(self):
        return {
            "observed_at_utc": datetime.now(timezone.utc).isoformat(),
            "battery_level_raw": 2,
            "battery_state": "ok",
            "battery_voltage_raw": 291,
            "battery_voltage_volts": 2.91,
            "blink_battery_check_time": datetime.now(timezone.utc).isoformat(),
            "camera_status": "online",
        }

    async def snapshot(self) -> bytes:
        return jpeg_bytes()


@pytest.fixture
async def dashboard(tmp_path: Path):
    services = make_dashboard(tmp_path)
    fake = services.client
    await services.start()
    http = TestClient(TestServer(create_web_app(services)))
    await http.start_server()
    try:
        yield http, services
    finally:
        await http.close()
        await services.stop()
    assert fake.closed


def make_dashboard(
    tmp_path: Path, *, allow_lan: bool = False, access_token: str | None = None
) -> DashboardApplication:
    base = load_config(ROOT)
    paths = replace(
        base.paths,
        runtime_dir=tmp_path,
        database=tmp_path / "experiment.db",
        latest_jpeg=tmp_path / "latest.jpg",
        hls_dir=tmp_path / "hls",
        private_dir=tmp_path / "private",
        log_file=tmp_path / "application.log",
    )
    config = replace(base, paths=paths, server=replace(base.server, allow_lan=allow_lan))
    services = DashboardApplication(ROOT, config, access_token=access_token)
    fake = FakeAdapterClient()
    services.client = fake
    services.backend.client = fake
    return services


async def test_health_index_and_canonical_status(dashboard):
    http, _services = dashboard
    health = await http.get("/healthz")
    assert health.status == 200
    assert await health.json() == {"service": "blink-battery-dashboard", "status": "ok"}

    index = await http.get("/")
    assert index.status == 200
    assert "Battery test dashboard" in await index.text()

    response = await http.get("/api/status")
    assert response.status == 200
    payload = await response.json()
    assert payload["state"] == "not_started"
    assert payload["test"] == {
        "index": 0,
        "number": 1,
        "name": "Snapshot every 300 seconds",
        "kind": "snapshot",
    }
    assert payload["adapter"]["camera"]["name"] == "Front Door"
    assert payload["controls"]["start"] is True
    assert response.headers["Cache-Control"] == "no-store"


async def test_control_transition_and_invalid_continue_are_structured(dashboard):
    http, _services = dashboard
    started = await http.post("/api/experiment/start", json={})
    assert started.status == 202
    assert (await started.json())["run"]["state"] == "running_snapshot"

    invalid = await http.post("/api/experiment/continue", json={})
    assert invalid.status == 409
    assert (await invalid.json())["error"] == "invalid_transition"

    stopped = await http.post("/api/experiment/stop", json={})
    assert stopped.status == 202
    assert (await stopped.json())["run"]["state"] == "stopped_manual"


async def test_measurements_errors_and_missing_media_contracts(dashboard):
    http, _services = dashboard
    measurements = await http.get("/api/measurements?limit=25")
    assert measurements.status == 200
    assert set(await measurements.json()) == {"measurements", "phases", "truncated"}

    errors = await http.get("/api/errors?limit=10")
    assert errors.status == 200
    assert (await errors.json()) == {"errors": []}

    latest = await http.get("/latest.jpg")
    assert latest.status == 404
    assert latest.headers["Cache-Control"] == "no-store"


async def test_query_validation(dashboard):
    http, _services = dashboard
    bad_limit = await http.get("/api/errors?limit=abc")
    assert bad_limit.status == 400
    bad_start = await http.get("/api/measurements?start=not-a-time")
    assert bad_start.status == 400


async def test_lan_mode_requires_token_cookie_and_same_origin_json(tmp_path: Path):
    token = "test-token-that-is-deliberately-longer-than-thirty-two-characters"
    services = make_dashboard(tmp_path, allow_lan=True, access_token=token)
    await services.start()
    http = TestClient(TestServer(create_web_app(services)))
    await http.start_server()
    try:
        assert (await http.get("/healthz")).status == 200
        assert (await http.get("/api/status")).status == 401

        authorized = await http.get(f"/?token={token}", allow_redirects=False)
        assert authorized.status == 302
        cookie = authorized.cookies[ACCESS_COOKIE].value
        headers = {"Cookie": f"{ACCESS_COOKIE}={cookie}"}

        assert (await http.get("/api/status", headers=headers)).status == 200
        cross_origin = await http.post(
            "/api/experiment/start",
            json={},
            headers={**headers, "Origin": "http://attacker.invalid"},
        )
        assert cross_origin.status == 403

        origin = str(http.make_url("/")).rstrip("/")
        form_post = await http.post(
            "/api/experiment/start",
            data={},
            headers={**headers, "Origin": origin},
        )
        assert form_post.status == 415
        valid = await http.post(
            "/api/experiment/start",
            json={},
            headers={**headers, "Origin": origin},
        )
        assert valid.status == 202
    finally:
        await http.close()
        await services.stop()
