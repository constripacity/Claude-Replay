# Changelog

All notable changes to Claude Replay are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.0] — 2026-05-31

First public release. A passive session checkpoint and recovery layer for
Claude Code: hooks record every session to a local SQLite store, and a CLI,
five MCP tools, a web dashboard, and a terminal UI read it back.

### Added

- **Store** (`store.py`) — SQLite-backed sessions, events, and checkpoints, with
  per-session monotonic sequencing and 8 KB-per-event payload truncation. DB at
  `~/.claude-replay/sessions.db` (overridable via `CLAUDE_REPLAY_DB`).
- **Hooks** (`hooks.py`) — `PreToolUse` / `PostToolUse` / `Stop` handlers that
  record tool calls, auto-checkpoint every 10 tool results, and write a final
  checkpoint on stop. Offline-first, completes in well under 50 ms, and never
  breaks the agent it records.
- **Recovery** (`resume.py`) — generate a paste-ready resume brief (objective,
  work done, pending step, files touched) from any session.
- **Export** (`export.py`) — render a session as a self-contained HTML trace.
- **Server** (`server.py`) — Starlette app on port **8766**: five `replay_*` MCP
  tools over SSE, a JSON API, and a static web dashboard.
- **Terminal UI** (`tui.py` + `tui_client.py`) — a Textual session browser over
  the JSON API, with one-key resume (copies the brief) and export.
- **CLI** (`cli.py`) — `install`, `uninstall`, `status`, `sessions`, `resume`,
  `export`, `serve`, `tui`, `reset`, and the internal `hook` dispatcher.
- **MCP tools** — `replay_status`, `replay_checkpoint`, `replay_resume`,
  `replay_sessions`, `replay_export`.

[0.1.0]: https://github.com/constripacity/Claude-Replay/releases/tag/v0.1.0
