# Claude Replay — context for Claude Code

## What this repo is
Session checkpoint and recovery layer for Claude Code. Hooks silently into
every session via Claude Code's hooks system. Stores events + checkpoints in
SQLite. Exposes 7 MCP tools for recovery, search, and export. Sibling to Claude
Bridge under Constripacity.

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
- claude_replay/export.py     — HTML trace rendering
- claude_replay/server.py     — Starlette app (MCP SSE + JSON API + static)
- claude_replay/tui_client.py — async httpx client over the JSON API (testable, no Textual)
- claude_replay/tui.py        — Textual session browser (talks to a running `serve`, never the DB)
- claude_replay/cli.py        — all CLI subcommands

## CLI subcommands
install · uninstall · hook <pre-tool|post-tool|stop> · status · sessions ·
search · resume · export · tag · prune · serve · mcp · tui · reset

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
