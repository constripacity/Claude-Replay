"""Tests for claude_replay.classify — death-cause classification.

Pure functions over a session row + event list, so these need no DB fixture.
"""

from __future__ import annotations

import json

import claude_replay.classify as classify


def _session(status="running", error_msg=None):
    return {"id": "s", "status": status, "error_msg": error_msg}


def _event(error_msg=None, tool_name=None, tool_result=None):
    return {"error_msg": error_msg, "tool_name": tool_name, "tool_result": tool_result}


# ── status-driven causes ──────────────────────────────────────────────────────

class TestStatusDriven:
    def test_completed_is_clean_finish(self):
        out = classify.classify(_session(status="completed"), [])
        assert out["cause"] == "clean_finish"
        assert out["label"] == "Clean finish"
        assert out["detail"] is None

    def test_running_with_no_events_never_started(self):
        out = classify.classify(_session(status="running"), [])
        assert out["cause"] == "never_started"

    def test_running_with_events_is_interrupted(self):
        out = classify.classify(_session(status="running"), [_event(tool_name="Read")])
        assert out["cause"] == "interrupted"

    def test_error_status_no_text_is_unknown(self):
        out = classify.classify(_session(status="error"), [])
        assert out["cause"] == "unknown"


# ── signature matching on explicit error text ─────────────────────────────────

class TestSignatures:
    def test_rate_limit(self):
        out = classify.classify(_session(error_msg="rate_limit_error (429)"), [])
        assert out["cause"] == "rate_limit"
        assert "429" in out["detail"]

    def test_context_overflow(self):
        out = classify.classify(_session(error_msg="prompt is too long: 250000 tokens"), [])
        assert out["cause"] == "context_overflow"

    def test_api_error_overloaded(self):
        out = classify.classify(_session(error_msg="overloaded_error (529)"), [])
        assert out["cause"] == "api_error"

    def test_unmatched_error_falls_back_to_api_error(self):
        out = classify.classify(_session(error_msg="something weird happened"), [])
        assert out["cause"] == "api_error"

    def test_event_error_msg_used_when_session_clean(self):
        events = [_event(error_msg="too many requests, slow down")]
        out = classify.classify(_session(status="running"), events)
        assert out["cause"] == "rate_limit"

    def test_explicit_error_wins_over_completed_status(self):
        out = classify.classify(_session(status="completed", error_msg="429 too many requests"), [])
        assert out["cause"] == "rate_limit"


# ── last_error helper ─────────────────────────────────────────────────────────

class TestLastError:
    def test_none_when_no_errors(self):
        assert classify.last_error([_event(tool_name="Read")]) is None

    def test_prefers_explicit_error_msg(self):
        events = [_event(error_msg="boom"), _event(tool_name="Read")]
        assert classify.last_error(events) == "boom"

    def test_detects_tool_result_error_marker(self):
        events = [_event(tool_name="Bash", tool_result=json.dumps({"is_error": True, "error": "exit 1"}))]
        assert classify.last_error(events) == "Bash: exit 1"

    def test_ignores_substring_noise_in_normal_output(self):
        # A normal tool result that merely mentions 'rate limit' must not count.
        events = [_event(tool_name="Bash", tool_result=json.dumps({"stdout": "the api rate limit is 60/min"}))]
        assert classify.last_error(events) is None

    def test_interrupted_surfaces_tool_error_as_detail(self):
        events = [_event(tool_name="Bash", tool_result=json.dumps({"is_error": True, "error": "exit 1"}))]
        out = classify.classify(_session(status="running"), events)
        assert out["cause"] == "interrupted"
        assert out["detail"] == "Bash: exit 1"
