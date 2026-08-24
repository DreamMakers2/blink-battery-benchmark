from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
import pytest

from blink_dashboard.clients import AdapterRequestError, BlinkAdapterClient


async def make_client(app: web.Application) -> tuple[TestClient, BlinkAdapterClient]:
    server = TestServer(app)
    http = TestClient(server)
    await http.start_server()
    adapter = BlinkAdapterClient(str(http.make_url("/")), session=http.session)
    return http, adapter


async def test_adapter_client_reads_contracts() -> None:
    app = web.Application()

    async def health(_request):
        return web.json_response({"status": "ok"})

    async def snapshot(_request):
        return web.Response(body=b"jpeg", content_type="image/jpeg")

    async def battery(_request):
        return web.json_response({"battery_state": "ok"})

    app.router.add_get("/", health)
    app.router.add_get("/snapshot", snapshot)
    app.router.add_get("/battery", battery)
    http, adapter = await make_client(app)
    try:
        assert (await adapter.health())["status"] == "ok"
        assert await adapter.snapshot() == b"jpeg"
        assert (await adapter.battery())["battery_state"] == "ok"
    finally:
        await http.close()


async def test_adapter_client_preserves_error_category() -> None:
    app = web.Application()

    async def battery(_request):
        return web.json_response({"error": "offline"}, status=503)

    app.router.add_get("/battery", battery)
    http, adapter = await make_client(app)
    try:
        with pytest.raises(AdapterRequestError) as error:
            await adapter.battery()
        assert error.value.category == "battery_endpoint_failure"
        assert error.value.status == 503
    finally:
        await http.close()
