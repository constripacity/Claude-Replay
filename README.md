# Claude Replay

<!-- mcp-name: io.github.constripacity/claude-replay -->

**The observability layer for Claude Code sessions — search, analytics, and visualization across every project.**

[![PyPI](https://img.shields.io/pypi/v/claude-replay?cacheSeconds=3600)](https://pypi.org/project/claude-replay/)
[![CI](https://github.com/constripacity/Claude-Replay/actions/workflows/ci.yml/badge.svg)](https://github.com/constripacity/Claude-Replay/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)
![MCP](https://img.shields.io/badge/MCP-compatible-orange)

---

Claude Code's `--resume`/`--continue` and `/rewind` recover *the session you're in*. Claude Replay does the part they don't: it hooks passively into every session, records what happened to a local SQLite store, and makes **every past session — across every project — searchable, comparable, measurable, and exportable**. Full-text search, per-session insight metrics, death-cause classification (*why* a session ended), session diffing, a web dashboard, and a terminal UI — none of which Claude Code has natively.

```
Claude Code session
        |
   hooks (PreToolUse · PostToolUse · Stop)
        |
        v
   ~/.claude-replay/sessions.db   ← events + checkpoints
        |
   +----+--------------------+
   |          |              |
 MCP tools  Dashboard      TUI
 (resume)   :8766          (browse)
```

Passive hooks write. MCP tools, the web dashboard, and the terminal UI read. Nothing leaves your machine.

---

## How this complements native Claude Code

Claude Replay is **additive** — it layers on top of the built-ins, it doesn't replace them.

| You want to… | Use |
|---|---|
| Resume / continue the current session | **Native** `claude --continue`, `--resume` |
| Undo file + conversation changes in a session | **Native** `/rewind` |
| Search every past session by content, tool, or outcome | **Replay** `search` |
| See *why* a session ended + per-session metrics | **Replay** `status` / `replay_insights` |
| Compare two runs side by side | **Replay** `diff` |
| Visualize a session timeline / browse all projects | **Replay** dashboard + TUI |
| Export a session as HTML / JSON / Markdown | **Replay** `export --format` |

Think of native resume/rewind as *recovery*, and Replay as *observability* over your whole history.

---

## Architecture

Passive hooks write to SQLite. MCP tools read from SQLite. Dashboard + TUI visualize SQLite. That's the whole system.

| Layer | File | Role |
|-------|------|------|
| Store | `claude_replay/store.py` | All DB access — sessions, events, checkpoints |
| Hooks | `claude_replay/hooks.py` | Record tool calls + auto-checkpoint, dispatched by `claude-replay hook <type>` |
| Recovery | `claude_replay/resume.py` | Generate a resume brief from a session |
| Export | `claude_replay/export.py` | Render a session as a self-contained HTML trace |
| Server | `claude_replay/server.py` | Starlette app — MCP SSE + JSON API + static dashboard |
| TUI | `claude_replay/tui.py` + `tui_client.py` | Textual session browser over the JSON API |
| CLI | `claude_replay/cli.py` | Every subcommand |

> **Port 8766**  deliberately one above Claude Bridge's 8765, so the two siblings can run side by side without colliding.

---

## Quickstart

### 1. Install

```bash
pip install claude-replay
```

Or from a clone if you'd like to hack on it:

```bash
git clone https://github.com/constripacity/Claude-Replay.git
cd Claude-Replay
pip install -e .[dev]              # editable install with test/lint deps
pip install -e .[tui]             # add the terminal UI deps (textual, httpx)
```

> If `pip install -e` fails on your environment (a known hatchling editable-install quirk on some setups), install the deps directly instead:
> `pip install mcp starlette uvicorn anyio textual httpx`.

### 2. Install the hooks

This wires Replay into Claude Code by merging three hooks into `~/.claude/settings.json`. It's idempotent and leaves any other tools' hooks untouched.

```bash
claude-replay install
```

```
✓ Installed Claude Replay hooks into ~/.claude/settings.json
  PreToolUse  → claude-replay hook pre-tool
  PostToolUse → claude-replay hook post-tool
  Stop        → claude-replay hook stop
```

From now on, every Claude Code session is recorded automatically. Remove the hooks any time with `claude-replay uninstall` (it removes only Replay's hooks).

### 3. Start the server (dashboard + MCP tools)

```bash
claude-replay serve                 # defaults: 127.0.0.1:8766
claude-replay serve --port 9000     # custom port
claude-replay serve --host 0.0.0.0  # bind all interfaces
```

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Claude Replay — Session checkpoint & recovery server
  Version: 0.3.0
  DB: ~/.claude-replay/sessions.db
  http://localhost:8766/             ← Dashboard
  http://localhost:8766/sse          ← MCP config
  http://localhost:8766/api/state    ← JSON state
  http://localhost:8766/status       ← Health check
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

### 4. (Optional) Register the MCP tools with Claude Code

So a running Claude Code session can call `replay_resume`, `replay_checkpoint`, etc. directly. Two ways:

**SSE** (alongside the dashboard — needs `claude-replay serve` running):

```bash
claude mcp add --transport sse -s user claude-replay http://localhost:8766/sse
```

**stdio** (no server process — the client launches Replay on demand):

```bash
claude mcp add -s user claude-replay -- claude-replay mcp
# or, without installing: uvx claude-replay mcp
```

Verify with `claude mcp list`  `claude-replay` should show `✓ Connected`. Inside an already-running session, type `/mcp` to re-handshake.

---

## When a session dies

```bash
# What's the state of the last session?
claude-replay status

# Print a paste-ready resume brief (most recent session, or pass an id)
claude-replay resume
claude-replay resume <session-id>

# Browse every recorded session in the terminal
claude-replay tui                   # needs `claude-replay serve` running

# Export a session as a self-contained HTML trace
claude-replay export                # → ~/.claude-replay/exports/<id>.html
```

Paste the `resume` output into a fresh Claude Code session and it picks up where the dead one left off  objective, what was done, what's next, and which files were touched.

---

## MCP Tools

Every connected Claude Code session gets these nine tools:

| Tool | Description |
|------|-------------|
| `replay_status` | Current session summary  objective, status, how it ended, event/checkpoint counts, last activity |
| `replay_checkpoint` | Force a checkpoint of the current session now, with an optional note |
| `replay_resume` | Generate a structured resume brief for a session (default: most recent) |
| `replay_sessions` | List recent sessions with status, model, duration, checkpoint count |
| `replay_insights` | Per-session metrics: how it ended, duration, tool calls, error rate, files touched, top tools |
| `replay_search` | Full-text search across sessions with filters (tool, cause, date, project), ranked by match count |
| `replay_diff` | Compare two sessions: metric deltas + which files each touched |
| `replay_tag` | Name a session and add/remove tags for later retrieval |
| `replay_export` | Render a session as a self-contained trace (html / json / md) and return the path |

---

## CLI Reference

| Command | What it does |
|---------|--------------|
| `claude-replay install` | Merge Replay's hooks into `~/.claude/settings.json` (idempotent) |
| `claude-replay uninstall` | Remove only Replay's hooks |
| `claude-replay status` | Current/last session at a glance, with insight metrics |
| `claude-replay sessions [--limit N]` | List recent sessions (with names + tags) |
| `claude-replay search <query> [--tool T] [--cause C] [--since 7d] [--project P]` | Full-text search with filters (omit query to browse by filter) |
| `claude-replay diff <session-a> <session-b>` | Compare two sessions side by side |
| `claude-replay resume [session_id]` | Print a resume brief (default: most recent) |
| `claude-replay export [session_id] [--output DIR] [--format html\|json\|md]` | Render a trace |
| `claude-replay tag [session_id] [--name N] [--add a,b] [--remove c] [--clear]` | Name or tag a session |
| `claude-replay prune [--older-than 30d] [--yes]` | Delete sessions with no recent activity (destructive) |
| `claude-replay serve [--host H] [--port P]` | Start the MCP + dashboard server (port 8766) |
| `claude-replay mcp` | Serve the MCP tools over stdio (for `uvx claude-replay mcp` / MCP clients) |
| `claude-replay tui [--url URL]` | Launch the terminal session browser |
| `claude-replay reset [--yes]` | Delete **all** recorded sessions (destructive) |
| `claude-replay hook <pre-tool\|post-tool\|stop>` | Internal — invoked by Claude Code's hooks |

---

## Dashboard & TUI

**Web dashboard** (`claude-replay serve`, then open `http://localhost:8766/`)  a vanilla-JS view that polls every 2 s: session list (with how-it-ended badge + tags), a live search box, per-session timeline, and one-click "Copy Resume Brief" / "Export HTML". No CDN, no build step.

**Terminal UI** (`claude-replay tui`)  a Textual browser in the same dark theme. A session sidebar, a live event feed, and a detail inspector showing how the session ended, its tags, the latest checkpoint, and files touched. Keys:

```
↑↓ navigate   Tab switch panel   Space pause
r  resume (copies the brief to your clipboard)
e  export HTML trace
?  help        q quit
```

The TUI talks to the server over HTTP  start `claude-replay serve` first (defaults to `http://127.0.0.1:8766`; point elsewhere with `--url`).

---

## Configuration

| Env var | Default | Purpose |
|---------|---------|---------|
| `CLAUDE_REPLAY_DB` | `~/.claude-replay/sessions.db` | SQLite store location |
| `CLAUDE_SESSION_ID` / `CLAUDE_CODE_SESSION_ID` | — | Session identity (Claude Code sets this in the hook payload); falls back to these env vars, then to a hash of the project dir |
| `CLAUDE_REPLAY_CORS_ORIGIN` | localhost only | Comma-separated extra CORS origins for the server |
| `CLAUDE_REPLAY_NO_DASHBOARD` | — | Set to disable the static dashboard mount (MCP/JSON only) |

The hook path is offline-first by design: it makes **no network calls** and completes in well under 50 ms  just the one SQLite write. Large tool payloads are truncated at 8 KB per event so the DB stays lean.

---

## Development

```bash
pip install -e .[dev]               # or install deps directly (see install note)
python -m pytest                    # full suite
ruff check .
```

Tests use an isolated `tmp_path` SQLite database (the `fresh_db` fixture) — they never touch your real `~/.claude-replay/sessions.db`. See [CONTRIBUTING.md](CONTRIBUTING.md) for the scope and the coding rules.

---

## License

MIT  see [LICENSE](LICENSE).

Sibling to [Claude Bridge](https://github.com/constripacity/Claude-Bridge). Built under the Constripacity banner.
