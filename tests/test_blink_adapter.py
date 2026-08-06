from __future__ import annotations

import asyncio
from collections import OrderedDict
import json
import sys
from types import SimpleNamespace

import pytest

from blink_dashboard.blink_adapter import (
    AdapterUnavailable,
    BLINKLIVEVIEW_ARCHIVE_SHA256,
    BLINKLIVEVIEW_COMMIT,
    BlinkAdapter,
    _managed_stream_type,
    _verify_2fa_remembered,
    authenticate_blink,
    load_managed_stream_class,
    select_camera,
)


class MemoryStore:
    def __init__(self, payload=None):
        self.payload = payload
        self.saved = []

    def load(self):
        return self.payload

    def save(self, payload):
        self.payload = payload
        self.saved.append(payload)


class FakeCamera:
    def __init__(self, name="Front Door", camera_id="7", serial="SERIAL-7"):
        self.name = name
        self.camera_id = camera_id
        self.serial = serial
        self.camera_type = "doorbell"
        self.product_type = "lotus"
        self.battery_level = 3
        self.battery_state = "ok"
        self._battery_voltage = 165
        self.battery_check_time = "2026-08-06T17:55:00Z"
        self.status = "online"
        self.image_from_cache = b"\xff\xd8jpeg\xff\xd9"
        self.thumbnail = None
        self.snap_calls = 0
        self.sync = SimpleNamespace(blink=None, network_id="12")

    @property
    def battery_voltage(self):
        return self._battery_voltage

    async def snap_picture(self):
        self.snap_calls += 1
        return True


class FakeBlink:
    def __init__(self, camera=None):
        self.available = True
        self.camera = camera or FakeCamera()
        self.cameras = OrderedDict([(self.camera.name, self.camera)])
        self.auth = SimpleNamespace(session=None)
        self.refresh_calls = 0
        self.active_operations = 0
        self.max_active_operations = 0

    async def refresh(self, force=False):
        assert force is True
        self.refresh_calls += 1
        self.active_operations += 1
        self.max_active_operations = max(self.max_active_operations, self.active_operations)
        await asyncio.sleep(0)
        self.active_operations -= 1
        return True


def make_adapter(blink=None, camera=None, **kwargs):
    camera = camera or FakeCamera()
    blink = blink or FakeBlink(camera)
    return BlinkAdapter(
        blink,
        camera,
        {
            "name": camera.name,
            "camera_id": camera.camera_id,
            "camera_type": camera.camera_type,
            "serial": camera.serial,
        },
        dependencies_root="unused-in-focused-tests",
        snapshot_delay_seconds=0,
        **kwargs,
    )


async def test_battery_contract_preserves_raw_values_and_converts_voltage() -> None:
    adapter = make_adapter()

    result = await adapter.battery()

    assert result["camera"] == "Front Door"
    assert result["battery_level_raw"] == 3
    assert result["battery_state"] == "ok"
    assert result["battery_voltage_raw"] == 165
    assert result["battery_voltage_volts"] == 1.65
    assert result["blink_battery_check_time"] == "2026-08-06T17:55:00Z"
    assert result["camera_status"] == "online"
    assert result["observed_at_utc"].endswith("Z")


async def test_snapshot_and_battery_operations_are_serialized() -> None:
    blink = FakeBlink()
    adapter = make_adapter(blink=blink, camera=blink.camera)

    await asyncio.gather(adapter.snapshot(), adapter.battery(), adapter.battery())

    assert blink.max_active_operations == 1
    assert blink.camera.snap_calls == 1


async def test_refresh_failure_becomes_structured_http_503() -> None:
    blink = FakeBlink()

    async def fail_refresh(force=False):
        raise ConnectionError("response body must not escape")

    blink.refresh = fail_refresh
    adapter = make_adapter(blink=blink, camera=blink.camera)

    response = await adapter._handle_battery(None)

    assert response.status == 503
    assert b"battery_unavailable" in response.body
    assert b"response body must not escape" not in response.body


def test_health_exposes_readiness_identity_clients_and_last_contact() -> None:
    adapter = make_adapter()
    adapter.last_successful_blink_contact = "2026-08-06T18:00:00Z"

    health = adapter.health()

    assert health == {
        "status": "ok",
        "authentication_ready": True,
        "camera": {
            "name": "Front Door",
            "camera_id": "7",
            "camera_type": "doorbell",
            "serial": "SERIAL-7",
        },
        "stream_clients": 0,
        "last_successful_blink_contact_utc": "2026-08-06T18:00:00Z",
    }


def test_saved_camera_matches_stable_id_and_serial_without_prompt() -> None:
    blink = FakeBlink()

    camera, identity = select_camera(
        blink,
        {"name": "Old Name", "camera_id": "7", "serial": "SERIAL-7"},
        input_fn=lambda _prompt: pytest.fail("saved camera should not prompt"),
    )

    assert camera is blink.camera
    assert identity["name"] == "Front Door"


def test_disappeared_saved_camera_requires_explicit_reselection() -> None:
    blink = FakeBlink()
    prompts = []

    camera, _identity = select_camera(
        blink,
        {"name": "Missing", "camera_id": "99", "serial": "MISSING"},
        input_fn=lambda prompt: prompts.append(prompt) or "1",
    )

    assert camera is blink.camera
    assert prompts == ["Select camera number: "]


class FakeAuth:
    def __init__(self, login_data, no_prompt, session, callback):
        assert no_prompt is True
        self.data = dict(login_data)
        self.session = session
        self.callback = callback
        self.data.setdefault("hardware_id", "STABLE-HARDWARE-ID")
        self.data.setdefault("token", "initial-token")

    @property
    def login_attributes(self):
        return dict(self.data)


class FakeAuthenticatingBlink:
    def __init__(self, session):
        self.auth = None
        self.available = False
        self.camera = FakeCamera()
        self.cameras = OrderedDict([(self.camera.name, self.camera)])

    async def start(self):
        if self.auth.data.get("username") == "expired@example.test":
            return False
        self.available = True
        return True


class FakeTwoFactorRequired(Exception):
    pass


class FakeApi:
    async def oauth_verify_2fa(self, *_args):
        return False


def scripted_blink_type(outcomes, *, mfa_outcomes=()):
    remaining = list(outcomes)
    remaining_mfa = list(mfa_outcomes)

    class ScriptedBlink:
        instances = []

        def __init__(self, session):
            self.auth = None
            self.available = False
            self.camera = FakeCamera()
            self.cameras = OrderedDict([(self.camera.name, self.camera)])
            self.instances.append(self)

        async def start(self):
            outcome = remaining.pop(0)
            if callable(outcome):
                outcome = outcome(self)
            if isinstance(outcome, BaseException):
                raise outcome
            self.available = outcome is True
            return outcome

        async def send_2fa_code(self, _code):
            outcome = remaining_mfa.pop(0)
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

    return ScriptedBlink


async def test_first_authentication_and_token_callback_save_encrypted_payload_shape() -> None:
    store = MemoryStore()
    answers = iter(["person@example.test"])

    result = await authenticate_blink(
        store,
        object(),
        input_fn=lambda _prompt: next(answers),
        password_fn=lambda _prompt: "secret-password",
        blink_types=(FakeAuthenticatingBlink, FakeAuth, FakeTwoFactorRequired, FakeApi()),
    )

    assert result.camera.name == "Front Door"
    assert store.payload["auth"]["username"] == "person@example.test"
    assert store.payload["auth"]["password"] == "secret-password"
    assert store.payload["auth"]["hardware_id"] == "STABLE-HARDWARE-ID"
    assert store.payload["selected_camera"]["camera_id"] == "7"
    assert store.payload["trusted_device"] == {"remember_me_requested": True}

    result.blink.auth.data["token"] = "rotated-token"
    result.blink.auth.callback()
    assert store.payload["auth"]["token"] == "rotated-token"
    assert store.payload["selected_camera"]["serial"] == "SERIAL-7"


async def test_saved_session_false_result_retries_without_credential_prompt() -> None:
    store = MemoryStore(
        {
            "auth": {"username": "expired@example.test", "password": "old"},
            "selected_camera": None,
        }
    )
    Blink = scripted_blink_type([False, True])
    delays = []

    result = await authenticate_blink(
        store,
        object(),
        input_fn=lambda prompt: pytest.fail(f"unexpected prompt: {prompt}"),
        password_fn=lambda prompt: pytest.fail(f"unexpected password prompt: {prompt}"),
        blink_types=(Blink, FakeAuth, FakeTwoFactorRequired, FakeApi()),
        retry_sleep=lambda delay: _record_delay(delays, delay),
        retry_initial_seconds=1,
        retry_max_seconds=4,
    )

    assert result.blink is Blink.instances[1]
    assert delays == [1]
    assert [instance.auth.data["username"] for instance in Blink.instances] == [
        "expired@example.test",
        "expired@example.test",
    ]


async def _record_delay(delays, delay):
    delays.append(delay)


async def test_saved_session_exception_retries_with_capped_exponential_backoff() -> None:
    store = MemoryStore({"auth": {"token": "saved"}, "selected_camera": None})
    Blink = scripted_blink_type(
        [
            ConnectionError("temporary outage"),
            False,
            RuntimeError("BlinkTwoFARequiredError: MFA required"),
            True,
        ]
    )
    delays = []

    await authenticate_blink(
        store,
        object(),
        input_fn=lambda prompt: pytest.fail(f"unexpected prompt: {prompt}"),
        password_fn=lambda prompt: pytest.fail(f"unexpected password prompt: {prompt}"),
        blink_types=(Blink, FakeAuth, FakeTwoFactorRequired, FakeApi()),
        retry_sleep=lambda delay: _record_delay(delays, delay),
        retry_initial_seconds=2,
        retry_max_seconds=5,
        retry_backoff_factor=2,
    )

    assert delays == [2, 4, 5]
    assert len(Blink.instances) == 4


async def test_saved_session_retry_preserves_store_and_propagates_cancellation() -> None:
    original = {"auth": {"username": "saved@example.test"}, "selected_camera": None}
    store = MemoryStore(original.copy())
    Blink = scripted_blink_type([False])

    async def cancel_retry(_delay):
        assert store.payload == original
        assert store.saved == []
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await authenticate_blink(
            store,
            object(),
            input_fn=lambda prompt: pytest.fail(f"unexpected prompt: {prompt}"),
            password_fn=lambda prompt: pytest.fail(f"unexpected password prompt: {prompt}"),
            blink_types=(Blink, FakeAuth, FakeTwoFactorRequired, FakeApi()),
            retry_sleep=cancel_retry,
            retry_initial_seconds=0,
            retry_max_seconds=0,
        )

    assert store.payload == original
    assert store.saved == []


async def test_saved_session_retry_reloads_rotated_encrypted_payload() -> None:
    store = MemoryStore({"auth": {"token": "old-token"}, "selected_camera": None})

    def rotate_from_callback(candidate):
        assert candidate.auth.data["token"] == "old-token"
        candidate.auth.data["token"] = "rotated-token"
        candidate.auth.data["hardware_id"] = "rotated-device"
        candidate.auth.callback()
        return False

    Blink = scripted_blink_type([rotate_from_callback, True])

    await authenticate_blink(
        store,
        object(),
        input_fn=lambda prompt: pytest.fail(f"unexpected prompt: {prompt}"),
        password_fn=lambda prompt: pytest.fail(f"unexpected password prompt: {prompt}"),
        blink_types=(Blink, FakeAuth, FakeTwoFactorRequired, FakeApi()),
        retry_sleep=lambda delay: _record_delay([], delay),
        retry_initial_seconds=0,
        retry_max_seconds=0,
    )

    assert Blink.instances[1].auth.data["token"] == "rotated-token"
    assert Blink.instances[1].auth.data["hardware_id"] == "rotated-device"


async def test_only_exact_two_factor_exception_prompts_for_mfa() -> None:
    class LookalikeTwoFactorRequired(FakeTwoFactorRequired):
        pass

    store = MemoryStore({"auth": {"token": "saved"}, "selected_camera": None})
    Blink = scripted_blink_type([LookalikeTwoFactorRequired("MFA required"), True])

    await authenticate_blink(
        store,
        object(),
        input_fn=lambda prompt: pytest.fail(f"lookalike exception prompted: {prompt}"),
        password_fn=lambda prompt: pytest.fail(f"unexpected password prompt: {prompt}"),
        blink_types=(Blink, FakeAuth, FakeTwoFactorRequired, FakeApi()),
        retry_sleep=lambda delay: _record_delay([], delay),
        retry_initial_seconds=0,
        retry_max_seconds=0,
    )


@pytest.mark.parametrize(
    "mfa_outcome",
    [False, ConnectionError("temporary MFA service outage")],
    ids=["unconfirmed", "exception"],
)
async def test_explicit_mfa_transient_failure_retains_saved_auth_and_retries(
    mfa_outcome,
) -> None:
    original = {"auth": {"token": "saved-token"}, "selected_camera": None}
    store = MemoryStore(original.copy())
    Blink = scripted_blink_type([FakeTwoFactorRequired(), True], mfa_outcomes=[mfa_outcome])
    prompts = []
    delays = []

    result = await authenticate_blink(
        store,
        object(),
        input_fn=lambda prompt: prompts.append(prompt) or "123456",
        password_fn=lambda prompt: pytest.fail(f"unexpected password prompt: {prompt}"),
        blink_types=(Blink, FakeAuth, FakeTwoFactorRequired, FakeApi()),
        retry_sleep=lambda delay: _record_delay(delays, delay),
        retry_initial_seconds=3,
        retry_max_seconds=10,
    )

    assert result.blink is Blink.instances[1]
    assert prompts == ["Enter the Blink MFA code: "]
    assert delays == [3]
    assert Blink.instances[1].auth.data["token"] == "saved-token"
    assert all("123456" not in repr(payload) for payload in store.saved)


async def test_saved_session_start_cancellation_propagates_without_retry() -> None:
    store = MemoryStore({"auth": {"token": "saved"}, "selected_camera": None})
    Blink = scripted_blink_type([asyncio.CancelledError()])
    sleep_called = False

    async def unexpected_sleep(_delay):
        nonlocal sleep_called
        sleep_called = True

    with pytest.raises(asyncio.CancelledError):
        await authenticate_blink(
            store,
            object(),
            input_fn=lambda prompt: pytest.fail(f"unexpected prompt: {prompt}"),
            password_fn=lambda prompt: pytest.fail(f"unexpected password prompt: {prompt}"),
            blink_types=(Blink, FakeAuth, FakeTwoFactorRequired, FakeApi()),
            retry_sleep=unexpected_sleep,
        )

    assert sleep_called is False


async def test_mfa_compatibility_request_marks_device_remembered() -> None:
    request = {}

    class Response:
        status = 201

        async def json(self):
            return {"status": "auth-completed"}

    class Session:
        async def post(self, url, headers, data):
            request.update(url=url, headers=headers, data=data)
            return Response()

    api = SimpleNamespace(
        OAUTH_USER_AGENT="agent",
        OAUTH_SIGNIN_URL="https://signin.example",
        OAUTH_2FA_VERIFY_URL="https://verify.example",
    )

    assert await _verify_2fa_remembered(api, SimpleNamespace(session=Session()), "csrf", "123456")
    assert request["data"] == {
        "2fa_code": "123456",
        "csrf-token": "csrf",
        "remember_me": "true",
    }


class FakeReader:
    def __init__(self, chunks):
        self.chunks = iter(chunks)
        self.reads = 0

    async def read(self, _size):
        self.reads += 1
        return next(self.chunks, b"")


class FakeWriter:
    def __init__(self):
        self.closed = False
        self.waited = False

    def close(self):
        self.closed = True

    async def wait_closed(self):
        self.waited = True


async def test_tcp_handler_observes_eof_and_stops_last_stream_immediately() -> None:
    adapter = make_adapter()
    stopped = []

    async def start_stream():
        return None

    async def stop_stream():
        stopped.append(True)

    adapter._ensure_stream_locked = start_stream
    adapter._stop_stream_locked = stop_stream
    reader = FakeReader([b""])
    writer = FakeWriter()

    await adapter._handle_client(reader, writer)

    assert reader.reads == 1
    assert writer.closed and writer.waited
    assert adapter.clients == []
    assert stopped == [True]


async def test_tcp_initial_stream_failure_closes_client_connection() -> None:
    adapter = make_adapter()

    async def fail_start():
        raise AdapterUnavailable("injected")

    adapter._ensure_stream_locked = fail_start
    reader = FakeReader([])
    writer = FakeWriter()

    await adapter._handle_client(reader, writer)

    assert reader.reads == 0
    assert writer.closed and writer.waited
    assert adapter.clients == []


async def test_managed_upstream_subclass_surfaces_initial_feed_failure(capsys) -> None:
    class BrokenUpstream:
        def __init__(self, server, response):
            self.target_writer = None

        async def feed(self):
            print("private-live-view-host.example:443")
            raise ConnectionError("cannot connect")

    stream = _managed_stream_type(BrokenUpstream)(object(), {})

    with pytest.raises(ConnectionError, match="cannot connect"):
        await stream.feed()
    assert stream.adapter_started.is_set()
    assert isinstance(stream.adapter_start_error, ConnectionError)
    assert "private-live-view-host" not in capsys.readouterr().out


def test_adapter_rejects_non_loopback_bind() -> None:
    with pytest.raises(ValueError, match="loopback"):
        BlinkAdapter._require_loopback("0.0.0.0")


def test_managed_loader_requires_and_loads_the_pinned_manifest(tmp_path) -> None:
    root = tmp_path / "blinkliveview-pinned"
    package = root / "blinkliveview"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "cli.py").write_text("class OnDemandLiveStream: pass\n", encoding="utf-8")
    (root / ".managed-dependency.json").write_text(
        json.dumps(
            {
                "commit": BLINKLIVEVIEW_COMMIT,
                "archive_sha256": BLINKLIVEVIEW_ARCHIVE_SHA256,
            }
        ),
        encoding="utf-8",
    )

    try:
        loaded = load_managed_stream_class(tmp_path)
        assert loaded.__name__ == "OnDemandLiveStream"
    finally:
        sys.modules.pop("blinkliveview.cli", None)
        sys.modules.pop("blinkliveview", None)


def test_managed_loader_rejects_unverified_source(tmp_path) -> None:
    package = tmp_path / "blinkliveview-pinned" / "blinkliveview"
    package.mkdir(parents=True)
    (package / "cli.py").write_text("class OnDemandLiveStream: pass\n", encoding="utf-8")

    with pytest.raises(AdapterUnavailable, match="manifest"):
        load_managed_stream_class(tmp_path)
