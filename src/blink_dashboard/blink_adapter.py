"""Secure Blink authentication and loopback-only adapter services.

Imports of Blinkpy and the fetched ``blinkliveview`` source are intentionally
deferred until authentication or streaming is requested.  This keeps local
tests, database tooling, and the dashboard itself independent of Blink hardware.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import importlib
import inspect
import json
import logging
from pathlib import Path
import sys
from typing import Any

from .secret_store import SecretRedactionFilter, SecretStore


LOGGER = logging.getLogger(__name__)
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
BLINKLIVEVIEW_COMMIT = "d8f0a02180efce003de690055b87e8e2d5482e12"
BLINKLIVEVIEW_ARCHIVE_SHA256 = "27e5fe91a6f4e0ffe8c55c2b226bda744e1e628fa5810fdc10f87a8ac710a050"


class AdapterError(RuntimeError):
    """Base error for the local Blink adapter."""


class AuthenticationFailed(AdapterError):
    """Blink rejected both the saved session and entered credentials."""


class CameraSelectionError(AdapterError):
    """No unambiguous camera selection could be made."""


class AdapterUnavailable(AdapterError):
    """A fresh Blink operation or live-view startup failed."""


@dataclass(slots=True)
class AuthenticationResult:
    blink: Any
    camera: Any
    camera_identity: dict[str, Any]


def utc_now() -> str:
    """Return a stable second-resolution UTC timestamp for adapter contracts."""

    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def camera_identity(camera: Any, fallback_name: str | None = None) -> dict[str, Any]:
    """Extract the stable camera fields persisted with the authentication state."""

    return {
        "name": getattr(camera, "name", None) or fallback_name,
        "camera_id": str(getattr(camera, "camera_id", "") or ""),
        "camera_type": getattr(camera, "camera_type", None)
        or getattr(camera, "product_type", None),
        "serial": getattr(camera, "serial", None),
    }


def _camera_entries(blink: Any) -> list[tuple[str, Any, dict[str, Any]]]:
    entries: list[tuple[str, Any, dict[str, Any]]] = []
    for name, camera in getattr(blink, "cameras", {}).items():
        entries.append((str(name), camera, camera_identity(camera, str(name))))
    return entries


def _match_saved_camera(
    entries: Sequence[tuple[str, Any, dict[str, Any]]], saved: Mapping[str, Any]
) -> tuple[str, Any, dict[str, Any]] | None:
    camera_id = str(saved.get("camera_id", "") or "")
    serial = str(saved.get("serial", "") or "")
    matches = []
    for entry in entries:
        identity = entry[2]
        id_matches = bool(camera_id) and identity["camera_id"] == camera_id
        serial_matches = bool(serial) and str(identity.get("serial") or "") == serial
        if (
            (camera_id and serial and id_matches and serial_matches)
            or (camera_id and not serial and id_matches)
            or (serial and not camera_id and serial_matches)
        ):
            matches.append(entry)
    return matches[0] if len(matches) == 1 else None


def select_camera(
    blink: Any,
    saved: Mapping[str, Any] | None = None,
    *,
    input_fn: Callable[[str], str] = input,
) -> tuple[Any, dict[str, Any]]:
    """Resolve a saved identity or prompt for an explicit camera choice.

    A previously selected camera that has disappeared always causes another
    prompt, even when only one different device remains.  A brand-new account
    with exactly one camera can be selected safely without a prompt.
    """

    entries = _camera_entries(blink)
    if not entries:
        raise CameraSelectionError("No Blink cameras were found for this account.")

    if saved:
        match = _match_saved_camera(entries, saved)
        if match is not None:
            return match[1], match[2]
    elif len(entries) == 1:
        return entries[0][1], entries[0][2]

    print("\nAvailable Blink cameras:")
    for index, (name, _camera, identity) in enumerate(entries, 1):
        device_type = identity.get("camera_type") or "unknown type"
        print(f"  {index}. {name} ({device_type})")
    if saved:
        print("The previously selected camera is unavailable or ambiguous; select again.")

    while True:
        answer = input_fn("Select camera number: ").strip()
        try:
            index = int(answer)
        except ValueError:
            print("Enter a camera number from the list.")
            continue
        if 1 <= index <= len(entries):
            entry = entries[index - 1]
            return entry[1], entry[2]
        print(f"Enter a number between 1 and {len(entries)}.")


def _safe_auth_attributes(auth: Any) -> dict[str, Any]:
    attributes = dict(auth.login_attributes)
    # One-time codes are never durable authentication state.
    for name in ("2fa_code", "twofa_code", "mfa", "otp"):
        attributes.pop(name, None)
    return attributes


def persist_authentication(
    store: SecretStore,
    blink: Any,
    selected_camera: Mapping[str, Any] | None,
) -> None:
    """Persist credentials, current tokens, device identity, and camera selection."""

    store.save(
        {
            "auth": _safe_auth_attributes(blink.auth),
            "selected_camera": dict(selected_camera) if selected_camera else None,
            "trusted_device": {"remember_me_requested": True},
        }
    )


def _load_blinkpy() -> tuple[type[Any], type[Any], type[BaseException], Any]:
    try:
        blink_module = importlib.import_module("blinkpy.blinkpy")
        auth_module = importlib.import_module("blinkpy.auth")
        api_module = importlib.import_module("blinkpy.api")
    except ImportError as exc:  # pragma: no cover - launcher installs the pinned lock
        raise AuthenticationFailed(
            "Blinkpy 0.25.9 is not installed in the managed environment."
        ) from exc
    if str(getattr(blink_module, "__version__", "")) != "0.25.9":
        raise AuthenticationFailed(
            "This adapter requires exactly Blinkpy 0.25.9 for its authentication contract."
        )
    return (
        blink_module.Blink,
        auth_module.Auth,
        auth_module.BlinkTwoFARequiredError,
        api_module,
    )


async def _verify_2fa_remembered(api: Any, auth: Any, csrf_token: str, code: str) -> bool:
    """Blinkpy 0.25.9 compatibility override enabling its trusted-device field."""

    headers = {
        "User-Agent": api.OAUTH_USER_AGENT,
        "Accept": "*/*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://api.oauth.blink.com",
        "Referer": api.OAUTH_SIGNIN_URL,
    }
    response = await auth.session.post(
        api.OAUTH_2FA_VERIFY_URL,
        headers=headers,
        data={"2fa_code": code, "csrf-token": csrf_token, "remember_me": "true"},
    )
    if response.status != 201:
        return False
    try:
        result = await response.json()
    except Exception:
        return False
    return result.get("status") == "auth-completed"


async def _complete_2fa(blink: Any, api: Any, code: str) -> bool:
    # ``Auth.complete_2fa_login`` references this module-level Blinkpy function.
    # The narrow temporary override avoids modifying the fetched dependency and
    # is restored before any other adapter work starts.
    original = api.oauth_verify_2fa

    async def trusted(auth: Any, csrf_token: str, twofa_code: str) -> bool:
        return await _verify_2fa_remembered(api, auth, csrf_token, twofa_code)

    api.oauth_verify_2fa = trusted
    try:
        return bool(await blink.send_2fa_code(code))
    finally:
        api.oauth_verify_2fa = original


async def authenticate_blink(
    store: SecretStore,
    session: Any,
    *,
    input_fn: Callable[[str], str] = input,
    password_fn: Callable[[str], str] | None = None,
    blink_types: tuple[type[Any], type[Any], type[BaseException], Any] | None = None,
    retry_sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    retry_initial_seconds: float = 2.0,
    retry_max_seconds: float = 60.0,
    retry_backoff_factor: float = 2.0,
) -> AuthenticationResult:
    """Authenticate, handle MFA, select a camera, and persist every token update.

    Once an encrypted saved session exists, startup failures are deliberately
    treated as ambiguous.  Blinkpy reports both rejected sessions and temporary
    service/network failures through ``False`` results and broad exceptions, so
    discarding the session or asking for the password would be unsafe.  Instead,
    each retry reloads the latest encrypted payload and creates a fresh Blink
    candidate.  Only Blinkpy's explicit two-factor exception may open an MFA
    prompt.
    """

    if password_fn is None:
        from getpass import getpass

        password_fn = getpass

    Blink, Auth, BlinkTwoFARequiredError, api = blink_types or _load_blinkpy()
    if retry_initial_seconds < 0 or retry_max_seconds < 0:
        raise ValueError("Blink authentication retry delays cannot be negative")
    if retry_backoff_factor < 1:
        raise ValueError("Blink authentication retry backoff must be at least 1")

    saved_payload = store.load() or {}
    saved_auth = saved_payload.get("auth")
    saved_camera = saved_payload.get("selected_camera")
    camera_state: dict[str, Any] = {
        "value": dict(saved_camera) if isinstance(saved_camera, Mapping) else None
    }

    def make_blink(auth_data: Mapping[str, Any]) -> Any:
        blink = Blink(session=session)

        def token_callback() -> None:
            persist_authentication(store, blink, camera_state["value"])

        blink.auth = Auth(dict(auth_data), no_prompt=True, session=session, callback=token_callback)
        return blink

    async def start(candidate: Any, *, retry_saved_auth: bool) -> bool:
        try:
            started = await candidate.start()
        except BlinkTwoFARequiredError as exc:
            # Do not infer MFA from exception text or related exception classes.
            # Blinkpy's exact sentinel is the only authority for prompting.
            if type(exc) is not BlinkTwoFARequiredError:
                if retry_saved_auth:
                    LOGGER.warning(
                        "Saved Blink authentication startup failed (%s); retrying.",
                        type(exc).__name__,
                    )
                    return False
                raise AuthenticationFailed("Blink authentication could not be completed.") from exc
            print("Blink requires multi-factor authentication.")
            code = input_fn("Enter the Blink MFA code: ").strip()
            try:
                completed = bool(code) and await _complete_2fa(candidate, api, code)
            except asyncio.CancelledError:
                raise
            except Exception as mfa_exc:
                if retry_saved_auth:
                    LOGGER.warning(
                        "Saved Blink MFA completion failed (%s); retrying authentication.",
                        type(mfa_exc).__name__,
                    )
                    return False
                raise AuthenticationFailed("Blink MFA verification failed.") from mfa_exc
            if not completed:
                if retry_saved_auth:
                    LOGGER.warning(
                        "Saved Blink MFA completion was not confirmed; retrying authentication."
                    )
                    return False
                raise AuthenticationFailed("Blink MFA verification failed.")
            started = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if retry_saved_auth:
                LOGGER.warning(
                    "Saved Blink authentication startup failed (%s); retrying.",
                    type(exc).__name__,
                )
                return False
            raise AuthenticationFailed("Blink authentication could not be completed.") from exc
        return started is True

    blink = None
    if isinstance(saved_auth, Mapping):
        # Retain an in-memory last-known copy in case an external writer briefly
        # makes the store unavailable, but reload the encrypted store before
        # every attempt so token callbacks/other processes can rotate the state.
        last_saved_auth = dict(saved_auth)
        retry_delay = min(retry_initial_seconds, retry_max_seconds)
        while blink is None:
            latest_payload = store.load() or {}
            latest_auth = latest_payload.get("auth")
            latest_camera = latest_payload.get("selected_camera")
            if isinstance(latest_auth, Mapping):
                last_saved_auth = dict(latest_auth)
            if isinstance(latest_camera, Mapping):
                saved_camera = latest_camera
                camera_state["value"] = dict(latest_camera)

            candidate = make_blink(last_saved_auth)
            if await start(candidate, retry_saved_auth=True):
                blink = candidate
                break

            LOGGER.warning(
                "Blink saved-session startup remains unavailable; retrying in %.1f seconds.",
                retry_delay,
            )
            await retry_sleep(retry_delay)
            retry_delay = min(retry_max_seconds, retry_delay * retry_backoff_factor)

    if blink is None:
        username = input_fn("Blink username (email): ").strip()
        password = password_fn("Blink password: ")
        if not username or not password:
            raise AuthenticationFailed("Blink username and password are required.")
        # Protect the first-login path before Blinkpy emits any diagnostics.
        redactor = SecretRedactionFilter((password,))
        for handler in logging.getLogger().handlers:
            handler.addFilter(redactor)
        candidate = make_blink({"username": username, "password": password})
        if not await start(candidate, retry_saved_auth=False):
            raise AuthenticationFailed("Blink authentication failed.")
        blink = candidate

    # Save immediately after authentication, before interactive camera selection.
    persist_authentication(store, blink, camera_state["value"])
    camera, identity = select_camera(
        blink,
        saved_camera if isinstance(saved_camera, Mapping) else None,
        input_fn=input_fn,
    )
    camera_state["value"] = identity
    persist_authentication(store, blink, identity)
    return AuthenticationResult(blink=blink, camera=camera, camera_identity=identity)


def load_managed_stream_class(dependencies_root: str | Path) -> type[Any]:
    """Load ``OnDemandLiveStream`` only from the verified managed source tree."""

    root = Path(dependencies_root).resolve()
    candidates = sorted(root.glob("**/blinkliveview/cli.py"))
    if len(candidates) != 1:
        raise AdapterUnavailable(
            "Managed blinkliveview source is missing or ambiguous; rerun the launcher bootstrap."
        )
    package_root = candidates[0].parent.parent.resolve()
    manifest_path = package_root / ".managed-dependency.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AdapterUnavailable(
            "Managed blinkliveview verification manifest is missing or invalid."
        ) from exc
    if (
        manifest.get("commit") != BLINKLIVEVIEW_COMMIT
        or manifest.get("archive_sha256") != BLINKLIVEVIEW_ARCHIVE_SHA256
    ):
        raise AdapterUnavailable("Managed blinkliveview source does not match the pinned build.")
    module = sys.modules.get("blinkliveview.cli")
    if module is not None:
        module_path = Path(inspect.getfile(module)).resolve()
        if package_root not in module_path.parents:
            raise AdapterUnavailable("A non-managed blinkliveview module is already loaded.")
    else:
        sys.path.insert(0, str(package_root))
        try:
            module = importlib.import_module("blinkliveview.cli")
        finally:
            try:
                sys.path.remove(str(package_root))
            except ValueError:  # pragma: no cover - defensive against import hooks
                pass
    module_path = Path(inspect.getfile(module)).resolve()
    if package_root not in module_path.parents:
        raise AdapterUnavailable("blinkliveview was not loaded from the managed source tree.")
    return module.OnDemandLiveStream


def _managed_stream_type(upstream: type[Any]) -> type[Any]:
    """Create a minimal subclass that exposes initial connection success/failure."""

    class ManagedOnDemandLiveStream(upstream):  # type: ignore[misc, valid-type]
        def __init__(self, server: Any, response: Mapping[str, Any]) -> None:
            super().__init__(server, response)
            self.adapter_started = asyncio.Event()
            self.adapter_start_error: BaseException | None = None

        async def feed(self) -> None:
            # The pinned upstream implementation prints its private Blink target
            # hostname and port unconditionally.  Suppress only that module's
            # print global while its feed is alive; adapter-owned logs remain
            # available and never expose live-view connection details.
            upstream_module = importlib.import_module(upstream.__module__)
            previous_print = vars(upstream_module).get("print")
            had_print_override = "print" in vars(upstream_module)
            setattr(upstream_module, "print", lambda *_args, **_kwargs: None)
            task = asyncio.create_task(super().feed())
            try:
                while self.target_writer is None and not task.done():
                    await asyncio.sleep(0)
                if task.done():
                    await task
                    if self.target_writer is None:
                        raise AdapterUnavailable("Blink live-view feed ended before connecting.")
                self.adapter_started.set()
                await task
            except BaseException as exc:
                if not isinstance(exc, asyncio.CancelledError):
                    self.adapter_start_error = exc
                self.adapter_started.set()
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                raise
            finally:
                self.adapter_started.set()
                if had_print_override:
                    setattr(upstream_module, "print", previous_print)
                else:
                    delattr(upstream_module, "print")

    return ManagedOnDemandLiveStream


class BlinkAdapter:
    """Serialize Blink operations and expose local HTTP and on-demand TCP APIs."""

    def __init__(
        self,
        blink: Any,
        camera: Any,
        camera_info: Mapping[str, Any],
        *,
        dependencies_root: str | Path,
        snapshot_delay_seconds: float = 1.0,
        stream_start_timeout_seconds: float = 10.0,
        stream_class_loader: Callable[[str | Path], type[Any]] = load_managed_stream_class,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self.blink = blink
        self.camera = camera
        self.camera_info = dict(camera_info)
        self.dependencies_root = Path(dependencies_root)
        self.snapshot_delay_seconds = snapshot_delay_seconds
        self.stream_start_timeout_seconds = stream_start_timeout_seconds
        self.stream_class_loader = stream_class_loader
        self.sleep = sleep
        self.operation_lock = asyncio.Lock()
        self._stream_lock = asyncio.Lock()
        self.clients: list[asyncio.StreamWriter] = []
        self.livestream: Any | None = None
        self.feed_task: asyncio.Task[Any] | None = None
        self.server: asyncio.AbstractServer | None = None
        self.http_runner: Any | None = None
        self.verbose = False  # Contract expected by upstream OnDemandLiveStream.
        self._stopping = False
        self.last_successful_blink_contact: str | None = None

    @property
    def authentication_ready(self) -> bool:
        return bool(getattr(self.blink, "available", True) and self.camera is not None)

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok" if self.authentication_ready else "degraded",
            "authentication_ready": self.authentication_ready,
            "camera": dict(self.camera_info),
            "stream_clients": len(self.clients),
            "last_successful_blink_contact_utc": self.last_successful_blink_contact,
        }

    async def _refresh(self) -> None:
        result = await self.blink.refresh(force=True)
        if result is not True:
            raise AdapterUnavailable("Blink did not complete a fresh refresh.")
        self.last_successful_blink_contact = utc_now()

    async def snapshot(self) -> bytes:
        """Request and return a fresh camera JPEG under the operation lock."""

        async with self.operation_lock:
            try:
                result = await self.camera.snap_picture()
                if result is False:
                    raise AdapterUnavailable("Blink rejected the snapshot request.")
                await self.sleep(self.snapshot_delay_seconds)
                await self._refresh()
                image = getattr(self.camera, "image_from_cache", None)
                if image:
                    return bytes(image)
                thumbnail = getattr(self.camera, "thumbnail", None)
                if thumbnail:
                    async with self.blink.auth.session.get(thumbnail) as response:
                        if response.status == 200:
                            image = await response.read()
                            if image:
                                self.last_successful_blink_contact = utc_now()
                                return bytes(image)
                raise AdapterUnavailable("Blink returned no snapshot image.")
            except AdapterUnavailable:
                raise
            except Exception as exc:
                LOGGER.warning("Snapshot operation failed (%s).", type(exc).__name__)
                raise AdapterUnavailable("Fresh Blink snapshot failed.") from exc

    async def battery(self) -> dict[str, Any]:
        """Force a fresh refresh and preserve Blink's raw battery fields."""

        async with self.operation_lock:
            try:
                await self._refresh()
                raw_voltage = getattr(self.camera, "battery_voltage", None)
                voltage = (
                    raw_voltage / 100
                    if isinstance(raw_voltage, (int, float)) and not isinstance(raw_voltage, bool)
                    else None
                )
                battery_check_time = getattr(self.camera, "battery_check_time", None)
                if isinstance(battery_check_time, datetime):
                    battery_check_time = battery_check_time.isoformat()
                return {
                    "camera": self.camera_info.get("name") or getattr(self.camera, "name", None),
                    "observed_at_utc": utc_now(),
                    "battery_level_raw": getattr(self.camera, "battery_level", None),
                    "battery_state": getattr(self.camera, "battery_state", None),
                    "battery_voltage_raw": raw_voltage,
                    "battery_voltage_volts": voltage,
                    "blink_battery_check_time": battery_check_time,
                    "camera_status": getattr(self.camera, "status", None),
                }
            except AdapterUnavailable:
                raise
            except Exception as exc:
                LOGGER.warning("Battery refresh failed (%s).", type(exc).__name__)
                raise AdapterUnavailable("Fresh Blink battery refresh failed.") from exc

    def create_app(self) -> Any:
        from aiohttp import web

        app = web.Application()
        app.router.add_get("/", self._handle_health)
        app.router.add_get("/snapshot", self._handle_snapshot)
        app.router.add_get("/battery", self._handle_battery)
        return app

    async def _handle_health(self, _request: Any) -> Any:
        from aiohttp import web

        return web.json_response(self.health())

    async def _handle_snapshot(self, _request: Any) -> Any:
        from aiohttp import web

        try:
            image = await self.snapshot()
        except AdapterUnavailable:
            return web.json_response(
                {"error": "snapshot_unavailable", "message": "Fresh Blink snapshot failed."},
                status=503,
            )
        return web.Response(
            body=image,
            content_type="image/jpeg",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    async def _handle_battery(self, _request: Any) -> Any:
        from aiohttp import web

        try:
            payload = await self.battery()
        except AdapterUnavailable:
            return web.json_response(
                {
                    "error": "battery_unavailable",
                    "message": "Fresh Blink battery refresh failed.",
                },
                status=503,
            )
        return web.json_response(payload)

    async def start_http(self, *, host: str = "127.0.0.1", port: int = 8080) -> None:
        from aiohttp import web

        self._require_loopback(host)
        self.http_runner = web.AppRunner(self.create_app())
        await self.http_runner.setup()
        site = web.TCPSite(self.http_runner, host, port)
        await site.start()

    async def start_tcp(self, *, host: str = "127.0.0.1", port: int = 5000) -> None:
        self._require_loopback(host)
        self.server = await asyncio.start_server(self._handle_client, host, port)

    @staticmethod
    def _require_loopback(host: str) -> None:
        if host.casefold() not in LOOPBACK_HOSTS:
            raise ValueError("Blink adapter services must bind to loopback only.")

    async def _request_liveview(self) -> Mapping[str, Any]:
        async with self.operation_lock:
            try:
                await self._refresh()
                api = importlib.import_module("blinkpy.api")
                response = await api.request_camera_liveview(
                    self.camera.sync.blink,
                    self.camera.sync.network_id,
                    self.camera.camera_id,
                    camera_type=self.camera.camera_type,
                )
            except Exception as exc:
                LOGGER.warning("Live-view request failed (%s).", type(exc).__name__)
                raise AdapterUnavailable("Blink live-view request failed.") from exc
        if not isinstance(response, Mapping) or not response.get("server"):
            raise AdapterUnavailable("Blink live-view response contained no server.")
        if not str(response["server"]).startswith("immis://"):
            raise AdapterUnavailable("Blink returned an unsupported live-view protocol.")
        return response

    async def _ensure_stream_locked(self) -> None:
        if self.feed_task is not None and not self.feed_task.done():
            return
        response = await self._request_liveview()
        upstream = self.stream_class_loader(self.dependencies_root)
        managed = _managed_stream_type(upstream)
        stream = managed(self, response)
        task = asyncio.create_task(stream.feed(), name="blink-live-view")
        self.livestream = stream
        self.feed_task = task
        try:
            await asyncio.wait_for(
                stream.adapter_started.wait(), timeout=self.stream_start_timeout_seconds
            )
            if stream.adapter_start_error is not None or task.done():
                await task
        except BaseException as exc:
            stream.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.livestream = None
            self.feed_task = None
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise AdapterUnavailable("Blink live view could not start.") from exc
        task.add_done_callback(self._feed_done)

    def _feed_done(self, task: asyncio.Task[Any]) -> None:
        if task.cancelled() or self._stopping:
            return
        # Retrieve any exception now so asyncio never reports an unobserved task.
        try:
            task.exception()
        except (asyncio.CancelledError, Exception):
            pass
        asyncio.create_task(self._close_clients_after_feed(), name="blink-feed-cleanup")

    async def _close_clients_after_feed(self) -> None:
        async with self._stream_lock:
            for writer in list(self.clients):
                writer.close()
            await self._stop_stream_locked()

    async def _stop_stream_locked(self) -> None:
        stream, task = self.livestream, self.feed_task
        self.livestream = None
        self.feed_task = None
        if stream is not None:
            stream.stop()
        if task is not None and not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        accepted = False
        try:
            async with self._stream_lock:
                if self._stopping:
                    return
                self.clients.append(writer)
                accepted = True
                if len(self.clients) == 1:
                    await self._ensure_stream_locked()

            # A stream consumer normally sends nothing.  Reading is intentional:
            # EOF/reset becomes visible immediately instead of a polling sleep loop.
            while not self._stopping:
                data = await reader.read(1024)
                if not data:
                    break
        except (AdapterUnavailable, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            async with self._stream_lock:
                if accepted and writer in self.clients:
                    self.clients.remove(writer)
                writer.close()
                try:
                    await writer.wait_closed()
                except (ConnectionError, OSError):
                    pass
                if not self.clients:
                    await self._stop_stream_locked()

    async def stop(self) -> None:
        self._stopping = True
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for writer in list(self.clients):
            writer.close()
        async with self._stream_lock:
            await self._stop_stream_locked()
            self.clients.clear()
        if self.http_runner is not None:
            await self.http_runner.cleanup()
            self.http_runner = None


__all__ = [
    "AdapterError",
    "AdapterUnavailable",
    "AuthenticationFailed",
    "AuthenticationResult",
    "BlinkAdapter",
    "CameraSelectionError",
    "authenticate_blink",
    "camera_identity",
    "load_managed_stream_class",
    "persist_authentication",
    "select_camera",
    "utc_now",
]
