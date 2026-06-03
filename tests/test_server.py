"""Tests for claude_replay.server — JSON API + MCP dispatch_tool."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

import claude_replay.server as server
import claude_replay.store as store


@pytest.fixture
def client(fresh_db):
    return TestClient(server.app)


def seed(sid="srv-sess-1", **kw):
    store.get_or_create_session(
        sid,
        project_dir=kw.get("project_dir", "/proj"),
        model=kw.get("model", "claude-opus-4-8"),
        objective=kw.get("objective", "Do server work"),
    )
    store.insert_event(sid, "tool_use", tool_name="Read", tool_input={"file_path": "a.py"})
    store.insert_event(sid, "tool_result", tool_name="Read", tool_result={"ok": True})
    store.insert_event(sid, "tool_result", tool_name="Write", tool_input={"file_path": "b.py"})
    store.write_checkpoint(sid, "Did work", step_next="More", files_touched=["b.py"])
    return sid


# ── /status ───────────────────────────────────────────────────────────────────

class TestStatus:
    def test_status(self, client):
        r = client.get("/status")
        assert r.status_code == 200
        d = r.json()
        assert d["service"] == "claude-replay"
        assert d["status"] == "online"
        assert d["version"] == store.VERSION
        # Bare healthcheck — no DB path leaks
        assert "db_path" not in d
        assert "sessions" not in d


# ── /api/state ────────────────────────────────────────────────────────────────

class TestApiState:
    def test_empty(self, client):
        r = client.get("/api/state")
        assert r.status_code == 200
        d = r.json()
        assert d["total_sessions"] == 0
        assert d["sessions"] == []

    def test_lists_sessions(self, client):
        seed("s1")
        seed("s2")
        d = client.get("/api/state").json()
        assert d["total_sessions"] == 2
        ids = [s["id"] for s in d["sessions"]]
        assert "s1" in ids and "s2" in ids

    def test_session_summary_fields(self, client):
        seed()
        s = client.get("/api/state").json()["sessions"][0]
        assert s["events"] == 3
        assert s["checkpoints"] == 1
        assert s["status"] == "running"
        assert s["id_short"] == "srv-sess"
        assert s["objective"] == "Do server work"

    def test_uptime_present(self, client):
        d = client.get("/api/state").json()
        assert "uptime_seconds" in d
        assert "uptime_human" in d


# ── /api/session/<id> ─────────────────────────────────────────────────────────

class TestApiSession:
    def test_detail(self, client):
        seed()
        d = client.get("/api/session/srv-sess-1").json()
        assert d["session"]["id"] == "srv-sess-1"
        assert len(d["events"]) == 3
        assert len(d["checkpoints"]) == 1

    def test_events_have_tool_names(self, client):
        seed()
        d = client.get("/api/session/srv-sess-1").json()
        names = [e["tool_name"] for e in d["events"]]
        assert "Read" in names and "Write" in names

    def test_missing_404(self, client):
        r = client.get("/api/session/nope")
        assert r.status_code == 404
        assert r.json()["error"] == "not found"


# ── /api/resume/<id> ──────────────────────────────────────────────────────────

class TestApiResume:
    def test_resume(self, client):
        seed()
        d = client.get("/api/resume/srv-sess-1").json()
        assert d["session_id"] == "srv-sess-1"
        assert "Claude Replay — Session Resume Brief" in d["brief"]
        assert "Do server work" in d["brief"]

    def test_missing_404(self, client):
        assert client.get("/api/resume/nope").status_code == 404


# ── /api/export/<id> ──────────────────────────────────────────────────────────

class TestApiExport:
    def test_export(self, client, tmp_path):
        seed()
        r = client.get(f"/api/export/srv-sess-1?output={tmp_path}")
        assert r.status_code == 200
        path = r.json()["path"]
        assert path.endswith(".html")
        assert (tmp_path).exists()

    def test_missing_404(self, client, tmp_path):
        assert client.get(f"/api/export/nope?output={tmp_path}").status_code == 404


# ── POST /api/checkpoint ──────────────────────────────────────────────────────

class TestApiCheckpoint:
    def test_manual_checkpoint(self, client):
        seed()
        r = client.post("/api/checkpoint", json={"session_id": "srv-sess-1", "note": "checkpoint me"})
        assert r.status_code == 200
        assert r.json()["seq"] == 2  # one auto-seeded + this one
        cp = store.get_latest_checkpoint("srv-sess-1")
        assert cp["step_done"] == "checkpoint me"

    def test_checkpoint_defaults_to_latest(self, client):
        seed()
        r = client.post("/api/checkpoint", json={})
        assert r.status_code == 200
        assert r.json()["session_id"] == "srv-sess-1"

    def test_checkpoint_no_session(self, client):
        r = client.post("/api/checkpoint", json={})
        assert r.status_code == 400

    def test_checkpoint_unknown_session(self, client):
        seed()
        r = client.post("/api/checkpoint", json={"session_id": "ghost"})
        assert r.status_code == 404

    def test_checkpoint_invalid_json(self, client):
        seed()
        r = client.post("/api/checkpoint", content="not json")
        assert r.status_code == 400

    def test_checkpoint_without_note(self, client):
        seed()
        r = client.post("/api/checkpoint", json={"session_id": "srv-sess-1"})
        assert r.status_code == 200
        cp = store.get_latest_checkpoint("srv-sess-1")
        assert "Manual checkpoint" in cp["step_done"]


# ── MCP dispatch_tool ─────────────────────────────────────────────────────────

class TestDispatchTool:
    async def test_status_empty(self, fresh_db):
        out = await server.dispatch_tool("replay_status", {})
        assert "No sessions" in out[0].text

    async def test_status(self, fresh_db):
        seed()
        out = await server.dispatch_tool("replay_status", {})
        assert "Do server work" in out[0].text
        assert "Checkpoints: 1" in out[0].text

    async def test_checkpoint(self, fresh_db):
        seed()
        out = await server.dispatch_tool("replay_checkpoint", {"note": "n"})
        assert "Checkpoint #2" in out[0].text
        assert store.count_checkpoints("srv-sess-1") == 2

    async def test_checkpoint_no_session(self, fresh_db):
        out = await server.dispatch_tool("replay_checkpoint", {})
        assert "No active session" in out[0].text

    async def test_resume(self, fresh_db):
        seed()
        out = await server.dispatch_tool("replay_resume", {})
        assert "Session Resume Brief" in out[0].text

    async def test_resume_explicit_id(self, fresh_db):
        seed("explicit")
        out = await server.dispatch_tool("replay_resume", {"session_id": "explicit"})
        assert "Session Resume Brief" in out[0].text

    async def test_resume_no_sessions(self, fresh_db):
        out = await server.dispatch_tool("replay_resume", {})
        assert "No sessions" in out[0].text

    async def test_sessions(self, fresh_db):
        seed("s1")
        seed("s2")
        out = await server.dispatch_tool("replay_sessions", {})
        assert "Recent sessions (2)" in out[0].text

    async def test_sessions_empty(self, fresh_db):
        out = await server.dispatch_tool("replay_sessions", {})
        assert "No sessions" in out[0].text

    async def test_export(self, fresh_db, tmp_path):
        seed()
        out = await server.dispatch_tool("replay_export", {"output": str(tmp_path)})
        assert "Exported trace" in out[0].text
        assert list(tmp_path.glob("*.html"))

    async def test_export_no_sessions(self, fresh_db, tmp_path):
        out = await server.dispatch_tool("replay_export", {"output": str(tmp_path)})
        assert "No sessions" in out[0].text

    async def test_unknown_tool(self, fresh_db):
        out = await server.dispatch_tool("bogus", {})
        assert "Unknown tool" in out[0].text

    async def test_search_match(self, fresh_db):
        seed("srv-sess-1")
        out = await server.dispatch_tool("replay_search", {"query": "Write"})
        assert "srv-sess" in out[0].text

    async def test_search_no_match(self, fresh_db):
        seed()
        out = await server.dispatch_tool("replay_search", {"query": "zzzznope"})
        assert "No matches" in out[0].text

    async def test_search_empty_query(self, fresh_db):
        out = await server.dispatch_tool("replay_search", {"query": "  "})
        assert "query or at least one filter" in out[0].text

    async def test_search_filter_only(self, fresh_db):
        seed("srv-sess-1")  # uses Read + Write tools
        out = await server.dispatch_tool("replay_search", {"query": "", "tool": "Write"})
        assert "srv-sess" in out[0].text

    async def test_tag_sets_name_and_tags(self, fresh_db):
        seed("srv-sess-1")
        out = await server.dispatch_tool(
            "replay_tag", {"session_id": "srv-sess-1", "name": "Run A", "add": ["bug"]}
        )
        assert "Run A" in out[0].text
        s = store.get_session("srv-sess-1")
        assert s["name"] == "Run A" and s["tags"] == ["bug"]

    async def test_tag_unknown_session(self, fresh_db):
        out = await server.dispatch_tool("replay_tag", {"session_id": "ghost", "add": ["x"]})
        assert "no session" in out[0].text


# ── list_tools ────────────────────────────────────────────────────────────────

class TestListTools:
    async def test_ten_tools(self, fresh_db):
        tools = await server.list_tools()
        names = {t.name for t in tools}
        assert names == {
            "replay_status", "replay_checkpoint", "replay_resume", "replay_sessions",
            "replay_export", "replay_search", "replay_tag", "replay_insights", "replay_diff",
            "replay_stats",
        }

    async def test_diff_tool(self, fresh_db):
        seed("a1")
        seed("b2")
        out = await server.dispatch_tool("replay_diff", {"session_a": "a1", "session_b": "b2"})
        assert "Compare a1" in out[0].text and "tool calls" in out[0].text

    async def test_diff_missing(self, fresh_db):
        out = await server.dispatch_tool("replay_diff", {"session_a": "x", "session_b": "y"})
        assert "not found" in out[0].text

    async def test_insights_tool(self, fresh_db):
        seed("srv-sess-1")
        out = await server.dispatch_tool("replay_insights", {"session_id": "srv-sess-1"})
        text = out[0].text
        assert "Insights for srv-sess" in text
        assert "Tool calls:" in text

    async def test_insights_unknown_session(self, fresh_db):
        out = await server.dispatch_tool("replay_insights", {"session_id": "ghost"})
        assert "no session" in out[0].text


# ── stdio transport ───────────────────────────────────────────────────────────

class TestStdioTransport:
    def test_run_stdio_is_coroutine(self):
        import inspect
        assert inspect.iscoroutinefunction(server.run_stdio)


# ── /api/search ───────────────────────────────────────────────────────────────

class TestApiSearch:
    def test_match(self, client):
        seed("s1")
        d = client.get("/api/search", params={"q": "Write"}).json()
        assert d["count"] >= 1
        assert any(r["session"]["id"] == "s1" for r in d["results"])

    def test_empty_query(self, client):
        d = client.get("/api/search", params={"q": ""}).json()
        assert d["count"] == 0
        assert d["results"] == []

    def test_summary_has_tags(self, client):
        seed("s1")
        store.set_tags("s1", ["release"])
        d = client.get("/api/search", params={"q": "release"}).json()
        assert d["results"][0]["session"]["tags"] == ["release"]


class TestApiSessionMetrics:
    def test_detail_includes_metrics(self, client):
        seed("s1")
        d = client.get("/api/session/s1").json()
        assert "metrics" in d
        assert d["metrics"]["tool_calls"] == 2  # seed writes 2 tool_result events
        assert "duration_human" in d["metrics"]


class TestApiDiff:
    def test_diff(self, client):
        seed("a1")
        seed("b2")
        d = client.get("/api/diff", params={"a": "a1", "b": "b2"}).json()
        assert d["a"]["session"]["id"] == "a1"
        assert "deltas" in d and "files" in d

    def test_diff_missing(self, client):
        r = client.get("/api/diff", params={"a": "x", "b": "y"})
        assert r.status_code == 404


class TestApiStats:
    def test_stats_rollup(self, client):
        seed("a1", project_dir="/proj/x")
        seed("b2", project_dir="/proj/y")
        d = client.get("/api/stats").json()
        assert d["session_count"] == 2
        assert d["total_tool_calls"] == 4  # 2 tool_result events per seed
        assert dict(d["tool_mix"])["Read"] == 2 and dict(d["tool_mix"])["Write"] == 2
        assert len(d["projects"]) == 2

    def test_stats_empty(self, client):
        assert client.get("/api/stats").json()["session_count"] == 0

    def test_stats_project_filter(self, client):
        seed("a1", project_dir="/proj/keep")
        seed("b2", project_dir="/proj/drop")
        d = client.get("/api/stats", params={"project": "keep"}).json()
        assert d["session_count"] == 1


class TestStatsTool:
    async def test_stats(self, fresh_db):
        seed("a1")
        out = await server.dispatch_tool("replay_stats", {})
        text = out[0].text
        assert "Analytics across 1 session" in text
        assert "Tool calls:" in text and "Why they end:" in text

    async def test_stats_empty(self, fresh_db):
        out = await server.dispatch_tool("replay_stats", {})
        assert "No sessions recorded yet." in out[0].text


# ── Helpers ───────────────────────────────────────────────────────────────────

class TestHelpers:
    def test_duration(self):
        assert server._duration("2026-05-30T12:00:00Z", "2026-05-30T13:30:45Z") == "01:30:45"

    def test_duration_missing(self):
        assert server._duration("2026-05-30T12:00:00Z", None) == "—"

    def test_format_uptime(self):
        assert server.format_uptime(3661) == "01:01:01"
