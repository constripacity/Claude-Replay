"""
Claude Replay — resume brief generator.

Turns a recorded session into a markdown briefing you paste into a fresh
Claude Code session to continue exactly where the dead one left off.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from . import classify, store

HEADER = "## Claude Replay — Session Resume Brief"
FOOTER = 'Resume from "Pending work" above. All file state is current on disk.'


def generate_brief(session_id: str) -> str:
    data = store.get_resume_data(session_id)
    if data is None:
        return f"No session found with id: {session_id}"

    session = data["session"]
    checkpoint = data["checkpoint"]
    events = store.list_events(session_id)

    lines: list[str] = [HEADER, ""]

    objective = session["objective"] or "(not recorded)"
    lines.append(f"**Original objective:** {objective}")

    started = session["started_at"]
    status = session["status"]
    ended = session["ended_at"]
    if not ended and events:
        ended = events[-1]["timestamp"]
    end_label = "Ended" if status == "completed" else "Interrupted"
    lines.append(f"**Session started:** {started}  |  **{end_label}:** {ended or '—'}")

    model = session["model"] or "(unknown)"
    model_line = f"**Model at interruption:** {model}"
    if session["error_msg"]:
        model_line += f"  |  **Error:** {session['error_msg']}"
    lines.append(model_line)

    # Why the session ended — clean finish vs rate limit vs context overflow, etc.
    death = classify.classify(session, events)
    death_line = f"**How it ended:** {death['label']}"
    if death["detail"]:
        death_line += f"  |  **Last error:** {death['detail']}"
    lines.append(death_line)

    lines.append(
        f"**Checkpoints recorded:** {data['checkpoint_count']}  |  "
        f"**Events recorded:** {data['event_count']}"
    )
    lines.append("")

    # Work completed
    lines.append("### Work completed (last checkpoint)")
    lines.append(checkpoint["step_done"] if checkpoint else _synthesize_completed(events))
    lines.append("")

    # Pending work
    lines.append("### Pending work")
    if checkpoint and checkpoint["step_next"]:
        lines.append(checkpoint["step_next"])
    else:
        lines.append("(none recorded — resume from the last completed step above)")
    lines.append("")

    # Files touched
    lines.append("### Files touched this session")
    files = _files_for(checkpoint, session_id)
    if files:
        lines.extend(f"- {path}  (modified)" for path in files)
    else:
        lines.append("(none recorded)")
    lines.append("")

    # Diff
    diff = checkpoint["diff_patch"] if checkpoint else None
    if diff:
        lines.append("### Diff since last checkpoint")
        lines.append("```diff")
        lines.append(diff)
        lines.append("```")
        lines.append("")

    # Live repository state — current branch + what's uncommitted RIGHT NOW in
    # the project dir. Computed at brief time (not death time) so the restart
    # reflects the tree you're actually resuming into.
    repo = _git_context(session["project_dir"])
    if repo:
        lines.append("### Repository state (live)")
        lines.append(f"**Branch:** {repo['branch']}")
        if repo["uncommitted"]:
            lines.append("**Uncommitted changes:**")
            lines.extend(f"- {entry}" for entry in repo["uncommitted"])
        else:
            lines.append("**Uncommitted changes:** (clean working tree)")
        lines.append("")

    lines.append("---")
    lines.append(FOOTER)
    return "\n".join(lines)


def _git_context(project_dir: str | None) -> dict[str, Any] | None:
    """Current branch + porcelain status for `project_dir`. Best-effort and
    offline: returns None if it's not a git repo, git is missing, or anything
    fails — the brief never depends on git being present."""
    if not project_dir or not (Path(project_dir) / ".git").exists():
        return None
    try:
        branch = _git(project_dir, "rev-parse", "--abbrev-ref", "HEAD")
        status = _git(project_dir, "status", "--porcelain")
    except Exception:
        return None
    if branch is None:
        return None
    uncommitted = [line.strip() for line in (status or "").splitlines() if line.strip()]
    return {"branch": branch.strip() or "(detached)", "uncommitted": uncommitted[:50]}


def _git(project_dir: str, *args: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", project_dir, *args],
        capture_output=True,
        text=True,
        timeout=5,
    )
    return result.stdout if result.returncode == 0 else None


def _files_for(checkpoint: dict[str, Any] | None, session_id: str) -> list[str]:
    if checkpoint and checkpoint.get("files_touched"):
        return checkpoint["files_touched"]
    return store.files_touched(session_id)


def _synthesize_completed(events: list[dict[str, Any]]) -> str:
    """No checkpoint yet (session died before the first auto-checkpoint at 10
    tool calls) — summarize from the raw event stream instead."""
    tool_calls = [e for e in events if e["event_type"] == "tool_result"]
    if not tool_calls:
        return "(no tool activity recorded before the session ended)"
    recent = [e["tool_name"] for e in tool_calls[-10:] if e["tool_name"]]
    recent_str = ", ".join(recent) if recent else "—"
    return (
        f"{len(tool_calls)} tool calls recorded (no checkpoint written). "
        f"Recent: {recent_str}."
    )
