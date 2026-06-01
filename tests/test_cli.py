"""Tests for claude_replay.cli — install/uninstall merge logic + commands."""

from __future__ import annotations

import io
import json

import pytest

import claude_replay.cli as cli
import claude_replay.store as store


# ── Pure merge logic ──────────────────────────────────────────────────────────

class TestMergeHooks:
    def test_merge_into_empty(self):
        result = cli.merge_hooks({})
        assert "PreToolUse" in result["hooks"]
        assert "PostToolUse" in result["hooks"]
        assert "Stop" in result["hooks"]

    def test_pre_tool_command(self):
        result = cli.merge_hooks({})
        block = result["hooks"]["PreToolUse"][0]
        assert block["matcher"] == ""
        assert block["hooks"][0]["command"] == "claude-replay hook pre-tool"

    def test_stop_has_no_matcher(self):
        result = cli.merge_hooks({})
        block = result["hooks"]["Stop"][0]
        assert "matcher" not in block
        assert block["hooks"][0]["command"] == "claude-replay hook stop"

    def test_idempotent(self):
        once = cli.merge_hooks({})
        twice = cli.merge_hooks(once)
        assert len(twice["hooks"]["PreToolUse"]) == 1
        assert len(twice["hooks"]["PostToolUse"]) == 1
        assert len(twice["hooks"]["Stop"]) == 1

    def test_preserves_existing_hooks(self):
        existing = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "other-tool check"}]}
                ]
            }
        }
        result = cli.merge_hooks(existing)
        commands = [
            h["command"]
            for block in result["hooks"]["PreToolUse"]
            for h in block["hooks"]
        ]
        assert "other-tool check" in commands
        assert "claude-replay hook pre-tool" in commands

    def test_does_not_mutate_input(self):
        original = {}
        cli.merge_hooks(original)
        assert original == {}

    def test_preserves_unrelated_top_level_keys(self):
        result = cli.merge_hooks({"model": "opus", "theme": "dark"})
        assert result["model"] == "opus"
        assert result["theme"] == "dark"


# ── Pure remove logic ─────────────────────────────────────────────────────────

class TestRemoveHooks:
    def test_remove_all_ours(self):
        installed = cli.merge_hooks({})
        cleaned = cli.remove_hooks(installed)
        assert "hooks" not in cleaned

    def test_remove_leaves_other_tools(self):
        settings = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "Bash", "hooks": [{"type": "command", "command": "other-tool check"}]}
                ]
            }
        }
        installed = cli.merge_hooks(settings)
        cleaned = cli.remove_hooks(installed)
        commands = [
            h["command"]
            for block in cleaned["hooks"]["PreToolUse"]
            for h in block["hooks"]
        ]
        assert commands == ["other-tool check"]

    def test_remove_from_shared_block(self):
        # Our hook and another tool's hook in the SAME matcher block
        settings = {
            "hooks": {
                "PreToolUse": [
                    {"matcher": "", "hooks": [
                        {"type": "command", "command": "other-tool check"},
                        {"type": "command", "command": "claude-replay hook pre-tool"},
                    ]}
                ]
            }
        }
        cleaned = cli.remove_hooks(settings)
        inner = cleaned["hooks"]["PreToolUse"][0]["hooks"]
        assert len(inner) == 1
        assert inner[0]["command"] == "other-tool check"

    def test_remove_when_none_present(self):
        settings = {"hooks": {"PreToolUse": [{"matcher": "", "hooks": [{"command": "x"}]}]}}
        cleaned = cli.remove_hooks(settings)
        assert cleaned["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "x"

    def test_remove_no_hooks_key(self):
        assert cli.remove_hooks({"model": "opus"}) == {"model": "opus"}

    def test_does_not_mutate_input(self):
        installed = cli.merge_hooks({})
        snapshot = json.loads(json.dumps(installed))
        cli.remove_hooks(installed)
        assert installed == snapshot


# ── is_installed / status ─────────────────────────────────────────────────────

class TestStatus:
    def test_is_installed_true(self):
        assert cli.is_installed(cli.merge_hooks({})) is True

    def test_is_installed_false_empty(self):
        assert cli.is_installed({}) is False

    def test_is_installed_false_partial(self):
        settings = cli.merge_hooks({})
        # remove the Stop hook only
        del settings["hooks"]["Stop"]
        assert cli.is_installed(settings) is False

    def test_status_map(self):
        status = cli.installed_status(cli.merge_hooks({}))
        assert status == {"PreToolUse": True, "PostToolUse": True, "Stop": True}


# ── File roundtrip via commands ───────────────────────────────────────────────

@pytest.fixture
def settings_file(tmp_path):
    return str(tmp_path / ".claude" / "settings.json")


class TestInstallCommand:
    def test_install_creates_file(self, settings_file):
        rc = cli.cmd_install(settings_file)
        assert rc == 0
        data = json.loads(open(settings_file, encoding="utf-8").read())
        assert cli.is_installed(data)

    def test_install_creates_parent_dir(self, settings_file):
        cli.cmd_install(settings_file)
        import os
        assert os.path.exists(settings_file)

    def test_install_idempotent_on_disk(self, settings_file):
        cli.cmd_install(settings_file)
        cli.cmd_install(settings_file)
        data = json.loads(open(settings_file, encoding="utf-8").read())
        assert len(data["hooks"]["PreToolUse"]) == 1

    def test_install_preserves_existing_file(self, settings_file, tmp_path):
        import os
        os.makedirs(os.path.dirname(settings_file), exist_ok=True)
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump({"model": "opus", "hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "other check"}]}]}}, f)
        cli.cmd_install(settings_file)
        data = json.loads(open(settings_file, encoding="utf-8").read())
        assert data["model"] == "opus"
        commands = [h["command"] for b in data["hooks"]["PreToolUse"] for h in b["hooks"]]
        assert "other check" in commands
        assert "claude-replay hook pre-tool" in commands


class TestUninstallCommand:
    def test_uninstall_removes(self, settings_file):
        cli.cmd_install(settings_file)
        cli.cmd_uninstall(settings_file)
        data = json.loads(open(settings_file, encoding="utf-8").read())
        assert not any(cli.installed_status(data).values())

    def test_uninstall_roundtrip_preserves_others(self, settings_file):
        import os
        os.makedirs(os.path.dirname(settings_file), exist_ok=True)
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump({"hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "other check"}]}]}}, f)
        cli.cmd_install(settings_file)
        cli.cmd_uninstall(settings_file)
        data = json.loads(open(settings_file, encoding="utf-8").read())
        commands = [h["command"] for b in data["hooks"]["PreToolUse"] for h in b["hooks"]]
        assert commands == ["other check"]

    def test_uninstall_missing_file(self, settings_file):
        rc = cli.cmd_uninstall(settings_file)
        assert rc == 0  # no-op, no crash

    def test_uninstall_no_replay_hooks(self, settings_file):
        import os
        os.makedirs(os.path.dirname(settings_file), exist_ok=True)
        with open(settings_file, "w", encoding="utf-8") as f:
            json.dump({"hooks": {"PreToolUse": [{"matcher": "", "hooks": [{"command": "x"}]}]}}, f)
        rc = cli.cmd_uninstall(settings_file)
        assert rc == 0


# ── settings_path resolution ──────────────────────────────────────────────────

class TestSettingsPath:
    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_REPLAY_SETTINGS", "/custom/settings.json")
        assert cli.settings_path() == "/custom/settings.json"

    def test_default_path(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_REPLAY_SETTINGS", raising=False)
        assert cli.settings_path().endswith("settings.json")
        assert ".claude" in cli.settings_path()


# ── main() entry point ────────────────────────────────────────────────────────

class TestMain:
    def test_main_install(self, tmp_path):
        path = str(tmp_path / "settings.json")
        rc = cli.main(["install", "--settings", path])
        assert rc == 0
        assert cli.is_installed(json.loads(open(path, encoding="utf-8").read()))

    def test_main_uninstall(self, tmp_path):
        path = str(tmp_path / "settings.json")
        cli.main(["install", "--settings", path])
        rc = cli.main(["uninstall", "--settings", path])
        assert rc == 0

    def test_main_no_command_prints_help(self, capsys):
        rc = cli.main([])
        assert rc == 1

    def test_main_hook_reads_stdin(self, fresh_db, monkeypatch):
        payload = {"session_id": "main-hook-sid", "cwd": "/p", "tool_name": "Read", "tool_input": {}}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        rc = cli.main(["hook", "pre-tool"])
        assert rc == 0
        import claude_replay.store as store
        assert store.get_session("main-hook-sid") is not None

    def test_main_install_uses_env_path(self, tmp_path, monkeypatch):
        path = str(tmp_path / "envsettings.json")
        monkeypatch.setenv("CLAUDE_REPLAY_SETTINGS", path)
        rc = cli.main(["install"])
        assert rc == 0
        assert cli.is_installed(json.loads(open(path, encoding="utf-8").read()))


# ── Read-side commands ────────────────────────────────────────────────────────

def _seed(sid="cli-sess-1"):
    store.get_or_create_session(sid, project_dir="/proj", model="claude-opus-4-8", objective="Do a thing")
    store.insert_event(sid, "tool_result", tool_name="Read")
    store.write_checkpoint(sid, "Did a thing", step_next="Next thing", files_touched=["a.py"])
    return sid


class TestResumeCommand:
    def test_no_sessions(self, fresh_db, capsys):
        rc = cli.cmd_resume(None)
        assert rc == 1
        assert "No sessions" in capsys.readouterr().out

    def test_resume_latest(self, fresh_db, capsys):
        _seed()
        rc = cli.cmd_resume(None)
        assert rc == 0
        assert "Do a thing" in capsys.readouterr().out

    def test_resume_by_id(self, fresh_db, capsys):
        _seed("explicit-id")
        rc = cli.cmd_resume("explicit-id")
        assert rc == 0
        assert "Did a thing" in capsys.readouterr().out

    def test_main_resume(self, fresh_db, capsys):
        _seed()
        assert cli.main(["resume"]) == 0


class TestExportCommand:
    def test_no_sessions(self, fresh_db, capsys):
        assert cli.cmd_export(None, None) == 1

    def test_export_writes_file(self, fresh_db, tmp_path, capsys):
        _seed()
        rc = cli.cmd_export(None, str(tmp_path))
        assert rc == 0
        out = capsys.readouterr().out
        assert "Exported" in out and "trace" in out
        assert list(tmp_path.glob("*.html"))

    def test_main_export(self, fresh_db, tmp_path):
        _seed()
        assert cli.main(["export", "--output", str(tmp_path)]) == 0

    def test_export_json_format(self, fresh_db, tmp_path):
        _seed()
        assert cli.main(["export", "--output", str(tmp_path), "--format", "json"]) == 0
        assert list(tmp_path.glob("*.json"))

    def test_export_md_format(self, fresh_db, tmp_path):
        _seed()
        assert cli.cmd_export(None, str(tmp_path), "md") == 0
        assert list(tmp_path.glob("*.md"))


class TestSessionsCommand:
    def test_empty(self, fresh_db, capsys):
        rc = cli.cmd_sessions(10)
        assert rc == 0
        assert "No sessions" in capsys.readouterr().out

    def test_lists(self, fresh_db, capsys):
        _seed("sess-a")
        _seed("sess-b")
        rc = cli.cmd_sessions(10)
        assert rc == 0
        out = capsys.readouterr().out
        assert "sess-a"[:8] in out
        assert "STATUS" in out

    def test_main_sessions(self, fresh_db):
        _seed()
        assert cli.main(["sessions", "--limit", "5"]) == 0


class TestStatusCommand:
    def test_empty(self, fresh_db, capsys):
        rc = cli.cmd_status()
        assert rc == 0
        assert "No sessions" in capsys.readouterr().out

    def test_shows_latest(self, fresh_db, capsys):
        _seed()
        rc = cli.cmd_status()
        assert rc == 0
        out = capsys.readouterr().out
        assert "Do a thing" in out
        assert "Checkpoints: 1" in out

    def test_shows_insights(self, fresh_db, capsys):
        _seed()
        cli.cmd_status()
        out = capsys.readouterr().out
        assert "Insights:" in out
        assert "Tool calls:" in out
        assert "Top tools:" in out

    def test_main_status(self, fresh_db):
        _seed()
        assert cli.main(["status"]) == 0


class TestSearchCommand:
    def test_no_match(self, fresh_db, capsys):
        _seed()
        assert cli.cmd_search("nonexistentxyz", 20) == 0
        assert "No matches" in capsys.readouterr().out

    def test_match_lists_session(self, fresh_db, capsys):
        _seed()
        rc = cli.cmd_search("Read", 20)
        assert rc == 0
        out = capsys.readouterr().out
        assert "cli-sess-1"[:8] in out
        assert "match" in out

    def test_main_search(self, fresh_db):
        _seed()
        assert cli.main(["search", "thing"]) == 0


class TestDiffCommand:
    def test_missing_session(self, fresh_db, capsys):
        assert cli.cmd_diff("nope", "nada") == 1
        assert "not found" in capsys.readouterr().err

    def test_diff_output(self, fresh_db, capsys):
        _seed("sess-a")
        _seed("sess-b")
        rc = cli.cmd_diff("sess-a", "sess-b")
        assert rc == 0
        out = capsys.readouterr().out
        assert "A: sess-a"[:8] in out
        assert "tool calls" in out

    def test_main_diff(self, fresh_db):
        _seed("sess-a")
        _seed("sess-b")
        assert cli.main(["diff", "sess-a", "sess-b"]) == 0


class TestPruneCommand:
    def test_parse_age(self):
        assert cli._parse_age("30d") == 30
        assert cli._parse_age("4w") == 28
        assert cli._parse_age("15") == 15
        assert cli._parse_age("nonsense") is None

    def test_bad_age_returns_error(self, fresh_db, capsys):
        rc = cli.cmd_prune("banana", assume_yes=True)
        assert rc == 1
        assert "could not parse" in capsys.readouterr().err

    def test_prunes_with_yes(self, fresh_db, capsys):
        store.get_or_create_session("old", objective="x")
        store.db().execute("UPDATE sessions SET started_at = ? WHERE id = ?",
                           ("2020-01-01T00:00:00Z", "old"))
        rc = cli.cmd_prune("30d", assume_yes=True)
        assert rc == 0
        assert store.get_session("old") is None

    def test_aborts_without_confirm(self, fresh_db, monkeypatch):
        _seed()
        monkeypatch.setattr("builtins.input", lambda _: "no")
        rc = cli.cmd_prune("0d", assume_yes=False)
        assert rc == 1
        assert len(store.list_sessions()) == 1

    def test_main_prune(self, fresh_db):
        assert cli.main(["prune", "--older-than", "30d", "--yes"]) == 0


class TestTagCommand:
    def test_no_sessions(self, fresh_db, capsys):
        assert cli.cmd_tag(None, None, None, None, False) == 1
        assert "No sessions" in capsys.readouterr().out

    def test_unknown_session(self, fresh_db, capsys):
        _seed()
        assert cli.cmd_tag("ghost", None, "x", None, False) == 1
        assert "no session" in capsys.readouterr().err

    def test_add_and_name(self, fresh_db, capsys):
        _seed()
        rc = cli.cmd_tag("cli-sess-1", "My run", "bug,auth", None, False)
        assert rc == 0
        s = store.get_session("cli-sess-1")
        assert s["name"] == "My run"
        assert s["tags"] == ["bug", "auth"]

    def test_remove_and_clear(self, fresh_db):
        _seed()
        cli.cmd_tag("cli-sess-1", None, "a,b,c", None, False)
        cli.cmd_tag("cli-sess-1", None, None, "b", False)
        assert store.get_session("cli-sess-1")["tags"] == ["a", "c"]
        cli.cmd_tag("cli-sess-1", None, None, None, True)
        assert store.get_session("cli-sess-1")["tags"] == []

    def test_defaults_to_latest(self, fresh_db):
        _seed()
        assert cli.cmd_tag(None, None, "solo", None, False) == 0
        assert store.get_session("cli-sess-1")["tags"] == ["solo"]

    def test_main_tag(self, fresh_db):
        _seed()
        assert cli.main(["tag", "cli-sess-1", "--add", "x"]) == 0


class TestMcpCommand:
    def test_cmd_mcp_runs_stdio(self, fresh_db, monkeypatch):
        # Replace the blocking stdio server with an async no-op so cmd_mcp
        # returns cleanly without holding stdin/stdout.
        import claude_replay.server as server

        called = {}

        async def fake_run_stdio():
            called["ran"] = True

        monkeypatch.setattr(server, "run_stdio", fake_run_stdio)
        assert cli.cmd_mcp() == 0
        assert called.get("ran") is True

    def test_main_mcp_dispatches(self, fresh_db, monkeypatch):
        monkeypatch.setattr(cli, "cmd_mcp", lambda: 0)
        assert cli.main(["mcp"]) == 0


class TestResetCommand:
    def test_reset_with_yes(self, fresh_db, capsys):
        _seed()
        rc = cli.cmd_reset(True)
        assert rc == 0
        assert store.list_sessions() == []

    def test_reset_aborts_without_confirm(self, fresh_db, monkeypatch):
        _seed()
        monkeypatch.setattr("builtins.input", lambda _: "no")
        rc = cli.cmd_reset(False)
        assert rc == 1
        assert len(store.list_sessions()) == 1

    def test_reset_confirmed_via_input(self, fresh_db, monkeypatch):
        _seed()
        monkeypatch.setattr("builtins.input", lambda _: "yes")
        rc = cli.cmd_reset(False)
        assert rc == 0
        assert store.list_sessions() == []

    def test_main_reset_yes(self, fresh_db):
        _seed()
        assert cli.main(["reset", "--yes"]) == 0
        assert store.list_sessions() == []


class TestDuration:
    def test_computes(self):
        assert cli._duration("2026-05-30T12:00:00Z", "2026-05-30T13:30:45Z") == "01:30:45"

    def test_missing_ended(self):
        assert cli._duration("2026-05-30T12:00:00Z", None) == "—"

    def test_negative(self):
        assert cli._duration("2026-05-30T13:00:00Z", "2026-05-30T12:00:00Z") == "—"
