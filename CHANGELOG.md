# Changelog

All notable changes to Claude Replay are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **Cross-session analytics** (`analytics.py`) — a rollup across *all* recorded
  sessions, the per-developer view native Claude Code doesn't give: total tool
  calls + average per session, overall error rate, the **death-cause breakdown
  ("why your sessions end")**, the tool mix, per-project rollups, and a day-by-day
  activity trend. Surfaced as `claude-replay stats` (with `--limit` / `--project`),
  a `replay_stats` MCP tool, and an `/api/stats` endpoint. Pure `analytics.aggregate()`
  + `store.sessions_with_events()`. MCP tools now number **ten**.
- **`claude-replay doctor`** — a self-check that answers the one question that
  matters after install: *is Replay actually recording?* It verifies the hooks
  are in `settings.json`, that the `claude-replay` command is resolvable on PATH
  (the most common silent failure — Claude Code can't run a hook it can't find,
  and hooks swallow their own errors by design), that the database exists, and
  that sessions are being recorded. Exits non-zero when something's wrong, so it
  is scriptable. Backed by a pure `doctor.evaluate()` and `store.count_sessions()`.
- **Install-time PATH warning** — `claude-replay install` now warns if
  `claude-replay` is not on PATH, pointing at `doctor`, so a broken setup is
  caught at install time instead of discovered as an empty dashboard later.

## [0.3.0] — 2026-06-01

Observability & insight — leaning into what native Claude Code doesn't do.
Repositioned as the observability layer *on top of* native resume/rewind.

### Added

- **Per-session insight metrics** (`metrics.py`) — pure, DB-free per-session stats:
  duration, tool-call count, error count/rate, files touched, and the most-used
  tools, composing the death cause. Surfaced in `status`, the dashboard detail
  pane, the TUI inspector, the `/api/session` payload, and a new `replay_insights`
  MCP tool.
- **Search filters + project grouping** — `claude-replay search` (and the
  `replay_search` MCP tool / `/api/search`) gain `--tool`, `--cause`, `--since`,
  `--until`, `--project` filters and browse-by-filter with no query. The dashboard
  and TUI now group sessions by project.
- **Session diff / compare** — `store.compare()`, `claude-replay diff <a> <b>`,
  a `replay_diff` MCP tool, an `/api/diff` endpoint, and a dashboard compare view:
  metric deltas (tool calls, errors, duration, files) + which files each touched.
- **Structured export** — `export --format json|md|html` (and the `replay_export`
  format arg) render JSON and Markdown alongside HTML, for portability into
  external tooling. MCP tools now number **nine**.

## [0.2.0] — 2026-06-01

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
- **stdio MCP transport** — `claude-replay mcp` serves the seven `replay_*` tools over
  stdio, so any MCP client can launch Replay directly (`uvx claude-replay mcp`) without
  the HTTP server. Published to the MCP registry as
  `io.github.constripacity/claude-replay` (see `server.json`).
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

[0.3.0]: https://github.com/constripacity/Claude-Replay/releases/tag/v0.3.0
[0.2.0]: https://github.com/constripacity/Claude-Replay/releases/tag/v0.2.0
[0.1.0]: https://github.com/constripacity/Claude-Replay/releases/tag/v0.1.0
