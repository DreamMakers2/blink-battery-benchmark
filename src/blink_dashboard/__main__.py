"""Parameter-free launcher target for the Blink battery dashboard."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import signal
import sys
from urllib.parse import quote

from aiohttp import web

from .app import DashboardApplication, create_web_app
from .config import load_config
from .logging_setup import configure_logging
from .supervisor import (
    StartupError,
    dashboard_is_running,
    load_or_create_dashboard_token,
    managed_adapter,
    open_dashboard,
)


LOGGER = logging.getLogger(__name__)
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


async def run() -> int:
    root = project_root()
    config = load_config(root)
    configure_logging(config.paths.log_file)
    host = config.server.dashboard_host
    if not config.server.allow_lan and host.casefold() not in LOOPBACK_HOSTS:
        raise StartupError(
            "server.dashboard_host must be loopback unless server.allow_lan is explicitly true"
        )
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in browser_host and not browser_host.startswith("["):
        browser_host = f"[{browser_host}]"
    dashboard_url = f"http://{browser_host}:{config.server.dashboard_port}"
    access_token = (
        load_or_create_dashboard_token(config.paths.private_dir / "dashboard-access.token")
        if config.server.allow_lan
        else None
    )
    browser_url = (
        f"{dashboard_url}/?token={quote(access_token, safe='')}"
        if access_token is not None
        else dashboard_url
    )

    if await dashboard_is_running(dashboard_url):
        print(f"Dashboard is already running at {dashboard_url}")
        open_dashboard(browser_url)
        return 0

    print("Blink Battery Dashboard")
    print("On first launch, complete the Blink credential and MFA prompts in this window.")

    async with managed_adapter(root, config):
        services = DashboardApplication(root, config, access_token=access_token)
        runner: web.AppRunner | None = None
        try:
            await services.start()
            runner = web.AppRunner(create_web_app(services), access_log=LOGGER)
            await runner.setup()
            site = web.TCPSite(runner, host, config.server.dashboard_port)
            await site.start()
            print(f"Dashboard ready: {dashboard_url}")
            open_dashboard(browser_url)

            stopped = asyncio.Event()
            loop = asyncio.get_running_loop()

            def request_stop() -> None:
                stopped.set()

            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, request_stop)
                except (NotImplementedError, RuntimeError):
                    signal.signal(sig, lambda *_args: loop.call_soon_threadsafe(request_stop))
            await stopped.wait()
        finally:
            if runner is not None:
                await runner.cleanup()
            await services.stop()
    return 0


def main() -> int:
    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 130
    except (StartupError, OSError, ValueError) as exc:
        logging.getLogger(__name__).error("Startup failed: %s", exc)
        print(f"Dashboard could not start: {exc}", file=sys.stderr)
        return 1
    except Exception:
        logging.getLogger(__name__).exception("Unexpected dashboard failure")
        print(
            "Dashboard stopped unexpectedly. See runtime/logs/application.log for details.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":  # pragma: no cover - invoked by launch.cmd
    raise SystemExit(main())
