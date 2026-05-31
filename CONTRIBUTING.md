# Contributing to Claude Replay

Thanks for your interest. Claude Replay is intentionally minimal — a passive
recording layer with a clean read surface. Contributions that stay within that
scope are welcome.

## What we accept

- Bug fixes with tests
- Performance improvements to the hook path (must stay <50ms)
- Coverage improvements for existing modules
- Documentation fixes

## What we don't accept

- Features that add network calls to the hook path
- New external dependencies without discussion (open an issue first)
- Breaking changes to the MCP tool signatures (`replay_*`)
- Features that duplicate Claude Code native behaviour

## Development setup

```bash
pip install -e ".[dev]"
pytest
ruff check .
```

> If `pip install -e` fails on your environment (a known hatchling editable-install
> quirk on some setups), install the deps directly instead:
> `pip install mcp starlette uvicorn anyio textual httpx pytest pytest-asyncio ruff`.

Tests use a temporary SQLite database (`tmp_path` fixture) — they never touch
`~/.claude-replay/sessions.db`. All tests must pass before any PR is merged.

## Commit style

One sentence, imperative mood: `Add checkpoint auto-write on Stop hook`.

## Code rules (from CLAUDE.md)

- All DB access through `store.py` — no raw `sqlite3` calls elsewhere
- Hooks must complete in <50ms — no blocking I/O except the SQLite write
- No external network calls from hooks — offline-first, always
- Follow Bridge's patterns exactly unless there is a specific reason not to
