"""Async HTTP client for the Claude Replay JSON API.

Used by claude_replay.tui. Wraps the read/write endpoints the server exposes:
    GET  /api/state
    GET  /api/session/{id}
    GET  /api/resume/{id}
    GET  /api/export/{id}[?output=DIR]
    POST /api/checkpoint

Kept separate from the UI so it's unit-testable without spinning up Textual —
mirrors claude_bridge.tui_client (the BridgeClient pattern).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


# Session-status colours — keep in sync with the dashboard / trace dark theme.
STATUS_COLORS: dict[str, str] = {
    "running":     "#3fb950",  # green
    "completed":   "#58a6ff",  # blue
    "done":        "#58a6ff",  # blue
    "interrupted": "#d97706",  # amber
    "stopped":     "#d97706",  # amber
    "error":       "#f85149",  # red
    "failed":      "#f85149",  # red
}

# Event-type colours + glyphs — match the trace.html legend.
EVENT_COLORS: dict[str, str] = {
    "tool_use":    "#7dd3fc",  # cyan
    "tool_result": "#3fb950",  # green
    "stop":        "#d97706",  # amber
    "error":       "#f85149",  # red
}
EVENT_GLYPHS: dict[str, str] = {
    "tool_use":    "→",
    "tool_result": "✓",
    "stop":        "■",
    "error":       "✗",
}

DEFAULT_STATUS_COLOR = "#8b949e"


def status_color(status: str | None) -> str:
    return STATUS_COLORS.get((status or "").lower(), DEFAULT_STATUS_COLOR)


def status_glyph(status: str | None) -> str:
    s = (status or "").lower()
    if s == "running":
        return "●"
    if s in ("completed", "done"):
        return "✓"
    if s in ("error", "failed"):
        return "✗"
    return "○"


def event_color(event_type: str | None) -> str:
    return EVENT_COLORS.get((event_type or "").lower(), DEFAULT_STATUS_COLOR)


def event_glyph(event_type: str | None) -> str:
    return EVENT_GLYPHS.get((event_type or "").lower(), "·")


def short_id(session_id: str | None, width: int = 8) -> str:
    """First `width` chars of a session id (the dashboard's id_short convention)."""
    return (session_id or "")[:width]


@dataclass
class ReplayError(Exception):
    status: int
    body: str

    def __str__(self) -> str:
        return f"HTTP {self.status}: {self.body[:200]}"


class ReplayClient:
    """Thin async wrapper around Claude Replay's JSON API."""

    def __init__(self, base_url: str = "http://127.0.0.1:8766", timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "ReplayClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        clean = {k: v for k, v in params.items() if v is not None}
        r = await self._client.get(path, params=clean)
        if r.status_code >= 400:
            raise ReplayError(r.status_code, r.text)
        return r.json()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        r = await self._client.post(path, json=body)
        if r.status_code >= 400:
            raise ReplayError(r.status_code, r.text)
        return r.json()

    async def state(self) -> dict[str, Any]:
        return await self._get("/api/state")

    async def session(self, session_id: str) -> dict[str, Any]:
        return await self._get(f"/api/session/{session_id}")

    async def resume(self, session_id: str) -> dict[str, Any]:
        return await self._get(f"/api/resume/{session_id}")

    async def export(self, session_id: str, output: str | None = None) -> dict[str, Any]:
        return await self._get(f"/api/export/{session_id}", output=output)

    async def checkpoint(
        self, session_id: str | None = None, note: str | None = None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {}
        if session_id is not None:
            body["session_id"] = session_id
        if note is not None:
            body["note"] = note
        return await self._post("/api/checkpoint", body)
