"""Tests for claude_replay.analytics — cross-session rollup."""

from __future__ import annotations

import claude_replay.analytics as analytics
import claude_replay.store as store


def _session(sid, project, tools, error_on=()):
    """Create a session with one tool_result event per name in `tools`;
    indices in `error_on` get an is_error result."""
    store.get_or_create_session(sid, project_dir=project, model="m", objective="o")
    for i, name in enumerate(tools):
        result = {"is_error": True} if i in error_on else {"ok": True}
        store.insert_event(sid, "tool_result", tool_name=name,
                           tool_input={"x": 1}, tool_result=result)


# ── Pure aggregate() ──────────────────────────────────────────────────────────

class TestAggregate:
    def test_empty_is_zeroed(self):
        r = analytics.aggregate([])
        assert r["session_count"] == 0
        assert r["total_tool_calls"] == 0
        assert r["death_causes"] == [] and r["projects"] == [] and r["by_day"] == []

    def test_totals_and_tool_mix(self, fresh_db):
        _session("a", r"C:\proj\alpha", ["Bash", "Bash", "Read"], error_on=(2,))
        _session("b", r"C:\proj\beta", ["Edit", "Write"])
        _session("c", r"C:\proj\alpha", ["Bash"])
        r = analytics.aggregate(store.sessions_with_events())

        assert r["session_count"] == 3
        assert r["total_tool_calls"] == 6
        mix = dict(r["tool_mix"])
        assert mix["Bash"] == 3 and mix["Read"] == 1 and mix["Edit"] == 1 and mix["Write"] == 1
        # one error out of six calls
        assert r["overall_error_rate"] == round(1 / 6, 3)
        assert r["avg_tool_calls"] == round(6 / 3, 1)

    def test_project_rollup_sorted_by_activity(self, fresh_db):
        _session("a", r"C:\proj\alpha", ["Bash", "Read"])
        _session("c", r"C:\proj\alpha", ["Bash"])
        _session("b", r"C:\proj\beta", ["Edit"])
        r = analytics.aggregate(store.sessions_with_events())

        projects = r["projects"]
        assert projects[0]["project"].endswith("alpha")
        assert projects[0]["sessions"] == 2
        assert projects[0]["tool_calls"] == 3
        beta = next(p for p in projects if p["project"].endswith("beta"))
        assert beta["sessions"] == 1

    def test_death_causes_partition_the_sessions(self, fresh_db):
        _session("a", "/p", ["Bash"])
        _session("b", "/p", ["Read"])
        r = analytics.aggregate(store.sessions_with_events())
        # every session contributes exactly one death label
        assert sum(c for _, c in r["death_causes"]) == r["session_count"] == 2

    def test_unknown_project_bucket(self, fresh_db):
        store.get_or_create_session("x", project_dir=None, model="m", objective="o")
        store.insert_event("x", "tool_result", tool_name="Bash", tool_input={}, tool_result={})
        r = analytics.aggregate(store.sessions_with_events())
        assert r["projects"][0]["project"] == "(unknown)"


class TestDay:
    def test_parses_zulu(self):
        assert analytics._day("2026-06-03T00:42:42Z") == "2026-06-03"

    def test_rejects_garbage(self):
        assert analytics._day("nonsense") is None
        assert analytics._day(None) is None
        assert analytics._day("2026/06/03") is None
