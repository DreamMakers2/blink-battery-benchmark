"""Local dashboard HTTP application and real experiment backend."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import hmac
import logging
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from .clients import AdapterRequestError, BlinkAdapterClient
from .config import AppConfig
from .experiment import ExperimentRunner, ExperimentState, InvalidTransition
from .media import HlsStreamConsumer, atomic_write_jpeg
from .storage import BatteryReading, SQLiteStorage


LOGGER = logging.getLogger(__name__)
ACCESS_COOKIE = "blink_dashboard_access"


def _utc_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


class LocalExperimentBackend:
    """Bridge the experiment state machine to the loopback adapter and media files."""

    def __init__(self, config: AppConfig, client: BlinkAdapterClient):
        self.config = config
        self.client = client
        self.latest_snapshot_at_utc: str | None = None
        self.last_battery: BatteryReading | None = None
        self.stream = HlsStreamConsumer(
            host=config.blink.stream_host,
            port=config.blink.stream_port,
            hls_dir=config.paths.hls_dir,
            ffmpeg=config.media.ffmpeg_executable,
            connect_timeout=config.blink.stream_connect_timeout_seconds,
            read_timeout=config.experiment.stream_data_timeout_seconds,
            reconnect_delay=config.blink.stream_reconnect_seconds,
            segment_seconds=config.media.hls_segment_seconds,
            list_size=config.media.hls_list_size,
        )

    async def capture_snapshot(self) -> None:
        payload = await self.client.snapshot()
        self.latest_snapshot_at_utc = await atomic_write_jpeg(
            self.config.paths.latest_jpeg, payload
        )

    async def read_battery(self) -> BatteryReading:
        payload = await self.client.battery()
        observed = _utc_datetime(payload.get("observed_at_utc")) or datetime.now(timezone.utc)
        reading = BatteryReading(
            observed_at_utc=observed,
            battery_level_raw=payload.get("battery_level_raw"),
            battery_state=payload.get("battery_state"),
            battery_voltage_raw=payload.get("battery_voltage_raw"),
            battery_voltage_volts=payload.get("battery_voltage_volts"),
            blink_battery_check_time=_utc_datetime(payload.get("blink_battery_check_time")),
            camera_status=payload.get("camera_status"),
        )
        self.last_battery = reading
        return reading

    async def run_stream(
        self,
        on_bytes: Callable[[int], None],
        stop_event: asyncio.Event,
        on_reconnect: Callable[[], None],
        on_error: Callable[[str, str], None],
    ) -> None:
        async def count_bytes(count: int, _received_at: str) -> None:
            on_bytes(count)

        await self.stream.run(
            stop_event,
            on_bytes=count_bytes,
            on_reconnect=on_reconnect,
            on_error=on_error,
        )

    async def stop(self) -> None:
        await self.stream.stop()


class DashboardApplication:
    """Own resources shared by HTTP handlers and background experiment tasks."""

    def __init__(
        self,
        project_root: Path,
        config: AppConfig,
        *,
        access_token: str | None = None,
    ):
        self.project_root = project_root.resolve()
        self.config = config
        self.access_token = access_token
        if config.server.allow_lan and not access_token:
            raise ValueError("LAN mode requires a dashboard access token")
        self.storage = SQLiteStorage(
            config.storage.database_path,
            busy_timeout_seconds=config.storage.busy_timeout_seconds,
        )
        self.client = BlinkAdapterClient(
            config.blink.http_base_url,
            snapshot_timeout=config.blink.request_timeout_seconds,
            battery_timeout=config.blink.battery_timeout_seconds,
        )
        self.backend = LocalExperimentBackend(config, self.client)
        self.experiment = ExperimentRunner(
            self.storage,
            config.experiment,
            self.backend,
            low_battery_states=config.blink.low_battery_states,
        )
        self.last_adapter_health: dict[str, Any] = {}

    async def start(self) -> None:
        await self.client.start()
        self.last_adapter_health = await self.client.health()
        await self.experiment.initialize()

    async def stop(self) -> None:
        await self.experiment.shutdown()
        await self.backend.stop()
        await self.client.close()
        self.storage.close()

    async def status(self) -> dict[str, Any]:
        try:
            self.last_adapter_health = await self.client.health()
        except AdapterRequestError as exc:
            LOGGER.warning("Adapter health unavailable: %s", exc)
        status = self.experiment.get_status()
        run = status.run
        state = ExperimentState(run.state)
        test_index = run.current_test_number - 1
        test_duration = self.config.experiment.test_duration_seconds
        recovery_duration = self.config.experiment.recovery_duration_seconds
        completed_before = test_index * (test_duration + recovery_duration)
        if state in {
            ExperimentState.RUNNING_SNAPSHOT,
            ExperimentState.RUNNING_STREAM,
            ExperimentState.STOPPED_MANUAL,
        }:
            completed_before += status.active_elapsed_seconds
        elif state in {ExperimentState.RECOVERY, ExperimentState.STOPPED_LOW_BATTERY}:
            completed_before += test_duration
            completed_before += recovery_duration - (status.recovery_remaining_seconds or 0)
        elif state == ExperimentState.COMPLETED:
            completed_before = (
                len(self.config.experiment.tests) * test_duration
                + (len(self.config.experiment.tests) - 1) * recovery_duration
            )
        total_duration = (
            len(self.config.experiment.tests) * test_duration
            + (len(self.config.experiment.tests) - 1) * recovery_duration
        )
        progress = (
            100
            if state == ExperimentState.COMPLETED
            else max(0, min(100, completed_before / total_duration * 100))
        )
        remaining = (
            status.recovery_remaining_seconds
            if state in {ExperimentState.RECOVERY, ExperimentState.STOPPED_LOW_BATTERY}
            else status.active_remaining_seconds
        )
        media_at = self.backend.stream.last_data_at or self.backend.latest_snapshot_at_utc
        hls_freshness_window = max(
            30.0,
            self.config.media.hls_segment_seconds * self.config.media.hls_list_size * 2,
        )
        adapter_ready = self.last_adapter_health.get("status") in {"ok", "degraded"}
        return {
            "state": run.state,
            "test": {
                "index": test_index,
                "number": run.current_test_number,
                "name": status.test_name,
                "kind": status.test_kind,
            },
            "test_count": len(self.config.experiment.tests),
            "phase": {
                "started_at_utc": run.phase_started_at_utc,
                "elapsed_seconds": (
                    recovery_duration - remaining
                    if state in {ExperimentState.RECOVERY, ExperimentState.STOPPED_LOW_BATTERY}
                    and remaining is not None
                    else status.active_elapsed_seconds
                ),
                "remaining_seconds": remaining,
            },
            "overall_progress_percent": progress,
            "battery": self.backend.last_battery,
            "counters": {
                "snapshot_attempt_count": run.snapshots_attempted,
                "successful_snapshot_count": run.snapshots_succeeded,
                "failed_snapshot_count": run.snapshots_failed,
                "snapshot_timeout_count": run.snapshot_timeouts,
                "received_stream_bytes": run.stream_bytes,
                "stream_reconnect_count": run.stream_reconnects,
            },
            "stream": {
                "receiving": status.stream_receiving,
                "outage_seconds": status.stream_outage_seconds,
                "fatal_outage_seconds": status.stream_fatal_outage_seconds,
                "data_timeout_seconds": self.config.experiment.stream_data_timeout_seconds,
            },
            "media": {
                "mode": "stream" if state == ExperimentState.RUNNING_STREAM else "snapshot",
                "ready": self.backend.stream.is_fresh(hls_freshness_window),
                "latest_at_utc": media_at,
                "snapshot_available": self.config.paths.latest_jpeg.is_file(),
            },
            "controls": {
                "start": state in {ExperimentState.NOT_STARTED, ExperimentState.STOPPED_MANUAL},
                "stop": state
                in {
                    ExperimentState.RUNNING_SNAPSHOT,
                    ExperimentState.RUNNING_STREAM,
                    ExperimentState.RECOVERY,
                    ExperimentState.STOPPED_LOW_BATTERY,
                },
                "restart": state != ExperimentState.NOT_STARTED,
                "continue": state == ExperimentState.STOPPED_LOW_BATTERY,
            },
            "stop_reason": run.stop_reason,
            "latest_error": run.latest_error,
            "adapter": self.last_adapter_health,
            "auth_ready": bool(self.last_adapter_health.get("authentication_ready", adapter_ready)),
        }


SERVICES_KEY = web.AppKey("services", DashboardApplication)


def create_web_app(services: DashboardApplication) -> web.Application:
    app = web.Application(middlewares=[_security_headers, _access_control])
    app[SERVICES_KEY] = services
    root = services.project_root
    services.config.paths.hls_dir.mkdir(parents=True, exist_ok=True)

    app.router.add_get("/", _index)
    app.router.add_get("/healthz", _health)
    app.router.add_get("/api/status", _status)
    app.router.add_get("/api/measurements", _measurements)
    app.router.add_get("/api/errors", _errors)
    app.router.add_post("/api/experiment/start", _control("start"))
    app.router.add_post("/api/experiment/stop", _control("stop"))
    app.router.add_post("/api/experiment/restart", _control("restart"))
    app.router.add_post("/api/experiment/continue", _control("continue_after_low_battery"))
    app.router.add_get("/latest.jpg", _latest_jpeg)
    app.router.add_static("/stream", services.config.paths.hls_dir, show_index=False)
    app.router.add_static("/static", root / "static", show_index=False)
    return app


@web.middleware
async def _security_headers(
    request: web.Request, handler: Callable[..., Any]
) -> web.StreamResponse:
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        _apply_security_headers(request, exc)
        raise
    _apply_security_headers(request, response)
    return response


def _apply_security_headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    if request.path.startswith("/api/") or request.path in {"/healthz", "/latest.jpg"}:
        response.headers["Cache-Control"] = "no-store"


@web.middleware
async def _access_control(request: web.Request, handler: Callable[..., Any]) -> web.StreamResponse:
    services = _services(request)
    if not services.config.server.allow_lan or request.path == "/healthz":
        return await handler(request)

    expected = services.access_token or ""
    query_token = request.query.get("token")
    cookie_token = request.cookies.get(ACCESS_COOKIE)
    query_matches = query_token is not None and hmac.compare_digest(query_token, expected)
    cookie_matches = cookie_token is not None and hmac.compare_digest(cookie_token, expected)
    if not (query_matches or cookie_matches):
        raise web.HTTPUnauthorized(text="A valid LAN dashboard access token is required")

    if request.method == "POST":
        expected_origin = f"{request.scheme}://{request.host}"
        if request.headers.get("Origin") != expected_origin:
            raise web.HTTPForbidden(text="Cross-origin experiment controls are not allowed")

    if query_matches and request.path == "/":
        response: web.StreamResponse = web.Response(status=302, headers={"Location": "/"})
    else:
        response = await handler(request)
    if query_matches:
        response.set_cookie(
            ACCESS_COOKIE,
            expected,
            httponly=True,
            samesite="Strict",
            path="/",
            max_age=60 * 60 * 24 * 30,
        )
    return response


def _services(request: web.Request) -> DashboardApplication:
    return request.app[SERVICES_KEY]


async def _index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(_services(request).project_root / "templates" / "index.html")


async def _health(_request: web.Request) -> web.Response:
    return web.json_response({"service": "blink-battery-dashboard", "status": "ok"})


async def _status(request: web.Request) -> web.Response:
    return web.json_response(_jsonable(await _services(request).status()))


def _bounded_int(request: web.Request, name: str, default: int, maximum: int) -> int:
    try:
        value = int(request.query.get(name, default))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=f"{name} must be an integer") from exc
    return max(1, min(maximum, value))


async def _measurements(request: web.Request) -> web.Response:
    services = _services(request)
    run = services.experiment.current_run
    limit = _bounded_int(request, "limit", 5000, 100_000)
    start_raw = request.query.get("start")
    try:
        start = _utc_datetime(start_raw) if start_raw else None
    except ValueError as exc:
        raise web.HTTPBadRequest(text="start must be an ISO-8601 timestamp") from exc
    points = services.storage.list_measurements(run.id, start=start, limit=limit)
    phases = services.storage.list_phases(run.id)
    return web.json_response(
        _jsonable({"measurements": points, "phases": phases, "truncated": len(points) == limit})
    )


async def _errors(request: web.Request) -> web.Response:
    services = _services(request)
    limit = _bounded_int(request, "limit", 50, 200)
    events = services.storage.list_events(services.experiment.current_run.id, limit=limit)
    events = [event for event in events if event.get("level") in {"warning", "error"}]
    for event in events:
        test_number = event.get("test_number")
        if isinstance(test_number, int) and 1 <= test_number <= len(
            services.config.experiment.tests
        ):
            event["test_name"] = services.config.experiment.test(test_number).name
    return web.json_response(_jsonable({"errors": events}))


def _control(method_name: str) -> Callable[[web.Request], Any]:
    async def handler(request: web.Request) -> web.Response:
        if request.content_type != "application/json":
            return web.json_response(
                {
                    "error": "unsupported_media_type",
                    "detail": "Experiment controls require application/json",
                },
                status=415,
            )
        method = getattr(_services(request).experiment, method_name)
        try:
            run = await method()
        except InvalidTransition as exc:
            return web.json_response(
                {"error": "invalid_transition", "detail": str(exc)}, status=409
            )
        return web.json_response(_jsonable({"accepted": True, "run": run}), status=202)

    return handler


async def _latest_jpeg(request: web.Request) -> web.StreamResponse:
    path = _services(request).config.paths.latest_jpeg
    if not path.is_file():
        raise web.HTTPNotFound(text="No successful snapshot is available yet")
    return web.FileResponse(path, headers={"Cache-Control": "no-store"})


__all__ = ["DashboardApplication", "LocalExperimentBackend", "create_web_app"]
