# Claude Replay — context for Claude Code

## What this repo is
Observability layer for Claude Code sessions — the cross-project search /
analytics / visualization that complements native resume/rewind (it does NOT
re-implement them; see the positioning note below). Hooks silently into every
session, stores events + checkpoints in SQLite, exposes 10 MCP tools. Sibling to
Claude Bridge under Constripacity.

## Positioning (load-bearing — read before adding features)
Native Claude Code already does resume (`--continue`/`--resume`), rewind
(`/rewind`), and plaintext `/export`. Replay must NOT duplicate those. Build only
in the differentiated lane: full-text **search**, per-session **analytics**
(metrics.py / classify.py), session **diff**, and **visualization** (dashboard/TUI/
structured export). Filter every new feature through "does native already do this?"

## Architecture in one sentence
Passive hooks write to SQLite. MCP tools read from SQLite. Dashboard + TUI
visualize SQLite. Nothing else.

## Port
8766 (Bridge is 8765 — deliberately different)

## Key files
- claude_replay/store.py      — all DB access lives here, nowhere else
- claude_replay/hooks.py      — hook handlers, dispatched by `claude-replay hook <type>`
- claude_replay/resume.py     — resume brief generation (death cause + live git state)
- claude_replay/classify.py   — death-cause classification (pure, no DB access)
- claude_replay/metrics.py    — per-session insight metrics (pure, no DB access)
- claude_replay/doctor.py     — `doctor` self-check (pure evaluate(); CLI gathers facts)
- claude_replay/analytics.py  — cross-session rollup (pure aggregate(), no DB access)
- claude_replay/export.py     — trace rendering (html / json / md)
- claude_replay/server.py     — Starlette app (MCP SSE + JSON API + static)
- claude_replay/tui_client.py — async httpx client over the JSON API (testable, no Textual)
- claude_replay/tui.py        — Textual session browser (talks to a running `serve`, never the DB)
- claude_replay/cli.py        — all CLI subcommands

## CLI subcommands
install · uninstall · hook <pre-tool|post-tool|stop> · status · doctor · stats ·
sessions · search · diff · resume · export · tag · prune · serve · mcp · tui · reset

## DB location
~/.claude-replay/sessions.db (overridable via CLAUDE_REPLAY_DB env var)

## Session identity resolution
1. payload `session_id` (modern Claude Code always sets this)
2. CLAUDE_SESSION_ID env var
3. CLAUDE_CODE_SESSION_ID env var
4. fallback: hash(project_dir)

## Hook payload format (stdin JSON from Claude Code)
PreToolUse:  { session_id?, tool_name, tool_input }
PostToolUse: { session_id?, tool_name, tool_input, tool_response }
Stop:        { session_id? }

## Coding rules
- All DB access through store.py — no raw sqlite3 calls elsewhere
- Hooks must complete in <50ms — no blocking I/O except the SQLite write
- No external network calls from hooks — offline-first, always
- Tests use a tmp_path fixture DB, never the real ~/.claude-replay/sessions.db
- Follow Bridge's patterns exactly unless there is a specific reason not to

## Running tests
python -m pytest tests/test_store.py -v
(pip install -e ".[dev]" may fail on some envs; install deps directly instead:
  pip install mcp starlette uvicorn anyio pytest pytest-asyncio httpx ruff)
