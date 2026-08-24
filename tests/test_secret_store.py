from __future__ import annotations

import logging
import os

import pytest

from blink_dashboard.secret_store import (
    MAGIC,
    SecretRedactionFilter,
    SecretStore,
    SecretStoreCorrupt,
    SecretStoreError,
    UnsupportedSecretBackend,
    redact_text,
)


class ReversingProtector:
    def protect(self, plaintext: bytes) -> bytes:
        return b"encrypted:" + plaintext[::-1]

    def unprotect(self, ciphertext: bytes) -> bytes:
        if not ciphertext.startswith(b"encrypted:"):
            raise SecretStoreCorrupt("bad fake ciphertext")
        return ciphertext.removeprefix(b"encrypted:")[::-1]


class FailingProtector(ReversingProtector):
    def protect(self, plaintext: bytes) -> bytes:
        raise SecretStoreError("injected encryption failure")


def test_encrypted_store_round_trip_has_versioned_binary_envelope(tmp_path) -> None:
    path = tmp_path / "runtime" / "private" / "auth.dpapi"
    store = SecretStore(path, protector=ReversingProtector())
    payload = {
        "auth": {
            "username": "person@example.test",
            "password": "super-secret-password",
            "token": "access-secret",
            "refresh_token": "refresh-secret",
            "hardware_id": "38E5FC8D-046F-49B6-BD5C-4D02557FCE6C",
        },
        "selected_camera": {"name": "Front Door", "camera_id": "7", "serial": "ABC"},
    }

    store.save(payload)

    raw = path.read_bytes()
    assert raw.startswith(MAGIC)
    assert b"super-secret-password" not in raw
    assert b"access-secret" not in raw
    assert store.load() == payload
    assert list(path.parent.glob("auth.dpapi.tmp-*")) == []


def test_missing_store_is_first_run(tmp_path) -> None:
    store = SecretStore(tmp_path / "auth.dpapi", protector=ReversingProtector())
    assert store.load() is None


@pytest.mark.parametrize(
    "contents",
    [b"", b"not-a-store", MAGIC + b"truncated"],
)
def test_malformed_store_is_rejected(tmp_path, contents: bytes) -> None:
    path = tmp_path / "auth.dpapi"
    path.write_bytes(contents)
    with pytest.raises(SecretStoreCorrupt):
        SecretStore(path, protector=ReversingProtector()).load()


def test_failed_encryption_keeps_last_valid_store(tmp_path) -> None:
    path = tmp_path / "auth.dpapi"
    store = SecretStore(path, protector=ReversingProtector())
    store.save({"value": "old"})
    before = path.read_bytes()

    with pytest.raises(SecretStoreError):
        SecretStore(path, protector=FailingProtector()).save({"value": "new"})

    assert path.read_bytes() == before
    assert list(tmp_path.glob("auth.dpapi.tmp-*")) == []


def test_payload_must_be_json_serializable(tmp_path) -> None:
    store = SecretStore(tmp_path / "auth.dpapi", protector=ReversingProtector())
    with pytest.raises(SecretStoreError, match="JSON serializable"):
        store.save({"bad": object()})


@pytest.mark.skipif(os.name == "nt", reason="non-Windows safety contract")
def test_default_backend_never_falls_back_to_plaintext(tmp_path) -> None:
    with pytest.raises(UnsupportedSecretBackend, match="no plaintext fallback"):
        SecretStore(tmp_path / "auth.dpapi")


def test_redaction_covers_known_values_headers_and_key_value_text() -> None:
    value = (
        "Authorization: Bearer abc.def password=hunter2 "
        "refresh_token=refresh-value mfa:123456 "
        "{'access_token': 'json-access', \"refresh_token\": \"json-refresh\"}"
    )
    redacted = redact_text(value, ("hunter2",))
    for secret in (
        "abc.def",
        "hunter2",
        "refresh-value",
        "123456",
        "json-access",
        "json-refresh",
    ):
        assert secret not in redacted
    assert redacted.count("[REDACTED]") >= 6


def test_logging_filter_redacts_before_formatting() -> None:
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "token=%s", ("secret-token",), None
    )
    assert SecretRedactionFilter(("secret-token",)).filter(record)
    assert record.args == ()
    assert "secret-token" not in record.getMessage()
