"""Tests for claude_replay.resume — the markdown resume brief."""

from __future__ import annotations

import claude_replay.resume as resume
import claude_replay.store as store


SID = "resume-test-001"


def seed_full(sid=SID):
    """A session with events + a checkpoint carrying step_next + files + diff."""
    store.get_or_create_session(
        sid, project_dir="/proj", model="claude-opus-4-8", objective="Build the store"
    )
    for _ in range(3):
        store.insert_event(sid, "tool_use", tool_name="Read")
        store.insert_event(sid, "tool_result", tool_name="Read")
    store.insert_event(sid, "tool_result", tool_name="Write", tool_input={"file_path": "store.py"})
    store.write_checkpoint(
        sid,
        "Wrote the store module",
        step_next="Write the hook handlers",
        files_touched=["store.py", "tests/test_store.py"],
        diff_patch="--- a/store.py\n+++ b/store.py\n@@ -1 +1 @@\n-old\n+new",
    )
    return sid


# ── Missing / empty ───────────────────────────────────────────────────────────

class TestMissing:
    def test_missing_session(self, fresh_db):
        out = resume.generate_brief("nonexistent")
        assert "No session found" in out

    def test_empty_session(self, fresh_db):
        store.get_or_create_session(SID, objective="Just started")
        out = resume.generate_brief(SID)
        assert resume.HEADER in out
        assert "Just started" in out
        assert "no tool activity" in out.lower()


# ── Full session with checkpoint ──────────────────────────────────────────────

class TestFullBrief:
    def test_has_header_and_footer(self, fresh_db):
        seed_full()
        out = resume.generate_brief(SID)
        assert out.startswith(resume.HEADER)
        assert out.rstrip().endswith(resume.FOOTER)

    def test_objective(self, fresh_db):
        seed_full()
        assert "**Original objective:** Build the store" in resume.generate_brief(SID)

    def test_model_line(self, fresh_db):
        seed_full()
        assert "claude-opus-4-8" in resume.generate_brief(SID)

    def test_counts(self, fresh_db):
        seed_full()
        out = resume.generate_brief(SID)
        assert "**Checkpoints recorded:** 1" in out
        assert "**Events recorded:** 7" in out

    def test_work_completed(self, fresh_db):
        seed_full()
        out = resume.generate_brief(SID)
        assert "### Work completed (last checkpoint)" in out
        assert "Wrote the store module" in out

    def test_pending_work(self, fresh_db):
        seed_full()
        out = resume.generate_brief(SID)
        assert "### Pending work" in out
        assert "Write the hook handlers" in out

    def test_files_touched(self, fresh_db):
        seed_full()
        out = resume.generate_brief(SID)
        assert "### Files touched this session" in out
        assert "- store.py  (modified)" in out
        assert "tests/test_store.py" in out

    def test_diff_block(self, fresh_db):
        seed_full()
        out = resume.generate_brief(SID)
        assert "### Diff since last checkpoint" in out
        assert "```diff" in out
        assert "+new" in out

    def test_interrupted_label_when_running(self, fresh_db):
        seed_full()
        # status defaults to 'running' → label should be "Interrupted"
        assert "**Interrupted:**" in resume.generate_brief(SID)


# ── No-checkpoint path ────────────────────────────────────────────────────────

class TestNoCheckpoint:
    def test_synthesizes_from_events(self, fresh_db):
        store.get_or_create_session(SID, objective="Died early")
        for _ in range(4):
            store.insert_event(SID, "tool_result", tool_name="Bash")
        out = resume.generate_brief(SID)
        assert "4 tool calls recorded" in out
        assert "no checkpoint" in out.lower()

    def test_pending_default(self, fresh_db):
        store.get_or_create_session(SID, objective="x")
        store.insert_event(SID, "tool_result", tool_name="Read")
        out = resume.generate_brief(SID)
        assert "(none recorded" in out

    def test_files_from_events_without_checkpoint(self, fresh_db):
        store.get_or_create_session(SID, objective="x")
        store.insert_event(SID, "tool_result", tool_name="Edit", tool_input={"file_path": "a.py"})
        out = resume.generate_brief(SID)
        assert "- a.py  (modified)" in out

    def test_no_diff_section_without_checkpoint(self, fresh_db):
        store.get_or_create_session(SID, objective="x")
        store.insert_event(SID, "tool_result", tool_name="Read")
        out = resume.generate_brief(SID)
        assert "### Diff" not in out


# ── Status variants ───────────────────────────────────────────────────────────

class TestStatusVariants:
    def test_completed_label(self, fresh_db):
        store.get_or_create_session(SID, objective="x")
        store.update_session(SID, status="completed", ended_at="2026-05-30T12:00:00Z")
        out = resume.generate_brief(SID)
        assert "**Ended:** 2026-05-30T12:00:00Z" in out

    def test_error_shown(self, fresh_db):
        store.get_or_create_session(SID, objective="x")
        store.update_session(SID, status="error", error_msg="overloaded_error (529)")
        out = resume.generate_brief(SID)
        assert "**Error:** overloaded_error (529)" in out

    def test_unknown_model(self, fresh_db):
        store.get_or_create_session(SID, objective="x")
        assert "(unknown)" in resume.generate_brief(SID)

    def test_ended_falls_back_to_last_event(self, fresh_db):
        store.get_or_create_session(SID, objective="x")
        store.insert_event(SID, "tool_result", tool_name="Read")
        events = store.list_events(SID)
        out = resume.generate_brief(SID)
        assert events[-1]["timestamp"] in out
