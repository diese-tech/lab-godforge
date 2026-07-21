"""HTTP client for the optional activity (draft) backend.

Infrastructure module for the Issue #48 refactor. It wraps the small REST surface
GodForge uses when ``ACTIVITY_BACKEND_URL`` is configured, isolating aiohttp and
the API-key header from ``bot.py``. All failures are swallowed to ``None`` with a
log line, preserving the previous best-effort behavior.
"""

from __future__ import annotations

import logging

import aiohttp

log = logging.getLogger("godforge.activity_backend")


class ActivityBackendClient:
    """Thin async REST client for the draft activity backend."""

    def __init__(self, base_url: str, api_key: str) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key

    @property
    def enabled(self) -> bool:
        return bool(self.base_url)

    def headers(self) -> dict:
        return {"X-Api-Key": self.api_key, "Content-Type": "application/json"}

    def ws_url(self) -> str:
        return (
            self.base_url.replace("https://", "wss://").replace("http://", "ws://")
            + "/ws"
        )

    async def post(self, path: str, data: dict | None = None) -> dict | None:
        url = self.base_url + path
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    url, json=data or {}, headers=self.headers()
                ) as resp:
                    return await resp.json()
        except Exception as exc:
            log.error("Activity backend POST %s failed: %s", path, exc)
            return None

    async def get(self, path: str) -> dict | None:
        url = self.base_url + path
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, headers=self.headers()) as resp:
                    return await resp.json()
        except Exception as exc:
            log.error("Activity backend GET %s failed: %s", path, exc)
            return None
