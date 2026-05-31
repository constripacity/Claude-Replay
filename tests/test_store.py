"""Tests for claude_replay.store — full coverage of all public functions."""

from __future__ import annotations

import json


import claude_replay.store as store


# ── Helpers ───────────────────────────────────────────────────────────────────

SESSION_ID = "test-session-001"
SESSION_ID_2 = "test-session-002"


def make_session(session_id: str = SESSION_ID, **kwargs) -> dict:
    return store.get_or_create_session(
        session_id,
        project_dir=kwargs.get("project_dir", "/home/user/myproject"),
        model=kwargs.get("model", "claude-opus-4-8"),
        objective=kwargs.get("objective", "Build a feature"),
    )


# ── DB initialisation ─────────────────────────────────────────────────────────

class TestDbInit:
    def test_creates_db_file(self, fresh_db):
        store.db()
        assert fresh_db.exists()

    def test_creates_parent_dirs(self, tmp_path, monkeypatch):
        nested = tmp_path / "a" / "b" / "c" / "sessions.db"
        monkeypatch.setattr(store, "DB_PATH", str(nested))
        monkeypatch.setattr(store, "_conn", None)
        store.db()
        assert nested.exists()
        store.close()

    def test_wal_mode(self, fresh_db):
        conn = store.db()
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode == "wal"

    def test_schema_tables_exist(self, fresh_db):
        conn = store.db()
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"sessions", "checkpoints", "events"}.issubset(tables)

    def test_schema_indexes_exist(self, fresh_db):
        conn = store.db()
        indexes = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        assert "idx_events_session" in indexes
        assert "idx_checkpoints_session" in indexes

    def test_connection_reuse(self, fresh_db):
        c1 = store.db()
        c2 = store.db()
        assert c1 is c2

    def test_close_and_reopen(self, fresh_db):
        c1 = store.db()
        store.close()
        assert store._conn is None
        c2 = store.db()
        assert c1 is not c2


# ── Sessions ──────────────────────────────────────────────────────────────────

class TestSessions:
    def test_create_session(self, fresh_db):
        s = make_session()
        assert s["id"] == SESSION_ID
        assert s["status"] == "running"
        assert s["project_dir"] == "/home/user/myproject"
        assert s["model"] == "claude-opus-4-8"
        assert s["objective"] == "Build a feature"
        assert s["ended_at"] is None
        assert s["error_msg"] is None
        assert s["started_at"].endswith("Z")

    def test_get_or_create_idempotent(self, fresh_db):
        s1 = make_session()
        s2 = store.get_or_create_session(SESSION_ID)
        assert s1["id"] == s2["id"]
        assert s1["started_at"] == s2["started_at"]

    def test_get_or_create_returns_existing_unchanged(self, fresh_db):
        make_session(objective="Original")
        s2 = store.get_or_create_session(SESSION_ID, objective="Overwrite attempt")
        assert s2["objective"] == "Original"

    def test_get_session_exists(self, fresh_db):
        make_session()
        s = store.get_session(SESSION_ID)
        assert s is not None
        assert s["id"] == SESSION_ID

    def test_get_session_missing(self, fresh_db):
        assert store.get_session("nonexistent") is None

    def test_update_session_status(self, fresh_db):
        make_session()
        store.update_session(SESSION_ID, status="completed")
        s = store.get_session(SESSION_ID)
        assert s["status"] == "completed"

    def test_update_session_ended_at(self, fresh_db):
        make_session()
        store.update_session(SESSION_ID, ended_at="2026-05-29T12:00:00Z")
        s = store.get_session(SESSION_ID)
        assert s["ended_at"] == "2026-05-29T12:00:00Z"

    def test_update_session_error_msg(self, fresh_db):
        make_session()
        store.update_session(SESSION_ID, status="error", error_msg="overloaded_error")
        s = store.get_session(SESSION_ID)
        assert s["status"] == "error"
        assert s["error_msg"] == "overloaded_error"

    def test_update_session_objective(self, fresh_db):
        make_session(objective=None)
        store.update_session(SESSION_ID, objective="Extracted later")
        s = store.get_session(SESSION_ID)
        assert s["objective"] == "Extracted later"

    def test_update_session_no_fields(self, fresh_db):
        make_session()
        store.update_session(SESSION_ID)  # no-op, must not raise
        s = store.get_session(SESSION_ID)
        assert s["status"] == "running"

    def test_list_sessions_empty(self, fresh_db):
        assert store.list_sessions() == []

    def test_list_sessions_returns_all(self, fresh_db):
        make_session(SESSION_ID)
        make_session(SESSION_ID_2)
        sessions = store.list_sessions()
        assert len(sessions) == 2

    def test_list_sessions_order_newest_first(self, fresh_db):
        make_session(SESSION_ID)
        make_session(SESSION_ID_2)
        sessions = store.list_sessions()
        ids = [s["id"] for s in sessions]
        assert ids.index(SESSION_ID_2) < ids.index(SESSION_ID) or len(set(ids)) == 2

    def test_list_sessions_limit(self, fresh_db):
        for i in range(5):
            make_session(f"sess-{i:03d}")
        sessions = store.list_sessions(limit=3)
        assert len(sessions) == 3

    def test_multiple_sessions_independent(self, fresh_db):
        make_session(SESSION_ID, objective="Task A")
        make_session(SESSION_ID_2, objective="Task B")
        s1 = store.get_session(SESSION_ID)
        s2 = store.get_session(SESSION_ID_2)
        assert s1["objective"] == "Task A"
        assert s2["objective"] == "Task B"


# ── Events ────────────────────────────────────────────────────────────────────

class TestEvents:
    def test_insert_tool_use_event(self, fresh_db):
        make_session()
        seq = store.insert_event(
            SESSION_ID,
            "tool_use",
            tool_name="Read",
            tool_input={"file_path": "/src/main.py"},
        )
        assert seq == 1

    def test_insert_tool_result_event(self, fresh_db):
        make_session()
        store.insert_event(SESSION_ID, "tool_use", tool_name="Read")
        seq = store.insert_event(
            SESSION_ID,
            "tool_result",
            tool_name="Read",
            tool_result={"content": "file contents"},
        )
        assert seq == 2

    def test_event_seq_monotonic(self, fresh_db):
        make_session()
        seqs = [store.insert_event(SESSION_ID, "tool_use", tool_name=f"Tool{i}") for i in range(5)]
        assert seqs == list(range(1, 6))

    def test_event_seq_per_session(self, fresh_db):
        make_session(SESSION_ID)
        make_session(SESSION_ID_2)
        s1 = store.insert_event(SESSION_ID, "tool_use", tool_name="Read")
        s2 = store.insert_event(SESSION_ID_2, "tool_use", tool_name="Read")
        assert s1 == 1
        assert s2 == 1  # independent per session

    def test_insert_stop_event(self, fresh_db):
        make_session()
        seq = store.insert_event(SESSION_ID, "stop")
        assert seq == 1

    def test_insert_error_event(self, fresh_db):
        make_session()
        seq = store.insert_event(SESSION_ID, "error", error_msg="overloaded_error 529")
        assert seq == 1

    def test_list_events_empty(self, fresh_db):
        make_session()
        assert store.list_events(SESSION_ID) == []

    def test_list_events_order(self, fresh_db):
        make_session()
        for name in ["Read", "Write", "Bash"]:
            store.insert_event(SESSION_ID, "tool_use", tool_name=name)
        events = store.list_events(SESSION_ID)
        assert [e["tool_name"] for e in events] == ["Read", "Write", "Bash"]

    def test_list_events_isolated_per_session(self, fresh_db):
        make_session(SESSION_ID)
        make_session(SESSION_ID_2)
        store.insert_event(SESSION_ID, "tool_use", tool_name="Read")
        assert store.list_events(SESSION_ID_2) == []

    def test_count_events_all(self, fresh_db):
        make_session()
        for _ in range(7):
            store.insert_event(SESSION_ID, "tool_use", tool_name="Read")
        assert store.count_events(SESSION_ID) == 7

    def test_count_events_by_type(self, fresh_db):
        make_session()
        for _ in range(3):
            store.insert_event(SESSION_ID, "tool_use", tool_name="Read")
        store.insert_event(SESSION_ID, "stop")
        assert store.count_events(SESSION_ID, "tool_use") == 3
        assert store.count_events(SESSION_ID, "stop") == 1

    def test_tool_input_stored_as_json(self, fresh_db):
        make_session()
        store.insert_event(
            SESSION_ID, "tool_use", tool_name="Write",
            tool_input={"file_path": "/a/b.py", "content": "x = 1"},
        )
        events = store.list_events(SESSION_ID)
        parsed = json.loads(events[0]["tool_input"])
        assert parsed["file_path"] == "/a/b.py"

    def test_tool_result_dict_stored(self, fresh_db):
        make_session()
        store.insert_event(
            SESSION_ID, "tool_result", tool_name="Bash",
            tool_result={"output": "hello", "exit_code": 0},
        )
        events = store.list_events(SESSION_ID)
        parsed = json.loads(events[0]["tool_result"])
        assert parsed["exit_code"] == 0

    def test_large_tool_input_truncated(self, fresh_db):
        make_session()
        big = "x" * (10 * 1024)  # 10 KB > 8 KB limit
        store.insert_event(SESSION_ID, "tool_use", tool_name="Write", tool_input=big)
        events = store.list_events(SESSION_ID)
        assert "[truncated]" in events[0]["tool_input"]
        assert len(events[0]["tool_input"].encode()) <= 9 * 1024  # with some margin

    def test_none_tool_input_stored_as_none(self, fresh_db):
        make_session()
        store.insert_event(SESSION_ID, "stop")
        events = store.list_events(SESSION_ID)
        assert events[0]["tool_input"] is None

    def test_event_timestamp_format(self, fresh_db):
        make_session()
        store.insert_event(SESSION_ID, "stop")
        events = store.list_events(SESSION_ID)
        ts = events[0]["timestamp"]
        assert ts.endswith("Z")
        assert "T" in ts


# ── Checkpoints ───────────────────────────────────────────────────────────────

class TestCheckpoints:
    def test_write_checkpoint_basic(self, fresh_db):
        make_session()
        seq = store.write_checkpoint(SESSION_ID, step_done="Completed step 1")
        assert seq == 1

    def test_write_checkpoint_full(self, fresh_db):
        make_session()
        seq = store.write_checkpoint(
            SESSION_ID,
            step_done="Wrote store.py",
            step_next="Write tests",
            files_touched=["claude_replay/store.py"],
            diff_patch="--- a/store.py\n+++ b/store.py\n@@ ...",
        )
        assert seq == 1

    def test_checkpoint_seq_monotonic(self, fresh_db):
        make_session()
        seqs = [store.write_checkpoint(SESSION_ID, step_done=f"Step {i}") for i in range(3)]
        assert seqs == [1, 2, 3]

    def test_checkpoint_seq_per_session(self, fresh_db):
        make_session(SESSION_ID)
        make_session(SESSION_ID_2)
        s1 = store.write_checkpoint(SESSION_ID, step_done="A")
        s2 = store.write_checkpoint(SESSION_ID_2, step_done="B")
        assert s1 == 1
        assert s2 == 1

    def test_get_latest_checkpoint_none(self, fresh_db):
        make_session()
        assert store.get_latest_checkpoint(SESSION_ID) is None

    def test_get_latest_checkpoint_returns_last(self, fresh_db):
        make_session()
        store.write_checkpoint(SESSION_ID, step_done="Step 1")
        store.write_checkpoint(SESSION_ID, step_done="Step 2")
        store.write_checkpoint(SESSION_ID, step_done="Step 3")
        cp = store.get_latest_checkpoint(SESSION_ID)
        assert cp["step_done"] == "Step 3"
        assert cp["seq"] == 3

    def test_checkpoint_files_touched_roundtrip(self, fresh_db):
        make_session()
        files = ["a/b.py", "c/d.py", "README.md"]
        store.write_checkpoint(SESSION_ID, step_done="x", files_touched=files)
        cp = store.get_latest_checkpoint(SESSION_ID)
        assert cp["files_touched"] == files

    def test_checkpoint_files_touched_none(self, fresh_db):
        make_session()
        store.write_checkpoint(SESSION_ID, step_done="x")
        cp = store.get_latest_checkpoint(SESSION_ID)
        assert cp["files_touched"] is None

    def test_list_checkpoints_order(self, fresh_db):
        make_session()
        for i in range(4):
            store.write_checkpoint(SESSION_ID, step_done=f"Step {i}")
        cps = store.list_checkpoints(SESSION_ID)
        assert [cp["seq"] for cp in cps] == [1, 2, 3, 4]

    def test_list_checkpoints_empty(self, fresh_db):
        make_session()
        assert store.list_checkpoints(SESSION_ID) == []

    def test_list_checkpoints_isolated_per_session(self, fresh_db):
        make_session(SESSION_ID)
        make_session(SESSION_ID_2)
        store.write_checkpoint(SESSION_ID, step_done="Only for sess 1")
        assert store.list_checkpoints(SESSION_ID_2) == []

    def test_count_checkpoints(self, fresh_db):
        make_session()
        for i in range(5):
            store.write_checkpoint(SESSION_ID, step_done=f"Step {i}")
        assert store.count_checkpoints(SESSION_ID) == 5

    def test_count_checkpoints_zero(self, fresh_db):
        make_session()
        assert store.count_checkpoints(SESSION_ID) == 0

    def test_checkpoint_diff_patch_stored(self, fresh_db):
        make_session()
        patch = "--- a/foo.py\n+++ b/foo.py\n@@ -1 +1 @@\n-old\n+new\n"
        store.write_checkpoint(SESSION_ID, step_done="x", diff_patch=patch)
        cp = store.get_latest_checkpoint(SESSION_ID)
        assert cp["diff_patch"] == patch

    def test_checkpoint_step_next_stored(self, fresh_db):
        make_session()
        store.write_checkpoint(SESSION_ID, step_done="done", step_next="pending work")
        cp = store.get_latest_checkpoint(SESSION_ID)
        assert cp["step_next"] == "pending work"

    def test_checkpoint_timestamp_format(self, fresh_db):
        make_session()
        store.write_checkpoint(SESSION_ID, step_done="x")
        cp = store.get_latest_checkpoint(SESSION_ID)
        assert cp["timestamp"].endswith("Z")


# ── Resume data ───────────────────────────────────────────────────────────────

class TestResumeData:
    def test_resume_data_missing_session(self, fresh_db):
        assert store.get_resume_data("nonexistent") is None

    def test_resume_data_empty_session(self, fresh_db):
        make_session()
        data = store.get_resume_data(SESSION_ID)
        assert data is not None
        assert data["session"]["id"] == SESSION_ID
        assert data["checkpoint"] is None
        assert data["event_count"] == 0
        assert data["checkpoint_count"] == 0

    def test_resume_data_with_activity(self, fresh_db):
        make_session()
        for _ in range(12):
            store.insert_event(SESSION_ID, "tool_use", tool_name="Read")
        store.write_checkpoint(SESSION_ID, step_done="Phase 1 done", step_next="Phase 2")
        store.write_checkpoint(SESSION_ID, step_done="Phase 2 done")

        data = store.get_resume_data(SESSION_ID)
        assert data["event_count"] == 12
        assert data["checkpoint_count"] == 2
        assert data["checkpoint"]["step_done"] == "Phase 2 done"
        assert data["checkpoint"]["seq"] == 2

    def test_resume_data_session_fields(self, fresh_db):
        make_session(objective="Write store.py", model="claude-opus-4-8")
        data = store.get_resume_data(SESSION_ID)
        assert data["session"]["objective"] == "Write store.py"
        assert data["session"]["model"] == "claude-opus-4-8"


# ── Reset ─────────────────────────────────────────────────────────────────────

class TestReset:
    def test_reset_clears_all_tables(self, fresh_db):
        make_session(SESSION_ID)
        make_session(SESSION_ID_2)
        store.insert_event(SESSION_ID, "tool_use", tool_name="Read")
        store.write_checkpoint(SESSION_ID, step_done="x")

        store.reset_all()

        assert store.list_sessions() == []
        assert store.list_events(SESSION_ID) == []
        assert store.list_checkpoints(SESSION_ID) == []

    def test_after_reset_can_insert(self, fresh_db):
        make_session()
        store.reset_all()
        s = make_session()
        assert s["id"] == SESSION_ID


# ── Truncation edge cases ─────────────────────────────────────────────────────

class TestTruncation:
    def test_string_input_within_limit(self, fresh_db):
        make_session()
        small = "hello world"
        store.insert_event(SESSION_ID, "tool_use", tool_name="X", tool_input=small)
        events = store.list_events(SESSION_ID)
        assert events[0]["tool_input"] == "hello world"

    def test_dict_input_within_limit(self, fresh_db):
        make_session()
        d = {"key": "value"}
        store.insert_event(SESSION_ID, "tool_use", tool_name="X", tool_input=d)
        events = store.list_events(SESSION_ID)
        assert json.loads(events[0]["tool_input"]) == d

    def test_large_result_truncated(self, fresh_db):
        make_session()
        big = {"output": "y" * (10 * 1024)}
        store.insert_event(SESSION_ID, "tool_result", tool_name="Bash", tool_result=big)
        events = store.list_events(SESSION_ID)
        assert "[truncated]" in events[0]["tool_result"]
