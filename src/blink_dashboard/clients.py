"""Clients for the loopback Blink adapter service."""

from __future__ import annotations

from typing import Any

import aiohttp


class AdapterRequestError(RuntimeError):
    """Raised when the local Blink adapter cannot fulfill a request."""

    def __init__(self, category: str, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.category = category
        self.status = status


class BlinkAdapterClient:
    """Small same-host HTTP client with explicit per-operation timeouts."""

    def __init__(
        self,
        base_url: str,
        *,
        snapshot_timeout: float = 120,
        battery_timeout: float = 30,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.snapshot_timeout = snapshot_timeout
        self.battery_timeout = battery_timeout
        self._session = session
        self._owns_session = session is None

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()

    async def close(self) -> None:
        if self._owns_session and self._session:
            await self._session.close()
        self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None:
            raise RuntimeError("BlinkAdapterClient.start() must be called first")
        return self._session

    async def health(self) -> dict[str, Any]:
        try:
            async with self.session.get(
                f"{self.base_url}/", timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                payload = await response.json(content_type=None)
                if response.status != 200:
                    raise AdapterRequestError(
                        "adapter_health",
                        payload.get("error", "adapter is not ready"),
                        status=response.status,
                    )
                return payload
        except AdapterRequestError:
            raise
        except Exception as exc:
            raise AdapterRequestError("adapter_health", str(exc)) from exc

    async def snapshot(self) -> bytes:
        try:
            async with self.session.get(
                f"{self.base_url}/snapshot",
                timeout=aiohttp.ClientTimeout(total=self.snapshot_timeout),
            ) as response:
                body = await response.read()
                if response.status != 200:
                    message = body.decode("utf-8", errors="replace")[:300]
                    raise AdapterRequestError("snapshot_failure", message, status=response.status)
                return body
        except AdapterRequestError:
            raise
        except TimeoutError as exc:
            raise AdapterRequestError("snapshot_timeout", "snapshot request timed out") from exc
        except Exception as exc:
            raise AdapterRequestError("snapshot_failure", str(exc)) from exc

    async def battery(self) -> dict[str, Any]:
        try:
            async with self.session.get(
                f"{self.base_url}/battery",
                timeout=aiohttp.ClientTimeout(total=self.battery_timeout),
            ) as response:
                payload = await response.json(content_type=None)
                if response.status != 200:
                    raise AdapterRequestError(
                        "battery_endpoint_failure",
                        payload.get("error", "battery refresh failed"),
                        status=response.status,
                    )
                return payload
        except AdapterRequestError:
            raise
        except TimeoutError as exc:
            raise AdapterRequestError(
                "battery_endpoint_failure", "battery request timed out"
            ) from exc
        except Exception as exc:
            raise AdapterRequestError("battery_endpoint_failure", str(exc)) from exc


__all__ = ["AdapterRequestError", "BlinkAdapterClient"]
