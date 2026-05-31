"""
Claude Replay — hook handlers.

Dispatched by `claude-replay hook <type>`. Each handler reads a Claude Code
hook payload (JSON on stdin) and writes to the SQLite store via store.py.

Hard constraints (see CLAUDE.md):
- Completes in <50ms — no blocking I/O except the SQLite write
- No external network calls — offline-first, always
- Never breaks the agent: every failure path swallows and exits 0

Hook payload shapes (stdin JSON from Claude Code):
    PreToolUse:  { session_id?, cwd?, transcript_path?, tool_name, tool_input }
    PostToolUse: { session_id?, cwd?, tool_name, tool_input, tool_response }
    Stop:        { session_id?, cwd? }
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from . import store

# Auto-write a checkpoint every N PostToolUse events.
CHECKPOINT_EVERY = 10

# Cap a stored diff so a huge working tree can't bloat the DB.
_MAX_DIFF_BYTES = 64 * 1024


# ── Session identity ──────────────────────────────────────────────────────────

def resolve_session_id(payload: dict[str, Any]) -> str:
    """Resolve a stable session ID from the hook payload.

    1. payload['session_id']            (modern Claude Code always sets this)
    2. CLAUDE_SESSION_ID env var
    3. CLAUDE_CODE_SESSION_ID env var
    4. fallback hash of the project dir  (last resort — groups a dir's activity)
    """
    sid = payload.get("session_id")
    if sid:
        return str(sid)
    for var in ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID"):
        value = os.environ.get(var)
        if value:
            return value
    project_dir = payload.get("cwd") or os.getcwd()
    digest = hashlib.sha1(project_dir.encode("utf-8", "replace")).hexdigest()[:12]
    return f"fallback-{digest}"


def _ensure_session(payload: dict[str, Any]) -> str:
    """Return the session ID, creating the session row on first sighting.

    Objective extraction reads the transcript — only done once, when the
    session is first created, to stay off the hot path.
    """
    sid = resolve_session_id(payload)
    if store.get_session(sid) is None:
        objective = _extract_objective(payload.get("transcript_path"))
        store.get_or_create_session(
            sid,
            project_dir=payload.get("cwd") or os.getcwd(),
            model=payload.get("model"),
            objective=objective,
        )
    return sid


# ── Handlers ──────────────────────────────────────────────────────────────────

def pre_tool(payload: dict[str, Any]) -> str:
    sid = _ensure_session(payload)
    store.insert_event(
        sid,
        "tool_use",
        tool_name=payload.get("tool_name"),
        tool_input=payload.get("tool_input"),
    )
    return sid


def post_tool(payload: dict[str, Any]) -> str:
    sid = _ensure_session(payload)
    store.insert_event(
        sid,
        "tool_result",
        tool_name=payload.get("tool_name"),
        tool_input=payload.get("tool_input"),
        tool_result=payload.get("tool_response"),
    )
    n = store.count_events(sid, "tool_result")
    if n and n % CHECKPOINT_EVERY == 0:
        _write_auto_checkpoint(sid, payload)
    return sid


def stop(payload: dict[str, Any]) -> str:
    sid = _ensure_session(payload)
    store.insert_event(sid, "stop")
    _write_final_checkpoint(sid, payload)
    store.update_session(sid, status="completed", ended_at=store._now())
    return sid


def handle(hook_type: str, payload: dict[str, Any]) -> str | None:
    """Route a hook type to its handler. Accepts CLI ('pre-tool') or Claude
    Code ('PreToolUse') spellings."""
    ht = hook_type.replace("_", "-").lower()
    if ht in ("pre-tool", "pretooluse"):
        return pre_tool(payload)
    if ht in ("post-tool", "posttooluse"):
        return post_tool(payload)
    if ht == "stop":
        return stop(payload)
    return None


# ── Checkpoint construction ───────────────────────────────────────────────────

def _write_auto_checkpoint(sid: str, payload: dict[str, Any]) -> None:
    store.write_checkpoint(
        sid,
        _summarize(sid, final=False),
        step_next=None,
        files_touched=_collect_files_touched(sid) or None,
        diff_patch=_compute_diff(payload.get("cwd")),
    )


def _write_final_checkpoint(sid: str, payload: dict[str, Any]) -> None:
    store.write_checkpoint(
        sid,
        _summarize(sid, final=True),
        step_next=None,
        files_touched=_collect_files_touched(sid) or None,
        diff_patch=_compute_diff(payload.get("cwd")),
    )


def _summarize(sid: str, *, final: bool) -> str:
    events = store.list_events(sid)
    tool_calls = [e for e in events if e["event_type"] == "tool_result"]
    recent = [e["tool_name"] for e in tool_calls[-10:] if e["tool_name"]]
    recent_str = ", ".join(recent) if recent else "—"
    prefix = "Session ended" if final else f"Auto-checkpoint at {len(tool_calls)} tool calls"
    return f"{prefix}. {len(tool_calls)} tool calls total. Recent: {recent_str}."


def _collect_files_touched(sid: str) -> list[str]:
    return store.files_touched(sid)


def _compute_diff(project_dir: str | None) -> str | None:
    """Best-effort working-tree diff. None if not a git repo, git is missing,
    or anything goes wrong — a diff is never required."""
    if not project_dir:
        return None
    if not (Path(project_dir) / ".git").exists():
        return None
    try:
        import subprocess

        result = subprocess.run(
            ["git", "-C", project_dir, "diff", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            return None
        out = result.stdout
        if not out.strip():
            return None
        if len(out.encode()) > _MAX_DIFF_BYTES:
            out = out.encode()[:_MAX_DIFF_BYTES].decode(errors="replace") + "\n…[diff truncated]"
        return out
    except Exception:
        return None


# ── Objective extraction ──────────────────────────────────────────────────────

def _extract_objective(transcript_path: str | None) -> str | None:
    """Pull the first user message out of a Claude Code transcript (JSONL).
    Best-effort: any failure returns None."""
    if not transcript_path:
        return None
    try:
        path = Path(transcript_path)
        if not path.exists():
            return None
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                message = entry.get("message")
                role = message.get("role") if isinstance(message, dict) else None
                if entry.get("type") == "user" or role == "user":
                    content = message.get("content") if isinstance(message, dict) else None
                    text = _content_text(content)
                    if text:
                        return text[:200]
        return None
    except Exception:
        return None


def _content_text(content: Any) -> str | None:
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        joined = " ".join(p for p in parts if p).strip()
        return joined or None
    return None


# ── Entry points ──────────────────────────────────────────────────────────────

def dispatch(hook_type: str, raw: str) -> str | None:
    """Parse a raw stdin payload and route it. Swallows everything — a hook
    must never break the agent it's recording."""
    try:
        payload = json.loads(raw) if raw and raw.strip() else {}
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        return handle(hook_type, payload)
    except Exception:
        return None


def run(hook_type: str) -> int:
    """Read the hook payload from stdin and dispatch. Always returns 0."""
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    dispatch(hook_type, raw)
    return 0
