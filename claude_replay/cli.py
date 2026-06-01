"""Console-script entry point for `claude-replay`.

Session 2 surface: install / uninstall / hook.
(status, sessions, resume, export, serve land in later sessions.)
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from . import __version__, store

# The three hooks Replay installs. Each tuple is (event_name, command, matcher).
# matcher=None means the event block carries no "matcher" key (Stop-style).
HOOK_SPECS: list[tuple[str, str, str | None]] = [
    ("PreToolUse", "claude-replay hook pre-tool", ""),
    ("PostToolUse", "claude-replay hook post-tool", ""),
    ("Stop", "claude-replay hook stop", None),
]

_OUR_COMMAND_PREFIX = "claude-replay hook "


# ── Settings path ─────────────────────────────────────────────────────────────

def settings_path() -> str:
    """Resolve the Claude Code settings.json path.
    Override with CLAUDE_REPLAY_SETTINGS (used by tests)."""
    override = os.environ.get("CLAUDE_REPLAY_SETTINGS")
    if override:
        return override
    return str(Path.home() / ".claude" / "settings.json")


def _read_settings(path: str) -> dict[str, Any]:
    p = Path(path)
    if not p.exists():
        return {}
    text = p.read_text(encoding="utf-8")
    if not text.strip():
        return {}
    data = json.loads(text)
    return data if isinstance(data, dict) else {}


def _write_settings(path: str, settings: dict[str, Any]) -> None:
    """Atomic write: temp file in the same dir, then os.replace."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)


# ── Pure merge / remove logic ─────────────────────────────────────────────────

def _is_ours(hook_entry: dict[str, Any]) -> bool:
    command = hook_entry.get("command", "")
    return isinstance(command, str) and command.startswith(_OUR_COMMAND_PREFIX)


def _command_present(blocks: list[dict[str, Any]], command: str) -> bool:
    for block in blocks:
        for entry in block.get("hooks", []):
            if entry.get("command") == command:
                return True
    return False


def merge_hooks(settings: dict[str, Any]) -> dict[str, Any]:
    """Return settings with Replay's three hooks merged in. Idempotent —
    never duplicates, never disturbs other tools' hooks."""
    result = copy.deepcopy(settings)
    hooks = result.setdefault("hooks", {})
    for event, command, matcher in HOOK_SPECS:
        blocks = hooks.setdefault(event, [])
        if _command_present(blocks, command):
            continue
        entry = {"type": "command", "command": command}
        if matcher is not None:
            blocks.append({"matcher": matcher, "hooks": [entry]})
        else:
            blocks.append({"hooks": [entry]})
    return result


def remove_hooks(settings: dict[str, Any]) -> dict[str, Any]:
    """Return settings with only Replay's hooks removed. Leaves every other
    hook — even ones sharing a matcher block with ours — intact."""
    result = copy.deepcopy(settings)
    hooks = result.get("hooks")
    if not isinstance(hooks, dict):
        return result
    for event in list(hooks.keys()):
        blocks = hooks.get(event, [])
        if not isinstance(blocks, list):
            continue
        new_blocks: list[dict[str, Any]] = []
        for block in blocks:
            inner = [h for h in block.get("hooks", []) if not _is_ours(h)]
            if inner:
                kept = dict(block)
                kept["hooks"] = inner
                new_blocks.append(kept)
            # block with no surviving hooks is dropped entirely
        if new_blocks:
            hooks[event] = new_blocks
        else:
            del hooks[event]
    if not hooks:
        del result["hooks"]
    return result


def installed_status(settings: dict[str, Any]) -> dict[str, bool]:
    """Map each event name → whether Replay's command is present."""
    hooks = settings.get("hooks", {}) if isinstance(settings, dict) else {}
    status: dict[str, bool] = {}
    for event, command, _ in HOOK_SPECS:
        blocks = hooks.get(event, []) if isinstance(hooks, dict) else []
        status[event] = _command_present(blocks, command)
    return status


def is_installed(settings: dict[str, Any]) -> bool:
    return all(installed_status(settings).values())


# ── Commands ──────────────────────────────────────────────────────────────────

def cmd_install(path: str) -> int:
    settings = _read_settings(path)
    if is_installed(settings):
        print(f"Claude Replay hooks already installed in {path}")
        return 0
    merged = merge_hooks(settings)
    _write_settings(path, merged)
    print(f"✓ Installed Claude Replay hooks into {path}")
    print("  PreToolUse  → claude-replay hook pre-tool")
    print("  PostToolUse → claude-replay hook post-tool")
    print("  Stop        → claude-replay hook stop")
    return 0


def cmd_uninstall(path: str) -> int:
    settings = _read_settings(path)
    if not any(installed_status(settings).values()):
        print(f"No Claude Replay hooks found in {path}")
        return 0
    cleaned = remove_hooks(settings)
    _write_settings(path, cleaned)
    print(f"✓ Removed Claude Replay hooks from {path}")
    return 0


def cmd_hook(hook_type: str) -> int:
    from . import hooks

    return hooks.run(hook_type)


# ── Read-side commands ────────────────────────────────────────────────────────

def _latest_session_id() -> str | None:
    sessions = store.list_sessions(1)
    return sessions[0]["id"] if sessions else None


def _default_export_dir() -> str:
    return str(Path.home() / ".claude-replay" / "exports")


def _duration(started: str | None, ended: str | None) -> str:
    if not started or not ended:
        return "—"
    try:
        from datetime import datetime

        a = datetime.fromisoformat(started.replace("Z", "+00:00"))
        b = datetime.fromisoformat(ended.replace("Z", "+00:00"))
        secs = int((b - a).total_seconds())
        if secs < 0:
            return "—"
        h, rem = divmod(secs, 3600)
        m, s = divmod(rem, 60)
        return f"{h:02d}:{m:02d}:{s:02d}"
    except ValueError:
        return "—"


def cmd_resume(session_id: str | None) -> int:
    from . import resume

    sid = session_id or _latest_session_id()
    if sid is None:
        print("No sessions recorded yet.")
        return 1
    print(resume.generate_brief(sid))
    return 0


def cmd_export(session_id: str | None, output_dir: str | None) -> int:
    from . import export

    sid = session_id or _latest_session_id()
    if sid is None:
        print("No sessions recorded yet.")
        return 1
    try:
        path = export.render_html(sid, output_dir or _default_export_dir())
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"✓ Exported trace → {path}")
    return 0


def cmd_sessions(limit: int) -> int:
    sessions = store.list_sessions(limit)
    if not sessions:
        print("No sessions recorded yet.")
        return 0
    print(f"{'ID':10} {'STATUS':12} {'MODEL':20} {'EVENTS':>6} {'CKPTS':>5}  STARTED")
    for s in sessions:
        tags = "  " + " ".join(f"#{t}" for t in s["tags"]) if s["tags"] else ""
        name = f"  “{s['name']}”" if s["name"] else ""
        print(
            f"{s['id'][:8]:10} "
            f"{(s['status'] or '—'):12} "
            f"{(s['model'] or '—')[:20]:20} "
            f"{store.count_events(s['id']):>6} "
            f"{store.count_checkpoints(s['id']):>5}  "
            f"{s['started_at'] or '—'}"
            f"{name}{tags}"
        )
    return 0


def cmd_search(query: str, limit: int) -> int:
    results = store.search(query, limit)
    if not results:
        print(f"No matches for: {query}")
        return 0
    print(f"{len(results)} session(s) match '{query}':")
    for r in results:
        s = r["session"]
        label = s["name"] or s["objective"] or "(no objective)"
        tags = " " + " ".join(f"#{t}" for t in s["tags"]) if s["tags"] else ""
        plural = "es" if r["matches"] != 1 else ""
        print(f"  {s['id'][:8]}  [{s['status']}]  {r['matches']} match{plural}  {label[:54]}{tags}")
        if r["snippet"]:
            print(f"      … {r['snippet']}")
    return 0


def cmd_prune(older_than: str, assume_yes: bool) -> int:
    days = _parse_age(older_than)
    if days is None:
        print(f"error: could not parse age '{older_than}' (try 30d, 4w, or a number of days)",
              file=sys.stderr)
        return 1
    if not assume_yes:
        try:
            resp = input(f"Delete sessions with no activity in the last {days} days? Type 'yes': ")
        except EOFError:
            resp = ""
        if resp.strip().lower() != "yes":
            print("Aborted.")
            return 1
    n = store.prune(days)
    print(f"✓ Pruned {n} session{'s' if n != 1 else ''} older than {days} days.")
    return 0


def cmd_tag(session_id: str | None, name: str | None,
            add: str | None, remove: str | None, clear: bool) -> int:
    sid = session_id or _latest_session_id()
    if sid is None:
        print("No sessions recorded yet.")
        return 1
    if store.get_session(sid) is None:
        print(f"error: no session with id: {sid}", file=sys.stderr)
        return 1
    if name is not None:
        store.set_name(sid, name)
    if clear:
        store.set_tags(sid, [])
    if add:
        store.add_tags(sid, _split_csv(add))
    if remove:
        store.remove_tags(sid, _split_csv(remove))
    s = store.get_session(sid)
    print(f"✓ {sid[:8]}  name: {s['name'] or '—'}  tags: {', '.join(s['tags']) or '—'}")
    return 0


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _parse_age(spec: str) -> int | None:
    """Parse an age like '30d', '4w', or a bare number of days. Returns days."""
    s = str(spec).strip().lower()
    m = re.fullmatch(r"(\d+)\s*([dw]?)", s)
    if not m:
        return None
    n = int(m.group(1))
    return n * 7 if m.group(2) == "w" else n


def cmd_status() -> int:
    sid = _latest_session_id()
    if sid is None:
        print("No sessions recorded yet.")
        return 0
    from . import classify

    data = store.get_resume_data(sid)
    s = data["session"]
    death = classify.classify(s, store.list_events(sid))
    print(f"Session:     {s['id']}")
    if s["name"]:
        print(f"Name:        {s['name']}")
    print(f"Objective:   {s['objective'] or '(not recorded)'}")
    print(f"Status:      {s['status']}")
    print(f"How it ended: {death['label']}"
          + (f"  ({death['detail']})" if death['detail'] else ""))
    if s["tags"]:
        print(f"Tags:        {', '.join(s['tags'])}")
    print(f"Model:       {s['model'] or '(unknown)'}")
    print(f"Project:     {s['project_dir'] or '—'}")
    print(f"Started:     {s['started_at']}")
    print(f"Ended:       {s['ended_at'] or '— (still running / interrupted)'}")
    print(f"Duration:    {_duration(s['started_at'], s['ended_at'])}")
    print(f"Events:      {data['event_count']}")
    print(f"Checkpoints: {data['checkpoint_count']}")
    return 0


def cmd_serve(host: str, port: int) -> int:
    import uvicorn

    from . import server

    bar = "━" * 52
    print(bar)
    print("  Claude Replay — Session checkpoint & recovery server")
    print(f"  Version: {__version__}")
    print(f"  DB: {os.path.abspath(store.DB_PATH)}")
    print(f"  http://localhost:{port}/             ← Dashboard")
    print(f"  http://localhost:{port}/sse          ← MCP config")
    print(f"  http://localhost:{port}/api/state    ← JSON state")
    print(f"  http://localhost:{port}/status       ← Health check")
    print(bar)
    sys.stdout.flush()

    store.db()  # initialize DB before serving
    uvicorn.run(server.app, host=host, port=port)
    return 0


def cmd_tui(url: str) -> int:
    from .tui import ReplayTUI

    ReplayTUI(url=url).run()
    return 0


def cmd_reset(assume_yes: bool) -> int:
    if not assume_yes:
        try:
            resp = input("This deletes ALL recorded sessions. Type 'yes' to confirm: ")
        except EOFError:
            resp = ""
        if resp.strip().lower() != "yes":
            print("Aborted.")
            return 1
    store.reset_all()
    print("✓ All sessions deleted.")
    return 0


# ── Argument parsing ──────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="claude-replay",
        description="Session checkpoint and recovery layer for Claude Code.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command")

    install_p = sub.add_parser("install", help="Install hooks into ~/.claude/settings.json")
    install_p.add_argument("--settings", default=None, help="Path to settings.json (default: ~/.claude/settings.json)")

    uninstall_p = sub.add_parser("uninstall", help="Remove Replay hooks from settings.json")
    uninstall_p.add_argument("--settings", default=None, help="Path to settings.json")

    hook_p = sub.add_parser("hook", help="Internal — called by Claude Code hooks")
    hook_p.add_argument("type", choices=["pre-tool", "post-tool", "stop"])

    resume_p = sub.add_parser("resume", help="Print a resume brief for the last (or given) session")
    resume_p.add_argument("session_id", nargs="?", default=None, help="Session ID (default: most recent)")

    export_p = sub.add_parser("export", help="Export a session as a self-contained HTML trace")
    export_p.add_argument("session_id", nargs="?", default=None, help="Session ID (default: most recent)")
    export_p.add_argument("--output", default=None, help="Output directory (default: ~/.claude-replay/exports)")

    sessions_p = sub.add_parser("sessions", help="List recent sessions")
    sessions_p.add_argument("--limit", type=int, default=10, help="How many to show (default: 10)")

    search_p = sub.add_parser("search", help="Full-text search across sessions")
    search_p.add_argument("query", help="Text to search for (event payloads, objective, name, tags)")
    search_p.add_argument("--limit", type=int, default=20, help="Max sessions to return (default: 20)")

    prune_p = sub.add_parser("prune", help="Delete sessions older than a cutoff (destructive)")
    prune_p.add_argument("--older-than", default="30d",
                         help="Age cutoff: 30d, 4w, or a number of days (default: 30d)")
    prune_p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    tag_p = sub.add_parser("tag", help="Name or tag a session for later retrieval")
    tag_p.add_argument("session_id", nargs="?", default=None, help="Session ID (default: most recent)")
    tag_p.add_argument("--name", default=None, help="Set a human-friendly name (empty string clears)")
    tag_p.add_argument("--add", default=None, help="Comma-separated tags to add")
    tag_p.add_argument("--remove", default=None, help="Comma-separated tags to remove")
    tag_p.add_argument("--clear", action="store_true", help="Remove all tags")

    sub.add_parser("status", help="Show the current/last session status")

    serve_p = sub.add_parser("serve", help="Start the MCP + dashboard server (port 8766)")
    serve_p.add_argument("--host", default="127.0.0.1", help="Interface to bind (default: 127.0.0.1)")
    serve_p.add_argument("--port", type=int, default=8766, help="Port to listen on (default: 8766)")

    tui_p = sub.add_parser("tui", help="Launch the terminal session browser (needs a running serve)")
    tui_p.add_argument("--url", default="http://127.0.0.1:8766", help="Replay server URL (default: http://127.0.0.1:8766)")

    reset_p = sub.add_parser("reset", help="Delete ALL recorded sessions (destructive)")
    reset_p.add_argument("--yes", action="store_true", help="Skip the confirmation prompt")

    args = parser.parse_args(argv)

    # stdout may be ignored for the hook subcommand, but reconfigure for the
    # banner-bearing commands (Windows cp1252 chokes on ✓ otherwise).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError):
        pass

    if args.command == "install":
        return cmd_install(args.settings or settings_path())
    if args.command == "uninstall":
        return cmd_uninstall(args.settings or settings_path())
    if args.command == "hook":
        return cmd_hook(args.type)
    if args.command == "resume":
        return cmd_resume(args.session_id)
    if args.command == "export":
        return cmd_export(args.session_id, args.output)
    if args.command == "sessions":
        return cmd_sessions(args.limit)
    if args.command == "search":
        return cmd_search(args.query, args.limit)
    if args.command == "prune":
        return cmd_prune(args.older_than, args.yes)
    if args.command == "tag":
        return cmd_tag(args.session_id, args.name, args.add, args.remove, args.clear)
    if args.command == "status":
        return cmd_status()
    if args.command == "serve":
        return cmd_serve(args.host, args.port)
    if args.command == "tui":
        return cmd_tui(args.url)
    if args.command == "reset":
        return cmd_reset(args.yes)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
