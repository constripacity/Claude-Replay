"""Tests for claude_replay.export — self-contained HTML trace rendering."""

from __future__ import annotations

import pytest

import claude_replay.export as export
import claude_replay.store as store


SID = "export-test-001"


def seed(sid=SID):
    store.get_or_create_session(
        sid, project_dir="/proj", model="claude-opus-4-8", objective="Export me"
    )
    store.insert_event(sid, "tool_use", tool_name="Read", tool_input={"file_path": "a.py"})
    store.insert_event(sid, "tool_result", tool_name="Read", tool_result={"content": "x = 1"})
    store.insert_event(sid, "tool_result", tool_name="Write", tool_input={"file_path": "b.py"})
    store.write_checkpoint(
        sid, "Did work", step_next="More work",
        files_touched=["b.py"], diff_patch="--- a/b.py\n+++ b/b.py\n+added",
    )
    return sid


# ── Basic render ──────────────────────────────────────────────────────────────

class TestRender:
    def test_creates_file(self, fresh_db, tmp_path):
        seed()
        path = export.render_html(SID, tmp_path)
        assert path.exists()

    def test_filename_pattern(self, fresh_db, tmp_path):
        seed()
        store.update_session(SID, ended_at="2026-05-30T12:00:00Z")
        # started_at date drives the filename
        path = export.render_html(SID, tmp_path)
        assert path.name.startswith("claude-replay-export-t-")
        assert path.name.endswith(".html")

    def test_creates_output_dir(self, fresh_db, tmp_path):
        seed()
        nested = tmp_path / "a" / "b"
        path = export.render_html(SID, nested)
        assert path.exists()

    def test_missing_session_raises(self, fresh_db, tmp_path):
        with pytest.raises(ValueError):
            export.render_html("nope", tmp_path)


# ── Self-contained ────────────────────────────────────────────────────────────

class TestSelfContained:
    def test_no_external_urls(self, fresh_db, tmp_path):
        seed()
        html = export.render_html(SID, tmp_path).read_text(encoding="utf-8")
        assert "http://" not in html
        assert "https://" not in html

    def test_no_cdn_references(self, fresh_db, tmp_path):
        seed()
        html = export.render_html(SID, tmp_path).read_text(encoding="utf-8").lower()
        assert "cdn" not in html
        assert "googleapis" not in html
        assert "unpkg" not in html

    def test_inline_style_present(self, fresh_db, tmp_path):
        seed()
        html = export.render_html(SID, tmp_path).read_text(encoding="utf-8")
        assert "<style>" in html

    def test_no_external_script_src(self, fresh_db, tmp_path):
        seed()
        html = export.render_html(SID, tmp_path).read_text(encoding="utf-8")
        assert "src=" not in html  # no external scripts/images


# ── Content sections ──────────────────────────────────────────────────────────

class TestContent:
    def test_valid_html_skeleton(self, fresh_db, tmp_path):
        seed()
        html = export.render_html(SID, tmp_path).read_text(encoding="utf-8")
        assert html.startswith("<!DOCTYPE html>")
        assert "</html>" in html.strip()

    def test_objective_present(self, fresh_db, tmp_path):
        seed()
        html = export.render_html(SID, tmp_path).read_text(encoding="utf-8")
        assert "Export me" in html

    def test_timeline_section(self, fresh_db, tmp_path):
        seed()
        html = export.render_html(SID, tmp_path).read_text(encoding="utf-8")
        assert "Timeline" in html
        assert "Read" in html
        assert "Write" in html

    def test_checkpoint_in_timeline(self, fresh_db, tmp_path):
        seed()
        html = export.render_html(SID, tmp_path).read_text(encoding="utf-8")
        assert "checkpoint" in html
        assert "Did work" in html

    def test_files_section(self, fresh_db, tmp_path):
        seed()
        html = export.render_html(SID, tmp_path).read_text(encoding="utf-8")
        assert "Files touched" in html
        assert "b.py" in html

    def test_diff_present(self, fresh_db, tmp_path):
        seed()
        html = export.render_html(SID, tmp_path).read_text(encoding="utf-8")
        assert "+added" in html

    def test_resume_brief_embedded(self, fresh_db, tmp_path):
        seed()
        html = export.render_html(SID, tmp_path).read_text(encoding="utf-8")
        assert 'id="resume-brief"' in html
        assert "Resume Brief" in html

    def test_status_badge(self, fresh_db, tmp_path):
        seed()
        store.update_session(SID, status="completed", ended_at="2026-05-30T12:00:00Z")
        html = export.render_html(SID, tmp_path).read_text(encoding="utf-8")
        assert "status-completed" in html


# ── HTML escaping ─────────────────────────────────────────────────────────────

class TestEscaping:
    def test_escapes_html_in_content(self, fresh_db, tmp_path):
        store.get_or_create_session(SID, objective="<script>alert(1)</script>")
        store.insert_event(SID, "tool_use", tool_name="Read")
        html = export.render_html(SID, tmp_path).read_text(encoding="utf-8")
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


# ── Date helper ───────────────────────────────────────────────────────────────

class TestDateStr:
    def test_parses_iso(self):
        assert export._date_str("2026-05-30T12:00:00Z") == "20260530"

    def test_handles_none(self):
        assert export._date_str(None) == "00000000"

    def test_handles_garbage(self):
        assert export._date_str("not-a-date") == "00000000"
