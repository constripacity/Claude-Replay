# Changelog

All notable changes to Claude Replay are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Death-cause classification** (`classify.py`) — infers *why* a session ended
  (clean finish, interrupted, rate limited, context overflow, API error) from
  the recorded tail instead of a flat "interrupted". A pure, DB-free module;
  the single source of truth surfaced in the resume brief, `status`, the
  `replay_status` MCP tool, and the dashboard JSON (`death_cause`/`death_label`).
- **Richer resume briefs** (`resume.py`) — the brief now reports how the session
  ended (with the last error, when known) and a **live repository-state** section:
  the current branch and uncommitted-file list, read from the project dir at
  brief time so the restart reflects the tree you're actually resuming into.
- **Full-text search** (`store.search`) — FTS5 index over event payloads (with a
  LIKE fallback on builds without FTS5), also matching objective / name / tags.
  Surfaced as `claude-replay search <query>`, a `replay_search` MCP tool, an
  `/api/search` endpoint, and a live search box in the dashboard.
- **Session naming & tagging** — name a session and add/remove tags via
  `claude-replay tag`, the `replay_tag` MCP tool, or the JSON API; shown in
  `sessions` / `status`, the dashboard, and the TUI. New `name`/`tags` columns,
  migrated onto existing 0.1.0 databases automatically.
- **Retention & pruning** (`store.prune`) — `claude-replay prune --older-than 30d`
  deletes stale sessions (by last activity) and VACUUMs so the file shrinks.
- **Death cause in the UI** — the `death_cause`/`death_label` field now renders in
  the dashboard (list badge + detail) and the TUI inspector.
- **Supply-chain hygiene** — Dependabot (pip + GitHub Actions, weekly) to keep
  dependencies and the SHA-pinned actions current. (CodeQL code scanning runs via
  GitHub's repo-managed default setup.)

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
