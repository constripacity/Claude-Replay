"""Tests for claude_replay.doctor — the install/recording self-check."""

from __future__ import annotations

import claude_replay.doctor as doctor
import claude_replay.cli as cli
import claude_replay.store as store


def _statuses(result):
    return {c.label: c.status for c in result["checks"]}


# ── Pure evaluate() logic ─────────────────────────────────────────────────────

class TestEvaluate:
    def test_fully_healthy(self):
        r = doctor.evaluate(
            hooks_installed=True, command_on_path="/usr/bin/claude-replay",
            db_path="/x.db", db_exists=True, session_count=4,
            last_session_age_hours=2.0,
        )
        assert r["ok"] and r["healthy"]
        assert _statuses(r)["Sessions recorded"] == "ok"

    def test_not_installed_is_fail(self):
        r = doctor.evaluate(
            hooks_installed=False, command_on_path="/usr/bin/claude-replay",
            db_path="/x.db", db_exists=False, session_count=0,
            last_session_age_hours=None,
        )
        assert not r["ok"]  # a fail makes it not-ok
        assert _statuses(r)["Hooks installed"] == "fail"

    def test_installed_but_not_on_path_is_the_silent_failure(self):
        # the headline case: hooks present, command missing -> warn, not healthy
        r = doctor.evaluate(
            hooks_installed=True, command_on_path=None,
            db_path="/x.db", db_exists=False, session_count=0,
            last_session_age_hours=None,
        )
        assert r["ok"]            # nothing strictly broken in settings
        assert not r["healthy"]   # but recording almost certainly isn't happening
        assert _statuses(r)["Hook command on PATH"] == "warn"

    def test_installed_runnable_but_nothing_recorded_warns(self):
        r = doctor.evaluate(
            hooks_installed=True, command_on_path="/usr/bin/claude-replay",
            db_path="/x.db", db_exists=True, session_count=0,
            last_session_age_hours=None,
        )
        assert r["ok"] and not r["healthy"]
        assert _statuses(r)["Sessions recorded"] == "warn"

    def test_singular_plural_and_recency_render(self):
        r = doctor.evaluate(
            hooks_installed=True, command_on_path="cr",
            db_path="/x.db", db_exists=True, session_count=1,
            last_session_age_hours=26.0,
        )
        text = doctor.render(r)
        assert "1 session;" in text and "26h ago" in text
        assert "session;" in text  # singular, not "sessions"

    def test_render_contains_hints_for_problems(self):
        r = doctor.evaluate(
            hooks_installed=False, command_on_path=None,
            db_path="/x.db", db_exists=False, session_count=0,
            last_session_age_hours=None,
        )
        text = doctor.render(r)
        assert "claude-replay install" in text       # fix hint for not-installed
        assert "not fully set up" in text.lower()


class TestAge:
    def test_minutes(self):
        assert doctor._age(0.25) == "15m"

    def test_hours(self):
        assert doctor._age(5.0) == "5h"

    def test_days(self):
        assert doctor._age(72.0) == "3d"


# ── CLI integration (real store, tmp DB) ──────────────────────────────────────

class TestDoctorCommand:
    def test_doctor_runs_against_empty_db(self, fresh_db, monkeypatch, capsys):
        # no settings file -> not installed; empty DB -> 0 sessions
        monkeypatch.setenv("CLAUDE_REPLAY_SETTINGS", str(fresh_db.parent / "settings.json"))
        rc = cli.cmd_doctor()
        out = capsys.readouterr().out
        assert "Claude Replay — doctor" in out
        assert "Hooks installed" in out
        assert rc == 1  # not installed -> non-zero

    def test_doctor_counts_real_sessions(self, fresh_db, monkeypatch, capsys):
        store.get_or_create_session("s1", project_dir="/p", model="m", objective="o")
        store.get_or_create_session("s2", project_dir="/p", model="m", objective="o")
        settings = fresh_db.parent / "settings.json"
        settings.write_text(
            __import__("json").dumps(cli.merge_hooks({})), encoding="utf-8")
        monkeypatch.setenv("CLAUDE_REPLAY_SETTINGS", str(settings))
        rc = cli.cmd_doctor()
        out = capsys.readouterr().out
        assert "2 sessions" in out
        assert rc == 0  # installed + sessions present -> ok
