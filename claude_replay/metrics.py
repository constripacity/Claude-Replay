"""
Claude Replay — per-session insight metrics.

Turns a session's event log into health/insight numbers Claude Code doesn't
surface natively (it shows only a Failed/Completed state icon). This is the
analytics half of Replay's "observability layer on top of resume/rewind".

Pure functions over a session row + its event list — NO DB access here (same
contract as classify.py), so it's trivially testable and safe to call from the
CLI, server, and TUI alike. Composes classify.classify() for the death cause.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any

from . import classify
from .store import FILE_TOOLS


def compute(session: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Return a flat dict of per-session metrics. All values derive from the
    recorded events — no network, no token-count guessing."""
    tool_results = [e for e in events if e.get("event_type") == "tool_result"]
    freq = Counter(e["tool_name"] for e in tool_results if e.get("tool_name"))
    errors = sum(1 for e in events if classify.event_is_error(e))
    files = _files_touched(events)
    duration = _duration_seconds(session, events)
    death = classify.classify(session, events)

    return {
        "tool_calls": len(tool_results),
        "event_count": len(events),
        "error_count": errors,
        "error_rate": round(errors / len(tool_results), 3) if tool_results else 0.0,
        "files_touched": len(files),
        "top_tools": freq.most_common(5),  # [(name, count), …]
        "tool_frequency": dict(freq),
        "duration_seconds": duration,
        "duration_human": _human_duration(duration),
        "death_cause": death["cause"],
        "death_label": death["label"],
    }


# ── internals ─────────────────────────────────────────────────────────────────

def _files_touched(events: list[dict[str, Any]]) -> set[str]:
    """Distinct file paths modified, derived purely from the event list — mirrors
    store.files_touched but without a DB round-trip."""
    import json

    files: set[str] = set()
    for event in events:
        if event.get("tool_name") not in FILE_TOOLS or not event.get("tool_input"):
            continue
        try:
            data = json.loads(event["tool_input"])
        except (ValueError, TypeError):
            continue
        if isinstance(data, dict):
            path = data.get("file_path") or data.get("notebook_path")
            if path:
                files.add(path)
    return files


def _duration_seconds(session: dict[str, Any], events: list[dict[str, Any]]) -> int | None:
    start = _parse(session.get("started_at"))
    end = _parse(session.get("ended_at"))
    if end is None and events:
        end = _parse(events[-1].get("timestamp"))
    if start is None or end is None:
        return None
    return max(0, int((end - start).total_seconds()))


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        # Stored as e.g. 2026-06-01T13:03:16Z; 3.10's fromisoformat needs +00:00.
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _human_duration(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 60:
        return f"{seconds}s"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{sec:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"
