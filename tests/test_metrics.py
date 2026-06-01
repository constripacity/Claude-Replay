"""Tests for claude_replay.metrics — pure per-session insight metrics."""

from __future__ import annotations

import json

import claude_replay.metrics as metrics


def _session(status="running", started=None, ended=None):
    return {"id": "s", "status": status, "error_msg": None,
            "started_at": started, "ended_at": ended}


def _evt(etype="tool_result", tool=None, tool_input=None, tool_result=None,
         error_msg=None, ts="2026-06-01T12:00:00Z"):
    return {
        "event_type": etype, "tool_name": tool,
        "tool_input": json.dumps(tool_input) if tool_input is not None else None,
        "tool_result": json.dumps(tool_result) if tool_result is not None else None,
        "error_msg": error_msg, "timestamp": ts,
    }


class TestCompute:
    def test_empty(self):
        m = metrics.compute(_session(), [])
        assert m["tool_calls"] == 0
        assert m["error_rate"] == 0.0
        assert m["files_touched"] == 0
        assert m["duration_seconds"] is None
        assert m["death_cause"] == "never_started"

    def test_counts_tool_calls_and_events(self):
        events = [_evt(tool="Read"), _evt(tool="Bash"), _evt("tool_use", tool="Read")]
        m = metrics.compute(_session(), events)
        assert m["tool_calls"] == 2  # only tool_result events
        assert m["event_count"] == 3

    def test_top_tools_frequency(self):
        events = [_evt(tool="Bash"), _evt(tool="Bash"), _evt(tool="Read")]
        m = metrics.compute(_session(), events)
        assert m["tool_frequency"] == {"Bash": 2, "Read": 1}
        assert m["top_tools"][0] == ("Bash", 2)

    def test_error_count_and_rate(self):
        events = [
            _evt(tool="Bash", tool_result={"is_error": True, "error": "boom"}),
            _evt(tool="Read", tool_result={"ok": True}),
            _evt(tool="Edit", error_msg="failed"),
        ]
        m = metrics.compute(_session(), events)
        assert m["error_count"] == 2
        assert m["error_rate"] == round(2 / 3, 3)

    def test_stderr_alone_is_not_an_error(self):
        # A normal command with incidental stderr must not count as an error.
        events = [_evt(tool="Bash", tool_result={"stdout": "x", "stderr": "warning"})]
        m = metrics.compute(_session(), events)
        assert m["error_count"] == 0

    def test_files_touched_distinct(self):
        events = [
            _evt(tool="Edit", tool_input={"file_path": "a.py"}),
            _evt(tool="Write", tool_input={"file_path": "a.py"}),
            _evt(tool="Write", tool_input={"file_path": "b.py"}),
            _evt(tool="Read", tool_input={"file_path": "c.py"}),  # Read isn't a FILE_TOOL
        ]
        m = metrics.compute(_session(), events)
        assert m["files_touched"] == 2

    def test_duration_from_session_timestamps(self):
        m = metrics.compute(
            _session(started="2026-06-01T12:00:00Z", ended="2026-06-01T12:01:30Z"), []
        )
        assert m["duration_seconds"] == 90
        assert m["duration_human"] == "1m30s"

    def test_duration_falls_back_to_last_event(self):
        events = [_evt(ts="2026-06-01T12:00:10Z")]
        m = metrics.compute(_session(started="2026-06-01T12:00:00Z"), events)
        assert m["duration_seconds"] == 10
        assert m["duration_human"] == "10s"

    def test_death_cause_composed(self):
        m = metrics.compute(_session(status="error", ended="2026-06-01T12:00:01Z"),
                            [_evt(error_msg="rate limit 429")])
        assert m["death_cause"] == "rate_limit"
        assert m["death_label"] == "Rate limited"


class TestHumanDuration:
    def test_formats(self):
        assert metrics._human_duration(None) == "—"
        assert metrics._human_duration(5) == "5s"
        assert metrics._human_duration(90) == "1m30s"
        assert metrics._human_duration(3661) == "1h01m"
