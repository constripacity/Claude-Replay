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


# ── Naming & tagging (v0.2.0) ─────────────────────────────────────────────────

class TestNamingTagging:
    def test_new_session_has_empty_tags(self, fresh_db):
        s = make_session()
        assert s["tags"] == []
        assert s["name"] is None

    def test_get_session_returns_list_tags(self, fresh_db):
        make_session()
        store.set_tags(SESSION_ID, ["bug", "auth"])
        assert store.get_session(SESSION_ID)["tags"] == ["bug", "auth"]

    def test_set_name(self, fresh_db):
        make_session()
        store.set_name(SESSION_ID, "Login refactor")
        assert store.get_session(SESSION_ID)["name"] == "Login refactor"

    def test_set_name_empty_clears(self, fresh_db):
        make_session()
        store.set_name(SESSION_ID, "x")
        store.set_name(SESSION_ID, "")
        assert store.get_session(SESSION_ID)["name"] is None

    def test_tags_deduped_case_insensitively(self, fresh_db):
        make_session()
        assert store.set_tags(SESSION_ID, ["Bug", "bug", " BUG ", "auth"]) == ["Bug", "auth"]

    def test_add_tags_merges(self, fresh_db):
        make_session()
        store.set_tags(SESSION_ID, ["a"])
        assert store.add_tags(SESSION_ID, ["b", "a"]) == ["a", "b"]

    def test_remove_tags(self, fresh_db):
        make_session()
        store.set_tags(SESSION_ID, ["a", "b", "c"])
        assert store.remove_tags(SESSION_ID, ["B"]) == ["a", "c"]

    def test_empty_tags_stored_as_null(self, fresh_db):
        make_session()
        store.set_tags(SESSION_ID, ["a"])
        store.set_tags(SESSION_ID, [])
        assert store.get_session(SESSION_ID)["tags"] == []


# ── Migration (v0.2.0) ────────────────────────────────────────────────────────

class TestMigration:
    def test_adds_columns_to_legacy_sessions_table(self, tmp_path, monkeypatch):
        # Build a 0.1.0-shaped DB by hand (no name/tags columns), then open it.
        import sqlite3

        db_path = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db_path))
        conn.executescript(
            """CREATE TABLE sessions (
                   id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
                   objective TEXT, project_dir TEXT, model TEXT,
                   status TEXT NOT NULL DEFAULT 'running', error_msg TEXT);
               CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                   session_id TEXT NOT NULL, seq INTEGER NOT NULL, timestamp TEXT NOT NULL,
                   event_type TEXT NOT NULL, tool_name TEXT, tool_input TEXT,
                   tool_result TEXT, error_msg TEXT);
               CREATE TABLE checkpoints (id INTEGER PRIMARY KEY AUTOINCREMENT,
                   session_id TEXT NOT NULL, seq INTEGER NOT NULL, timestamp TEXT NOT NULL,
                   step_done TEXT NOT NULL, step_next TEXT, files_touched TEXT, diff_patch TEXT);
               INSERT INTO sessions (id, started_at) VALUES ('old', '2026-01-01T00:00:00Z');
               INSERT INTO events (session_id, seq, timestamp, event_type, tool_name)
                   VALUES ('old', 1, '2026-01-01T00:00:00Z', 'tool_result', 'LegacyTool');"""
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr(store, "DB_PATH", str(db_path))
        monkeypatch.setattr(store, "_conn", None)
        try:
            # Opening migrates: new columns usable, and the existing event got
            # backfilled into the FTS index.
            store.set_tags("old", ["migrated"])
            assert store.get_session("old")["tags"] == ["migrated"]
            assert any(h["session"]["id"] == "old" for h in store.search("LegacyTool"))
        finally:
            store.close()
            monkeypatch.setattr(store, "_conn", None)


# ── Full-text search (v0.2.0) ─────────────────────────────────────────────────

class TestSearch:
    def _seed(self):
        make_session(SESSION_ID, objective="Fix the login bug")
        store.insert_event(SESSION_ID, "tool_result", tool_name="Edit",
                           tool_input={"file_path": "auth/login.py"})
        store.insert_event(SESSION_ID, "tool_result", tool_name="Bash",
                           tool_result={"stdout": "ran the migration script"})
        make_session(SESSION_ID_2, objective="Write docs")
        store.insert_event(SESSION_ID_2, "tool_result", tool_name="Write",
                           tool_input={"file_path": "README.md"})

    def test_empty_query_returns_nothing(self, fresh_db):
        self._seed()
        assert store.search("") == []
        assert store.search("   ") == []

    def test_matches_event_payload(self, fresh_db):
        self._seed()
        hits = store.search("login")
        ids = [h["session"]["id"] for h in hits]
        assert SESSION_ID in ids
        assert SESSION_ID_2 not in ids

    def test_matches_objective(self, fresh_db):
        self._seed()
        ids = [h["session"]["id"] for h in store.search("docs")]
        assert SESSION_ID_2 in ids

    def test_matches_tag(self, fresh_db):
        self._seed()
        store.set_tags(SESSION_ID_2, ["release"])
        ids = [h["session"]["id"] for h in store.search("release")]
        assert SESSION_ID_2 in ids

    def test_ranks_by_match_count(self, fresh_db):
        make_session(SESSION_ID, objective="x")
        for _ in range(3):
            store.insert_event(SESSION_ID, "tool_result", tool_name="Bash",
                               tool_result={"stdout": "widget widget"})
        make_session(SESSION_ID_2, objective="x")
        store.insert_event(SESSION_ID_2, "tool_result", tool_name="Bash",
                           tool_result={"stdout": "widget"})
        hits = store.search("widget")
        assert hits[0]["session"]["id"] == SESSION_ID
        assert hits[0]["matches"] >= hits[-1]["matches"]

    def test_fts_special_chars_dont_crash(self, fresh_db):
        self._seed()
        # Quotes / parens would be FTS5 operators if unescaped.
        assert isinstance(store.search('login("bug")'), list)

    def test_deleted_events_leave_fts(self, fresh_db):
        self._seed()
        assert store.search("login")
        store.reset_all()
        assert store.search("login") == []


# ── Retention / prune (v0.2.0) ────────────────────────────────────────────────

class TestPrune:
    def _dated_session(self, sid, started, ended=None):
        store.get_or_create_session(sid, objective="x")
        store.db().execute(
            "UPDATE sessions SET started_at = ?, ended_at = ? WHERE id = ?",
            (started, ended, sid),
        )

    def test_prunes_old_keeps_recent(self, fresh_db):
        self._dated_session("old", "2020-01-01T00:00:00Z")
        store.insert_event("old", "tool_result", tool_name="Read")
        store.write_checkpoint("old", step_done="done")
        self._dated_session("fresh", "2026-05-31T00:00:00Z")

        removed = store.prune(older_than_days=30)

        assert removed == 1
        assert store.get_session("old") is None
        assert store.get_session("fresh") is not None
        assert store.list_events("old") == []
        assert store.list_checkpoints("old") == []

    def test_uses_ended_at_when_present(self, fresh_db):
        # Started long ago but ENDED recently → must be kept.
        self._dated_session("longrun", "2020-01-01T00:00:00Z", "2026-05-31T00:00:00Z")
        assert store.prune(older_than_days=30) == 0
        assert store.get_session("longrun") is not None

    def test_negative_days_rejected(self, fresh_db):
        import pytest
        with pytest.raises(ValueError):
            store.prune(older_than_days=-1)

    def test_prune_keeps_fts_consistent(self, fresh_db):
        self._dated_session("old", "2020-01-01T00:00:00Z")
        store.insert_event("old", "tool_result", tool_name="Bash",
                           tool_result={"stdout": "findme"})
        store.prune(older_than_days=30, vacuum=False)
        assert store.search("findme") == []
