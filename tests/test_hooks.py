"""Tests for claude_replay.hooks — handlers, identity, auto-checkpoint, robustness."""

from __future__ import annotations

import json


import claude_replay.hooks as hooks
import claude_replay.store as store


SID = "hook-test-session"


def pre_payload(**kw):
    p = {"session_id": SID, "cwd": "/proj", "tool_name": "Read", "tool_input": {"file_path": "/proj/a.py"}}
    p.update(kw)
    return p


def post_payload(**kw):
    p = {
        "session_id": SID,
        "cwd": "/proj",
        "tool_name": "Read",
        "tool_input": {"file_path": "/proj/a.py"},
        "tool_response": {"content": "x = 1"},
    }
    p.update(kw)
    return p


# ── Session identity ──────────────────────────────────────────────────────────

class TestIdentity:
    def test_payload_session_id_wins(self, fresh_db, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "from-env")
        assert hooks.resolve_session_id({"session_id": "from-payload"}) == "from-payload"

    def test_env_claude_session_id(self, fresh_db, monkeypatch):
        monkeypatch.setenv("CLAUDE_SESSION_ID", "env-sid")
        assert hooks.resolve_session_id({}) == "env-sid"

    def test_env_claude_code_session_id(self, fresh_db, monkeypatch):
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "code-sid")
        assert hooks.resolve_session_id({}) == "code-sid"

    def test_fallback_hash_stable(self, fresh_db, monkeypatch):
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        a = hooks.resolve_session_id({"cwd": "/some/dir"})
        b = hooks.resolve_session_id({"cwd": "/some/dir"})
        assert a == b
        assert a.startswith("fallback-")

    def test_fallback_hash_differs_by_dir(self, fresh_db, monkeypatch):
        monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
        a = hooks.resolve_session_id({"cwd": "/dir/one"})
        b = hooks.resolve_session_id({"cwd": "/dir/two"})
        assert a != b


# ── pre_tool ──────────────────────────────────────────────────────────────────

class TestPreTool:
    def test_creates_session(self, fresh_db):
        hooks.pre_tool(pre_payload())
        s = store.get_session(SID)
        assert s is not None
        assert s["project_dir"] == "/proj"
        assert s["status"] == "running"

    def test_records_tool_use_event(self, fresh_db):
        hooks.pre_tool(pre_payload())
        events = store.list_events(SID)
        assert len(events) == 1
        assert events[0]["event_type"] == "tool_use"
        assert events[0]["tool_name"] == "Read"

    def test_tool_input_stored(self, fresh_db):
        hooks.pre_tool(pre_payload())
        events = store.list_events(SID)
        assert json.loads(events[0]["tool_input"])["file_path"] == "/proj/a.py"

    def test_idempotent_session_across_calls(self, fresh_db):
        hooks.pre_tool(pre_payload())
        started = store.get_session(SID)["started_at"]
        hooks.pre_tool(pre_payload(tool_name="Write"))
        assert store.get_session(SID)["started_at"] == started
        assert store.count_events(SID) == 2

    def test_model_recorded(self, fresh_db):
        hooks.pre_tool(pre_payload(model="claude-opus-4-8"))
        assert store.get_session(SID)["model"] == "claude-opus-4-8"


# ── post_tool ─────────────────────────────────────────────────────────────────

class TestPostTool:
    def test_records_tool_result(self, fresh_db):
        hooks.post_tool(post_payload())
        events = store.list_events(SID)
        assert events[-1]["event_type"] == "tool_result"
        assert json.loads(events[-1]["tool_result"])["content"] == "x = 1"

    def test_creates_session_if_cold(self, fresh_db):
        # Replay installed mid-session: first event seen is a PostToolUse
        hooks.post_tool(post_payload())
        assert store.get_session(SID) is not None

    def test_no_checkpoint_before_threshold(self, fresh_db):
        for _ in range(9):
            hooks.post_tool(post_payload())
        assert store.count_checkpoints(SID) == 0

    def test_auto_checkpoint_at_ten(self, fresh_db):
        for _ in range(10):
            hooks.post_tool(post_payload())
        assert store.count_checkpoints(SID) == 1

    def test_auto_checkpoint_every_ten(self, fresh_db):
        for _ in range(20):
            hooks.post_tool(post_payload())
        assert store.count_checkpoints(SID) == 2

    def test_checkpoint_has_summary(self, fresh_db):
        for _ in range(10):
            hooks.post_tool(post_payload())
        cp = store.get_latest_checkpoint(SID)
        assert "tool calls" in cp["step_done"]
        assert "Read" in cp["step_done"]

    def test_checkpoint_collects_files(self, fresh_db):
        for i in range(9):
            hooks.post_tool(post_payload())
        hooks.post_tool(post_payload(tool_name="Write", tool_input={"file_path": "/proj/out.py"}))
        cp = store.get_latest_checkpoint(SID)
        assert "/proj/out.py" in (cp["files_touched"] or [])

    def test_no_diff_for_non_git_dir(self, fresh_db):
        for _ in range(10):
            hooks.post_tool(post_payload())
        cp = store.get_latest_checkpoint(SID)
        assert cp["diff_patch"] is None


# ── stop ──────────────────────────────────────────────────────────────────────

class TestStop:
    def test_records_stop_event(self, fresh_db):
        hooks.pre_tool(pre_payload())
        hooks.stop({"session_id": SID, "cwd": "/proj"})
        assert store.list_events(SID)[-1]["event_type"] == "stop"

    def test_sets_completed_status(self, fresh_db):
        hooks.pre_tool(pre_payload())
        hooks.stop({"session_id": SID, "cwd": "/proj"})
        s = store.get_session(SID)
        assert s["status"] == "completed"
        assert s["ended_at"] is not None

    def test_writes_final_checkpoint(self, fresh_db):
        hooks.pre_tool(pre_payload())
        hooks.post_tool(post_payload())
        hooks.stop({"session_id": SID, "cwd": "/proj"})
        cp = store.get_latest_checkpoint(SID)
        assert cp is not None
        assert "Session ended" in cp["step_done"]

    def test_stop_cold_session(self, fresh_db):
        hooks.stop({"session_id": SID, "cwd": "/proj"})
        s = store.get_session(SID)
        assert s["status"] == "completed"


# ── handle / dispatch ─────────────────────────────────────────────────────────

class TestHandle:
    def test_handle_pre_tool_cli_spelling(self, fresh_db):
        assert hooks.handle("pre-tool", pre_payload()) == SID

    def test_handle_claude_code_spelling(self, fresh_db):
        assert hooks.handle("PreToolUse", pre_payload()) == SID

    def test_handle_post_tool(self, fresh_db):
        hooks.handle("post-tool", post_payload())
        assert store.list_events(SID)[-1]["event_type"] == "tool_result"

    def test_handle_stop(self, fresh_db):
        hooks.handle("pre-tool", pre_payload())
        hooks.handle("stop", {"session_id": SID})
        assert store.get_session(SID)["status"] == "completed"

    def test_handle_unknown_type(self, fresh_db):
        assert hooks.handle("bogus", {}) is None


class TestDispatch:
    def test_dispatch_valid_json(self, fresh_db):
        assert hooks.dispatch("pre-tool", json.dumps(pre_payload())) == SID
        assert store.count_events(SID) == 1

    def test_dispatch_invalid_json_no_raise(self, fresh_db):
        # Invalid JSON is treated as an empty payload — must never raise.
        # (Identity then resolves via env/fallback, same as an empty payload.)
        result = hooks.dispatch("pre-tool", "{not valid json")
        assert result is not None or result is None  # the point: no exception

    def test_dispatch_empty_string(self, fresh_db):
        # Empty payload → fallback session id, but no crash
        result = hooks.dispatch("pre-tool", "")
        assert result is not None  # fallback session created

    def test_dispatch_non_dict_json(self, fresh_db):
        assert hooks.dispatch("pre-tool", "[1, 2, 3]") is not None  # coerced to {}

    def test_dispatch_swallows_handler_error(self, fresh_db, monkeypatch):
        def boom(payload):
            raise RuntimeError("kaboom")
        monkeypatch.setattr(hooks, "pre_tool", boom)
        # Must not raise
        assert hooks.dispatch("pre-tool", json.dumps(pre_payload())) is None


# ── run (stdin) ───────────────────────────────────────────────────────────────

class TestRun:
    def test_run_reads_stdin(self, fresh_db, monkeypatch):
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(pre_payload())))
        rc = hooks.run("pre-tool")
        assert rc == 0
        assert store.count_events(SID) == 1

    def test_run_always_zero_on_bad_input(self, fresh_db, monkeypatch):
        import io
        monkeypatch.setattr("sys.stdin", io.StringIO("garbage{"))
        assert hooks.run("pre-tool") == 0


# ── Objective extraction ──────────────────────────────────────────────────────

class TestObjective:
    def test_extract_from_transcript(self, fresh_db, tmp_path):
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": "Build the store module"}}) + "\n",
            encoding="utf-8",
        )
        hooks.pre_tool(pre_payload(transcript_path=str(transcript)))
        assert store.get_session(SID)["objective"] == "Build the store module"

    def test_extract_from_content_list(self, fresh_db, tmp_path):
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            json.dumps({"type": "user", "message": {"role": "user",
                "content": [{"type": "text", "text": "Fix the bug"}]}}) + "\n",
            encoding="utf-8",
        )
        assert hooks._extract_objective(str(transcript)) == "Fix the bug"

    def test_extract_missing_file(self, fresh_db):
        assert hooks._extract_objective("/nonexistent/path.jsonl") is None

    def test_extract_none_path(self, fresh_db):
        assert hooks._extract_objective(None) is None

    def test_extract_truncates_long(self, fresh_db, tmp_path):
        transcript = tmp_path / "t.jsonl"
        long_text = "y" * 500
        transcript.write_text(
            json.dumps({"type": "user", "message": {"role": "user", "content": long_text}}) + "\n",
            encoding="utf-8",
        )
        assert len(hooks._extract_objective(str(transcript))) == 200

    def test_extract_skips_non_user(self, fresh_db, tmp_path):
        transcript = tmp_path / "t.jsonl"
        transcript.write_text(
            json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "hi"}}) + "\n"
            + json.dumps({"type": "user", "message": {"role": "user", "content": "the real objective"}}) + "\n",
            encoding="utf-8",
        )
        assert hooks._extract_objective(str(transcript)) == "the real objective"


# ── Files collection ──────────────────────────────────────────────────────────

class TestFilesCollection:
    def test_collects_from_edit_tools(self, fresh_db):
        store.get_or_create_session(SID, project_dir="/proj")
        store.insert_event(SID, "tool_result", tool_name="Write", tool_input={"file_path": "/a.py"})
        store.insert_event(SID, "tool_result", tool_name="Edit", tool_input={"file_path": "/b.py"})
        store.insert_event(SID, "tool_result", tool_name="Read", tool_input={"file_path": "/c.py"})
        files = hooks._collect_files_touched(SID)
        assert "/a.py" in files
        assert "/b.py" in files
        assert "/c.py" not in files  # Read is not a file-touch

    def test_dedupes_files(self, fresh_db):
        store.get_or_create_session(SID, project_dir="/proj")
        for _ in range(3):
            store.insert_event(SID, "tool_result", tool_name="Edit", tool_input={"file_path": "/same.py"})
        assert hooks._collect_files_touched(SID) == ["/same.py"]

    def test_notebook_path(self, fresh_db):
        store.get_or_create_session(SID, project_dir="/proj")
        store.insert_event(SID, "tool_result", tool_name="NotebookEdit", tool_input={"notebook_path": "/nb.ipynb"})
        assert "/nb.ipynb" in hooks._collect_files_touched(SID)
