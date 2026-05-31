"""Tests for tui_client.py — the async HTTP client + pure helpers used by the TUI.

The client is exercised by mounting it against the real Starlette ASGI app via
httpx.ASGITransport, so we cover the full JSON API contract without a real
socket. Mirrors claude_bridge/tests/test_tui_client.py.
"""

import asyncio

import httpx
import pytest

import claude_replay.server as server
import claude_replay.store as store
from claude_replay.tui_client import (
    EVENT_COLORS,
    STATUS_COLORS,
    ReplayClient,
    ReplayError,
    event_color,
    event_glyph,
    short_id,
    status_color,
    status_glyph,
)


# ── Pure helpers (no I/O) ─────────────────────────────────────────────────────

def test_status_color_known():
    assert status_color("running") == STATUS_COLORS["running"]
    assert status_color("completed") == STATUS_COLORS["completed"]
    assert status_color("error") == STATUS_COLORS["error"]


def test_status_color_is_case_insensitive():
    assert status_color("RUNNING") == status_color("running")


def test_status_color_unknown_falls_back():
    assert status_color("weird") == "#8b949e"
    assert status_color(None) == "#8b949e"


def test_status_glyph():
    assert status_glyph("running") == "●"
    assert status_glyph("completed") == "✓"
    assert status_glyph("error") == "✗"
    assert status_glyph("interrupted") == "○"
    assert status_glyph(None) == "○"


def test_event_color_and_glyph():
    assert event_color("tool_use") == EVENT_COLORS["tool_use"]
    assert event_color("tool_result") == EVENT_COLORS["tool_result"]
    assert event_color("mystery") == "#8b949e"
    assert event_glyph("tool_use") == "→"
    assert event_glyph("tool_result") == "✓"
    assert event_glyph("stop") == "■"
    assert event_glyph("nope") == "·"


def test_short_id():
    assert short_id("abcdef1234567890") == "abcdef12"
    assert short_id("abcdef1234567890", width=4) == "abcd"
    assert short_id(None) == ""
    assert short_id("") == ""


# ── ReplayClient — full async lifecycle against the real ASGI app ─────────────

@pytest.fixture
def asgi_client(fresh_db):
    """A ReplayClient bound to the in-process ASGI app — no real socket."""
    transport = httpx.ASGITransport(app=server.app)
    client = ReplayClient(base_url="http://testserver")
    client._client = httpx.AsyncClient(
        transport=transport, base_url="http://testserver", timeout=5.0
    )
    return client


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _seed_session(sid: str = "sess-aaaaaaaa-1111") -> str:
    store.get_or_create_session(
        sid, project_dir="/tmp/proj", model="claude-opus-4-8", objective="ship the TUI"
    )
    store.insert_event(sid, "tool_use", tool_name="Read", tool_input={"file_path": "/tmp/a.py"})
    store.insert_event(sid, "tool_result", tool_name="Read", tool_input={"file_path": "/tmp/a.py"})
    store.write_checkpoint(sid, "read the file", step_next="edit it", files_touched=["/tmp/a.py"])
    return sid


def test_state_round_trip(asgi_client):
    async def go():
        try:
            state = await asgi_client.state()
            assert state["service"] == "claude-replay"
            assert state["sessions"] == []

            _seed_session()
            state2 = await asgi_client.state()
            assert state2["total_sessions"] == 1
            assert len(state2["sessions"]) == 1
            s = state2["sessions"][0]
            assert s["model"] == "claude-opus-4-8"
            assert s["events"] == 2
            assert s["checkpoints"] == 1
        finally:
            await asgi_client.aclose()
    run(go())


def test_session_detail(asgi_client):
    async def go():
        try:
            sid = _seed_session()
            detail = await asgi_client.session(sid)
            assert detail["session"]["id"] == sid
            assert detail["session"]["objective"] == "ship the TUI"
            assert len(detail["events"]) == 2
            assert detail["events"][0]["event_type"] == "tool_use"
            assert len(detail["checkpoints"]) == 1
            cp = detail["checkpoints"][0]
            assert cp["step_done"] == "read the file"
            assert cp["files_touched"] == ["/tmp/a.py"]
        finally:
            await asgi_client.aclose()
    run(go())


def test_session_not_found_raises(asgi_client):
    async def go():
        try:
            with pytest.raises(ReplayError) as exc:
                await asgi_client.session("does-not-exist")
            assert exc.value.status == 404
        finally:
            await asgi_client.aclose()
    run(go())


def test_resume_round_trip(asgi_client):
    async def go():
        try:
            sid = _seed_session()
            data = await asgi_client.resume(sid)
            assert data["session_id"] == sid
            assert isinstance(data["brief"], str)
            assert data["brief"].strip()
        finally:
            await asgi_client.aclose()
    run(go())


def test_export_round_trip(asgi_client, tmp_path):
    async def go():
        try:
            sid = _seed_session()
            data = await asgi_client.export(sid, output=str(tmp_path))
            assert data["session_id"] == sid
            assert data["path"].endswith(".html")
        finally:
            await asgi_client.aclose()
    run(go())


def test_checkpoint_via_api(asgi_client):
    async def go():
        try:
            sid = _seed_session()
            data = await asgi_client.checkpoint(session_id=sid, note="manual via tui")
            assert data["session_id"] == sid
            # seed wrote checkpoint #1, this is #2
            assert data["seq"] == 2
        finally:
            await asgi_client.aclose()
    run(go())


def test_checkpoint_unknown_session_raises(asgi_client):
    async def go():
        try:
            with pytest.raises(ReplayError) as exc:
                await asgi_client.checkpoint(session_id="ghost")
            assert exc.value.status == 404
        finally:
            await asgi_client.aclose()
    run(go())
