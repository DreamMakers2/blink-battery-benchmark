"""Encrypted, project-local persistence for Blink authentication state.

The production backend is deliberately Windows-only.  DPAPI binds ciphertext to
the Windows user that created it; there is no plaintext or cross-platform
fallback.  ``Protector`` is injectable solely so the binary envelope and atomic
replacement behaviour can be tested on non-Windows development hosts.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import re
import struct
import tempfile
from typing import Any, Mapping, Protocol


MAGIC = b"BLINK-AUTH-DPAPI\x00"
FORMAT_VERSION = 1
_HEADER = struct.Struct(">17sI")


class SecretStoreError(RuntimeError):
    """Base error for an unreadable or unwritable encrypted secret store."""


class UnsupportedSecretBackend(SecretStoreError):
    """Raised when Windows current-user DPAPI is unavailable."""


class SecretStoreCorrupt(SecretStoreError):
    """Raised when an encrypted store is malformed or cannot be decrypted."""


class Protector(Protocol):
    """Minimal encryption interface used by :class:`SecretStore`."""

    def protect(self, plaintext: bytes) -> bytes: ...

    def unprotect(self, ciphertext: bytes) -> bytes: ...


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _blob(data: bytes) -> tuple[_DATA_BLOB, object]:
    # Keep the allocated array alive for the duration of the CryptProtectData call.
    buffer = ctypes.create_string_buffer(data, len(data))
    return (
        _DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte))),
        buffer,
    )


class WindowsDPAPIProtector:
    """Encrypt bytes with Windows DPAPI in current-user scope."""

    _CRYPTPROTECT_UI_FORBIDDEN = 0x01

    def __init__(self, description: str = "Blink battery dashboard credentials") -> None:
        if os.name != "nt":
            raise UnsupportedSecretBackend(
                "Blink credentials require Windows current-user DPAPI; "
                "no plaintext fallback is available."
            )
        self.description = description
        self._crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_signatures()

    def _configure_signatures(self) -> None:
        self._crypt32.CryptProtectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DATA_BLOB),
        ]
        self._crypt32.CryptProtectData.restype = wintypes.BOOL
        self._crypt32.CryptUnprotectData.argtypes = [
            ctypes.POINTER(_DATA_BLOB),
            ctypes.POINTER(wintypes.LPWSTR),
            ctypes.POINTER(_DATA_BLOB),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DATA_BLOB),
        ]
        self._crypt32.CryptUnprotectData.restype = wintypes.BOOL
        self._kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        self._kernel32.LocalFree.restype = ctypes.c_void_p

    def protect(self, plaintext: bytes) -> bytes:
        incoming, keepalive = _blob(plaintext)
        outgoing = _DATA_BLOB()
        ok = self._crypt32.CryptProtectData(
            ctypes.byref(incoming),
            self.description,
            None,
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(outgoing),
        )
        # The blob stores only a pointer, so retain the Python allocation through
        # the native call rather than allowing premature garbage collection.
        _ = keepalive
        if not ok:
            raise SecretStoreError(
                f"Windows DPAPI encryption failed (error {ctypes.get_last_error()})."
            )
        try:
            return ctypes.string_at(outgoing.pbData, outgoing.cbData)
        finally:
            self._kernel32.LocalFree(outgoing.pbData)

    def unprotect(self, ciphertext: bytes) -> bytes:
        incoming, keepalive = _blob(ciphertext)
        outgoing = _DATA_BLOB()
        description = wintypes.LPWSTR()
        ok = self._crypt32.CryptUnprotectData(
            ctypes.byref(incoming),
            ctypes.byref(description),
            None,
            None,
            None,
            self._CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(outgoing),
        )
        _ = keepalive
        if not ok:
            raise SecretStoreCorrupt(
                "The encrypted Blink credentials cannot be decrypted for this Windows user "
                f"(error {ctypes.get_last_error()})."
            )
        try:
            return ctypes.string_at(outgoing.pbData, outgoing.cbData)
        finally:
            self._kernel32.LocalFree(outgoing.pbData)
            if description:
                self._kernel32.LocalFree(description)


class SecretStore:
    """Versioned DPAPI envelope with same-directory atomic replacement."""

    def __init__(self, path: str | Path, *, protector: Protector | None = None) -> None:
        self.path = Path(path)
        self.protector = protector if protector is not None else WindowsDPAPIProtector()

    def load(self) -> dict[str, Any] | None:
        """Decrypt the stored JSON object, or return ``None`` before first setup."""

        try:
            encoded = self.path.read_bytes()
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SecretStoreError("Unable to read the encrypted credential store.") from exc

        if len(encoded) < _HEADER.size:
            raise SecretStoreCorrupt("The encrypted credential store is truncated.")
        magic, version = _HEADER.unpack_from(encoded)
        if magic != MAGIC:
            raise SecretStoreCorrupt("The encrypted credential store has an invalid header.")
        if version != FORMAT_VERSION:
            raise SecretStoreCorrupt(f"Unsupported encrypted credential format version: {version}.")
        try:
            plaintext = self.protector.unprotect(encoded[_HEADER.size :])
            document = json.loads(plaintext.decode("utf-8"))
        except SecretStoreError:
            raise
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise SecretStoreCorrupt("The decrypted credential payload is invalid.") from exc
        if not isinstance(document, dict) or document.get("schema_version") != FORMAT_VERSION:
            raise SecretStoreCorrupt("The decrypted credential payload has an invalid schema.")
        payload = document.get("payload")
        if not isinstance(payload, dict):
            raise SecretStoreCorrupt("The decrypted credential payload is not an object.")
        return payload

    def save(self, payload: Mapping[str, Any]) -> None:
        """Encrypt and atomically replace the complete authentication payload."""

        document = {"schema_version": FORMAT_VERSION, "payload": dict(payload)}
        try:
            plaintext = json.dumps(
                document,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise SecretStoreError("Credential payload is not JSON serializable.") from exc

        ciphertext = self.protector.protect(plaintext)
        envelope = _HEADER.pack(MAGIC, FORMAT_VERSION) + ciphertext
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SecretStoreError("Unable to create the private credential directory.") from exc

        descriptor = -1
        temporary_name: str | None = None
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f"{self.path.name}.tmp-", dir=self.path.parent
            )
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(envelope)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_name, self.path)
            temporary_name = None
        except OSError as exc:
            raise SecretStoreError("Unable to atomically save encrypted credentials.") from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_name is not None:
                try:
                    os.unlink(temporary_name)
                except FileNotFoundError:
                    pass


_KEY_VALUE_SECRET = re.compile(
    r"(?i)([\"']?(?:authorization|password|passwd|access_token|refresh_token|token|"
    r"2fa(?:_code)?|mfa)[\"']?\s*[:=]\s*[\"']?)([^\"'\s,;}]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")


def redact_text(value: object, secrets: tuple[str, ...] = ()) -> str:
    """Return a log-safe rendering of text containing known secret values."""

    text = str(value)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _KEY_VALUE_SECRET.sub(lambda match: f"{match.group(1)}[REDACTED]", text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


class SecretRedactionFilter:
    """Logging filter that removes credentials before any handler formats them."""

    def __init__(self, secrets: tuple[str, ...] = ()) -> None:
        self.secrets = tuple(secret for secret in secrets if secret)

    def filter(self, record: object) -> bool:
        # Avoid importing logging just for the Protocol-like record shape.
        get_message = getattr(record, "getMessage")
        setattr(record, "msg", redact_text(get_message(), self.secrets))
        setattr(record, "args", ())
        # Exception formatting happens after filters and could otherwise append a
        # raw HTTP body or URL containing credentials to an already-redacted line.
        if getattr(record, "exc_info", None):
            setattr(record, "exc_info", None)
            setattr(record, "exc_text", None)
        return True


__all__ = [
    "FORMAT_VERSION",
    "MAGIC",
    "Protector",
    "SecretRedactionFilter",
    "SecretStore",
    "SecretStoreCorrupt",
    "SecretStoreError",
    "UnsupportedSecretBackend",
    "WindowsDPAPIProtector",
    "redact_text",
]
