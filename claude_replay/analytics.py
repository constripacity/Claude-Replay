"""
Claude Replay — cross-session analytics.

Where `metrics.py` answers "what happened in *this* session", this answers
"what's true across *all* my sessions" — the rollup native Claude Code doesn't
do at the individual-developer level: which projects you spend tool calls in,
your overall error rate, the tool mix, the day-by-day trend, and — the launch
question — *why your sessions actually end*.

Pure: `aggregate()` takes already-loaded (session, events) pairs and returns a
plain dict. No DB, no network. The CLI / API / MCP layers load the data and
feed it in; the renderer lives in the CLI (mirrors `metrics.py`).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from . import metrics


def aggregate(items: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> dict[str, Any]:
    """Roll up a list of (session, events) pairs into cross-session stats.

    Returns a flat, JSON-serialisable dict. Empty input yields a zeroed rollup
    (never raises), so callers can render "nothing recorded yet" uniformly.
    """
    n = len(items)
    if n == 0:
        return _empty()

    total_tool_calls = 0
    total_errors = 0
    total_events = 0
    durations: list[int] = []
    causes: Counter[str] = Counter()
    tool_mix: Counter[str] = Counter()
    projects: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, int]] = {}

    for session, events in items:
        m = metrics.compute(session, events)
        total_tool_calls += m["tool_calls"]
        total_errors += m["error_count"]
        total_events += m["event_count"]
        if m["duration_seconds"] is not None:
            durations.append(m["duration_seconds"])
        causes[m["death_label"]] += 1
        for tool, count in m["tool_frequency"].items():
            tool_mix[tool] += count

        proj = session.get("project_dir") or "(unknown)"
        pa = projects.setdefault(
            proj, {"sessions": 0, "tool_calls": 0, "errors": 0, "causes": Counter()})
        pa["sessions"] += 1
        pa["tool_calls"] += m["tool_calls"]
        pa["errors"] += m["error_count"]
        pa["causes"][m["death_label"]] += 1

        day = _day(session.get("started_at"))
        if day:
            da = by_day.setdefault(day, {"sessions": 0, "tool_calls": 0, "errors": 0})
            da["sessions"] += 1
            da["tool_calls"] += m["tool_calls"]
            da["errors"] += m["error_count"]

    project_rows = sorted(
        (
            {
                "project": proj,
                "sessions": a["sessions"],
                "tool_calls": a["tool_calls"],
                "error_rate": _rate(a["errors"], a["tool_calls"]),
                "top_cause": a["causes"].most_common(1)[0][0] if a["causes"] else "—",
            }
            for proj, a in projects.items()
        ),
        key=lambda r: (r["sessions"], r["tool_calls"]),
        reverse=True,
    )

    day_rows = [
        {"day": day, "sessions": a["sessions"], "error_rate": _rate(a["errors"], a["tool_calls"])}
        for day, a in sorted(by_day.items())
    ]

    return {
        "session_count": n,
        "total_tool_calls": total_tool_calls,
        "total_events": total_events,
        "overall_error_rate": _rate(total_errors, total_tool_calls),
        "avg_tool_calls": round(total_tool_calls / n, 1),
        "avg_duration_seconds": int(sum(durations) / len(durations)) if durations else None,
        "death_causes": causes.most_common(),       # [(label, count), …] desc
        "tool_mix": tool_mix.most_common(10),        # [(tool, count), …] desc
        "projects": project_rows,                    # busiest first
        "by_day": day_rows,                          # chronological
    }


def _empty() -> dict[str, Any]:
    return {
        "session_count": 0,
        "total_tool_calls": 0,
        "total_events": 0,
        "overall_error_rate": 0.0,
        "avg_tool_calls": 0.0,
        "avg_duration_seconds": None,
        "death_causes": [],
        "tool_mix": [],
        "projects": [],
        "by_day": [],
    }


def _rate(errors: int, tool_calls: int) -> float:
    return round(errors / tool_calls, 3) if tool_calls else 0.0


def _day(started: str | None) -> str | None:
    """First 10 chars of an ISO-Zulu timestamp = YYYY-MM-DD. None if missing."""
    if not started or len(started) < 10:
        return None
    day = started[:10]
    return day if day[4] == "-" and day[7] == "-" else None
