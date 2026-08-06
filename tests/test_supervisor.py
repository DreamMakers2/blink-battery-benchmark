from __future__ import annotations

from aiohttp import web
import pytest

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

    async def start(payload: dict[str, object], status: int = 200) -> str:
        app = web.Application()

        async def health(_request: web.Request) -> web.Response:
            return web.json_response(payload, status=status)

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


def test_dashboard_access_token_is_project_local_and_stable(tmp_path):
    path = tmp_path / "runtime" / "private" / "dashboard-access.token"
    first = load_or_create_dashboard_token(path)
    second = load_or_create_dashboard_token(path)
    assert first == second
    assert len(first) >= 32
    assert path.read_text(encoding="ascii").strip() == first
