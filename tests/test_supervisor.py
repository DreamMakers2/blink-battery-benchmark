from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiohttp import web
import pytest

import blink_dashboard.supervisor as supervisor
from blink_dashboard.supervisor import (
    StartupError,
    dashboard_is_running,
    load_or_create_dashboard_token,
    wait_for_adapter,
)


class FakeProcess:
    returncode: int | None = None


@pytest.fixture
async def local_server():
    runners: list[web.AppRunner] = []

    async def start(payload, status: int = 200) -> str:
        app = web.Application()

        async def health(_request: web.Request) -> web.Response:
            current_payload = payload() if callable(payload) else payload
            return web.json_response(current_payload, status=status)

        app.router.add_get("/healthz", health)
        app.router.add_get("/", health)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        runners.append(runner)
        port = site._server.sockets[0].getsockname()[1]
        return f"http://127.0.0.1:{port}"

    yield start
    for runner in runners:
        await runner.cleanup()


async def test_detects_compatible_dashboard(local_server):
    url = await local_server({"service": "blink-battery-dashboard"})
    assert await dashboard_is_running(url)


async def test_rejects_unrelated_health_endpoint(local_server):
    url = await local_server({"service": "something-else"})
    assert not await dashboard_is_running(url)


async def test_wait_for_adapter_returns_health(local_server):
    url = await local_server({"status": "ok", "authentication_ready": True})
    result = await wait_for_adapter(url, FakeProcess(), timeout_seconds=1)
    assert result["authentication_ready"] is True


async def test_wait_for_adapter_reports_early_exit():
    process = FakeProcess()
    process.returncode = 7
    with pytest.raises(StartupError, match="code 7"):
        await wait_for_adapter("http://127.0.0.1:9", process, timeout_seconds=1)


async def test_wait_for_adapter_has_no_production_wall_clock_cutoff(local_server, monkeypatch):
    calls = 0

    def delayed_readiness():
        nonlocal calls
        calls += 1
        if calls < 4:
            return {"status": "starting", "authentication_ready": False}
        return {"status": "ok", "authentication_ready": True}

    url = await local_server(delayed_readiness)
    monkeypatch.setattr(
        supervisor,
        "time",
        SimpleNamespace(
            monotonic=lambda: pytest.fail(
                "default adapter wait must not consult a startup deadline"
            )
        ),
    )

    result = await wait_for_adapter(url, FakeProcess(), poll_interval_seconds=0)

    assert result["authentication_ready"] is True
    assert calls == 4


async def test_wait_for_adapter_remains_cancelable(local_server):
    url = await local_server({"status": "starting", "authentication_ready": False})
    task = asyncio.create_task(wait_for_adapter(url, FakeProcess(), poll_interval_seconds=0.01))
    await asyncio.sleep(0.03)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


def test_dashboard_access_token_is_project_local_and_stable(tmp_path):
    path = tmp_path / "runtime" / "private" / "dashboard-access.token"
    first = load_or_create_dashboard_token(path)
    second = load_or_create_dashboard_token(path)
    assert first == second
    assert len(first) >= 32
    assert path.read_text(encoding="ascii").strip() == first
