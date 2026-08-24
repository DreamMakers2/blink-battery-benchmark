"""Console entry point for the loopback-only Blink adapter process."""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
import signal
import sys

from .blink_adapter import BlinkAdapter, authenticate_blink
from .logging_setup import configure_logging
from .secret_store import (
    SecretRedactionFilter,
    SecretStore,
    SecretStoreError,
    UnsupportedSecretBackend,
)


LOGGER = logging.getLogger(__name__)


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Blink battery dashboard adapter")
    parser.add_argument("--project-root", type=Path, default=_project_root())
    parser.add_argument("--http-port", type=int, default=8080)
    parser.add_argument("--tcp-port", type=int, default=5000)
    parser.add_argument("--snapshot-delay", type=float, default=1.0)
    parser.add_argument("--stream-start-timeout", type=float, default=10.0)
    return parser.parse_args(argv)


def _add_redaction_filter(secrets: tuple[str, ...]) -> None:
    redactor = SecretRedactionFilter(secrets)
    for handler in logging.getLogger().handlers:
        handler.addFilter(redactor)


async def async_main(argv: list[str] | None = None) -> int:
    args = _arguments(argv)
    project_root = args.project_root.resolve()
    configure_logging(project_root / "runtime" / "logs" / "adapter.log")
    secret_path = project_root / "runtime" / "private" / "auth.dpapi"
    dependencies_root = project_root / "runtime" / "deps"

    try:
        store = SecretStore(secret_path)
    except UnsupportedSecretBackend as exc:
        print(f"Adapter cannot start: {exc}", file=sys.stderr)
        return 2

    try:
        existing = store.load() or {}
    except SecretStoreError as exc:
        print(f"Adapter cannot read encrypted credentials: {exc}", file=sys.stderr)
        return 2
    existing_auth = existing.get("auth") if isinstance(existing, dict) else None
    if isinstance(existing_auth, dict):
        _add_redaction_filter(
            tuple(
                str(existing_auth.get(key) or "") for key in ("password", "token", "refresh_token")
            )
        )

    # A single session is owned by Blinkpy for the lifetime of this process.
    from aiohttp import ClientSession

    try:
        async with ClientSession() as session:
            result = await authenticate_blink(store, session)
            _add_redaction_filter(
                tuple(
                    str(result.blink.auth.login_attributes.get(key) or "")
                    for key in ("password", "token", "refresh_token")
                )
            )
            adapter = BlinkAdapter(
                result.blink,
                result.camera,
                result.camera_identity,
                dependencies_root=dependencies_root,
                snapshot_delay_seconds=args.snapshot_delay,
                stream_start_timeout_seconds=args.stream_start_timeout,
            )
            await adapter.start_http(host="127.0.0.1", port=args.http_port)
            try:
                await adapter.start_tcp(host="127.0.0.1", port=args.tcp_port)
            except BaseException:
                await adapter.stop()
                raise

            stop_event = asyncio.Event()
            loop = asyncio.get_running_loop()

            def request_stop() -> None:
                stop_event.set()

            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, request_stop)
                except (NotImplementedError, RuntimeError):
                    # Windows' default Proactor loop does not implement this API.
                    signal.signal(sig, lambda *_args: loop.call_soon_threadsafe(request_stop))

            print(
                "Blink adapter ready: "
                f"http://127.0.0.1:{args.http_port} and "
                f"tcp://127.0.0.1:{args.tcp_port}"
            )
            try:
                await stop_event.wait()
            finally:
                await adapter.stop()
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        LOGGER.error("Blink adapter stopped (%s).", type(exc).__name__)
        print(
            "Blink adapter could not start. See the redacted application log for details.",
            file=sys.stderr,
        )
        return 1
    return 0


def main() -> int:
    try:
        return asyncio.run(async_main())
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":  # pragma: no cover - exercised by Windows acceptance test
    raise SystemExit(main())
