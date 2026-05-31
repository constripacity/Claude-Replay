"""End-to-end integration test for Claude Replay.

Drives the *real hook entry point* (`hooks.dispatch`, which parses raw JSON and
routes to the handlers — the same path `claude-replay hook <type>` takes minus
the stdin read) through a full session lifecycle:

    pre-tool  →  post-tool × N (crossing the auto-checkpoint threshold)  →  stop

then runs the recovery surface — resume brief + HTML export — on that
hook-seeded session. The per-module suites cover the hooks, resume, and export
in isolation; this is the one test that proves the whole pipeline agrees on a
session built the way a live Claude Code session builds it.
"""

from __future__ import annotations

import json

import claude_replay.export as export
import claude_replay.hooks as hooks
import claude_replay.resume as resume
import claude_replay.store as store

SID = "integration-session-0001"
CWD = "/work/myproject"  # not a git repo → diff is gracefully skipped


def _pre(tool_name: str, file_path: str) -> str:
    return json.dumps({
        "session_id": SID,
        "cwd": CWD,
        "model": "claude-opus-4-8",
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
    })


def _post(tool_name: str, file_path: str, content: str = "ok") -> str:
    return json.dumps({
        "session_id": SID,
        "cwd": CWD,
        "tool_name": tool_name,
        "tool_input": {"file_path": file_path},
        "tool_response": {"content": content},
    })


def test_full_pipeline_hooks_to_resume_and_export(fresh_db, tmp_path, monkeypatch):
    # Live runtime may set these; the payload session_id must win deterministically.
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    # 1. A tool call opens the session.
    hooks.dispatch("PreToolUse", _pre("Read", f"{CWD}/app.py"))

    # 2. Ten completed tool calls — crosses the 10-result auto-checkpoint threshold.
    edits = ["app.py", "app.py", "util.py", "util.py", "README.md",
             "app.py", "test_app.py", "util.py", "app.py", "config.toml"]
    for i, f in enumerate(edits):
        tool = "Edit" if i % 2 else "Write"
        hooks.dispatch("PostToolUse", _post(tool, f"{CWD}/{f}", content=f"v{i}"))

    # 3. Session ends.
    hooks.dispatch("Stop", json.dumps({"session_id": SID, "cwd": CWD}))

    # ── The store reflects the whole lifecycle ──
    session = store.get_session(SID)
    assert session is not None
    assert session["status"] == "completed"
    assert session["ended_at"] is not None
    assert session["model"] == "claude-opus-4-8"
    assert session["project_dir"] == CWD

    # 1 tool_use + 10 tool_result + 1 stop
    assert store.count_events(SID) == 12
    assert store.count_events(SID, "tool_result") == 10

    # auto-checkpoint at the 10th result + the final checkpoint on stop
    assert store.count_checkpoints(SID) == 2

    # distinct files touched, in first-touch order (non-git → no diff, didn't crash)
    files = store.files_touched(SID)
    assert files == [
        f"{CWD}/app.py", f"{CWD}/util.py", f"{CWD}/README.md",
        f"{CWD}/test_app.py", f"{CWD}/config.toml",
    ]
    final_cp = store.list_checkpoints(SID)[-1]
    assert final_cp["diff_patch"] is None  # not a git repo

    # ── Resume brief generated from hook-seeded data ──
    brief = resume.generate_brief(SID)
    assert isinstance(brief, str) and brief.strip()
    assert "Files touched" in brief
    assert "app.py" in brief                 # a touched file is surfaced
    assert "claude-opus-4-8" in brief        # model carried through

    # ── HTML export of the same session ──
    out = export.render_html(SID, tmp_path)
    assert out.exists()
    assert SID[:8] in out.name               # filename keyed on the short id
    html = out.read_text(encoding="utf-8")
    assert SID[:8] in html
    assert "util.py" in html                 # a touched file shows up in the trace
    assert html.lstrip().lower().startswith("<!doctype html")


def test_cold_start_pipeline_still_resumes(fresh_db, tmp_path, monkeypatch):
    """Replay installed mid-session: the first hook seen is a PostToolUse, with
    no prior session row. The pipeline must still produce a resume + export."""
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    cold = "cold-start-session"
    raw = json.dumps({
        "session_id": cold,
        "cwd": CWD,
        "tool_name": "Edit",
        "tool_input": {"file_path": f"{CWD}/mid.py"},
        "tool_response": {"content": "patched"},
    })
    hooks.dispatch("PostToolUse", raw)
    hooks.dispatch("Stop", json.dumps({"session_id": cold, "cwd": CWD}))

    assert store.get_session(cold) is not None
    brief = resume.generate_brief(cold)
    assert brief.strip()
    assert "mid.py" in brief                 # the one touched file survives cold start
    out = export.render_html(cold, tmp_path)
    assert out.exists()
