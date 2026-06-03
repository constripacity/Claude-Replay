"""
Claude Replay — `doctor` self-check.

Answers the one question that matters after install: *is Replay actually
recording?* The most common silent failure is hooks written to settings.json
that reference a `claude-replay` command Claude Code can't find on PATH — the
hooks then fail quietly (they swallow errors and exit 0 by design) and nothing
is ever recorded, with no signal to the user.

`evaluate()` is pure (takes facts, returns checks) so it's unit-testable; the
CLI gathers the real facts (settings, PATH, DB, session count) and feeds it in.
"""

from __future__ import annotations

from typing import Any, NamedTuple


class Check(NamedTuple):
    label: str
    status: str  # ok | warn | fail | info
    detail: str
    hint: str | None = None


_ICON = {"ok": "✓", "warn": "!", "fail": "✗", "info": "·"}


def evaluate(
    *,
    hooks_installed: bool,
    command_on_path: str | None,
    db_path: str,
    db_exists: bool,
    session_count: int,
    last_session_age_hours: float | None,
) -> dict[str, Any]:
    """Turn the gathered facts into an ordered list of checks + an overall
    verdict. Pure — no I/O."""
    checks: list[Check] = []

    # 1. Are the three hooks in settings.json?
    if hooks_installed:
        checks.append(Check(
            "Hooks installed", "ok",
            "PreToolUse / PostToolUse / Stop are in settings.json"))
    else:
        checks.append(Check(
            "Hooks installed", "fail",
            "Replay's hooks are not in settings.json",
            "run: claude-replay install"))

    # 2. Can Claude Code actually run the hook command? (the silent-failure guard)
    if command_on_path:
        checks.append(Check(
            "Hook command on PATH", "ok",
            f"claude-replay → {command_on_path}"))
    else:
        checks.append(Check(
            "Hook command on PATH", "warn",
            "'claude-replay' is not on PATH — Claude Code can't run the hooks, so "
            "they fail silently and nothing is recorded",
            "put the install dir on PATH (or `pip install --user claude-replay`), "
            "then re-run doctor"))

    # 3. Does the database exist yet?
    if db_exists:
        checks.append(Check("Database", "ok", db_path))
    else:
        checks.append(Check(
            "Database", "info",
            f"not created yet — {db_path}",
            "it is created automatically on the first recorded event"))

    # 4. Is anything actually being recorded? (the real proof)
    if session_count > 0:
        recency = ""
        if last_session_age_hours is not None:
            recency = f"; most recent {_age(last_session_age_hours)} ago"
        checks.append(Check(
            "Sessions recorded", "ok",
            f"{session_count} session{'s' if session_count != 1 else ''}{recency}"))
    elif hooks_installed and command_on_path:
        checks.append(Check(
            "Sessions recorded", "warn",
            "hooks are installed and runnable, but 0 sessions recorded yet",
            "use Claude Code normally (start or finish a session), then re-run doctor"))
    else:
        checks.append(Check(
            "Sessions recorded", "info",
            "0 sessions recorded",
            "fix the items above, then use Claude Code normally"))

    has_fail = any(c.status == "fail" for c in checks)
    has_warn = any(c.status == "warn" for c in checks)
    return {
        "checks": checks,
        "ok": not has_fail,              # nothing strictly broken
        "healthy": not has_fail and not has_warn,  # installed AND recording
    }


def _age(hours: float) -> str:
    if hours < 1:
        return f"{max(1, int(hours * 60))}m"
    if hours < 48:
        return f"{int(hours)}h"
    return f"{int(hours / 24)}d"


def render(result: dict[str, Any]) -> str:
    lines = ["Claude Replay — doctor", ""]
    for c in result["checks"]:
        lines.append(f"  {_ICON[c.status]} {c.label}: {c.detail}")
        if c.hint and c.status in ("warn", "fail"):
            lines.append(f"      → {c.hint}")
    lines.append("")
    if result["healthy"]:
        lines.append("All good — Replay is installed and recording. ✓")
    elif result["ok"]:
        lines.append("Installed, but see the warning(s) above — recording may not be happening.")
    else:
        lines.append("Not fully set up — address the ✗ item(s) above.")
    return "\n".join(lines)
