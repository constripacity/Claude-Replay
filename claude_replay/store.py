"""
Claude Replay — SQLite store.

All database access lives here. Nothing else in this package calls sqlite3
directly. Schema: three tables (sessions, checkpoints, events). WAL mode,
synchronous=NORMAL — survives process crashes without corruption.

Default DB location: ~/.claude-replay/sessions.db
Override:           CLAUDE_REPLAY_DB environment variable
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VERSION = "0.1.0"

# ── DB location ───────────────────────────────────────────────────────────────

_DEFAULT_DIR = Path.home() / ".claude-replay"
_DEFAULT_DB = _DEFAULT_DIR / "sessions.db"
DB_PATH: str = os.environ.get("CLAUDE_REPLAY_DB") or str(_DEFAULT_DB)

_conn: sqlite3.Connection | None = None


# ── Connection + schema ───────────────────────────────────────────────────────

def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        path = Path(DB_PATH)
        path.parent.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
        _conn.execute("PRAGMA foreign_keys=ON")
        _apply_schema(_conn)
    return _conn


def _apply_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS sessions (
            id          TEXT PRIMARY KEY,
            started_at  TEXT NOT NULL,
            ended_at    TEXT,
            objective   TEXT,
            project_dir TEXT,
            model       TEXT,
            status      TEXT NOT NULL DEFAULT 'running',
            error_msg   TEXT
        );

        CREATE TABLE IF NOT EXISTS checkpoints (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id    TEXT NOT NULL REFERENCES sessions(id),
            seq           INTEGER NOT NULL,
            timestamp     TEXT NOT NULL,
            step_done     TEXT NOT NULL,
            step_next     TEXT,
            files_touched TEXT,
            diff_patch    TEXT
        );

        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id  TEXT NOT NULL REFERENCES sessions(id),
            seq         INTEGER NOT NULL,
            timestamp   TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            tool_name   TEXT,
            tool_input  TEXT,
            tool_result TEXT,
            error_msg   TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_events_session
            ON events(session_id, seq);

        CREATE INDEX IF NOT EXISTS idx_checkpoints_session
            ON checkpoints(session_id, seq);
    """)


def close() -> None:
    """Close the connection. Mainly for tests."""
    global _conn
    if _conn is not None:
        _conn.close()
        _conn = None


# ── Helpers ───────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _next_seq(conn: sqlite3.Connection, table: str, session_id: str) -> int:
    row = conn.execute(
        f"SELECT COALESCE(MAX(seq), 0) + 1 FROM {table} WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return row[0]


# ── Sessions ──────────────────────────────────────────────────────────────────

def get_or_create_session(
    session_id: str,
    *,
    project_dir: str | None = None,
    model: str | None = None,
    objective: str | None = None,
) -> dict[str, Any]:
    """Return existing session row or create a new one. Thread-safe via SQLite."""
    conn = db()
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is not None:
        return dict(row)
    now = _now()
    conn.execute(
        """INSERT INTO sessions (id, started_at, objective, project_dir, model, status)
           VALUES (?, ?, ?, ?, ?, 'running')""",
        (session_id, now, objective, project_dir, model),
    )
    return {
        "id": session_id,
        "started_at": now,
        "ended_at": None,
        "objective": objective,
        "project_dir": project_dir,
        "model": model,
        "status": "running",
        "error_msg": None,
    }


def update_session(
    session_id: str,
    *,
    status: str | None = None,
    ended_at: str | None = None,
    error_msg: str | None = None,
    objective: str | None = None,
) -> None:
    conn = db()
    fields: list[str] = []
    values: list[Any] = []
    if status is not None:
        fields.append("status = ?")
        values.append(status)
    if ended_at is not None:
        fields.append("ended_at = ?")
        values.append(ended_at)
    if error_msg is not None:
        fields.append("error_msg = ?")
        values.append(error_msg)
    if objective is not None:
        fields.append("objective = ?")
        values.append(objective)
    if not fields:
        return
    values.append(session_id)
    conn.execute(f"UPDATE sessions SET {', '.join(fields)} WHERE id = ?", values)


def get_session(session_id: str) -> dict[str, Any] | None:
    row = db().execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    return dict(row) if row else None


def list_sessions(limit: int = 20) -> list[dict[str, Any]]:
    rows = db().execute(
        "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Events ────────────────────────────────────────────────────────────────────

# Truncate large payloads to avoid bloating the DB.
_MAX_PAYLOAD_BYTES = 8 * 1024


def _truncate(value: Any) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if len(text.encode()) > _MAX_PAYLOAD_BYTES:
        return text.encode()[:_MAX_PAYLOAD_BYTES].decode(errors="replace") + "\n…[truncated]"
    return text


def insert_event(
    session_id: str,
    event_type: str,
    *,
    tool_name: str | None = None,
    tool_input: Any = None,
    tool_result: Any = None,
    error_msg: str | None = None,
) -> int:
    conn = db()
    seq = _next_seq(conn, "events", session_id)
    conn.execute(
        """INSERT INTO events
               (session_id, seq, timestamp, event_type, tool_name, tool_input, tool_result, error_msg)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            seq,
            _now(),
            event_type,
            tool_name,
            _truncate(tool_input),
            _truncate(tool_result),
            error_msg,
        ),
    )
    return seq


def list_events(session_id: str) -> list[dict[str, Any]]:
    rows = db().execute(
        "SELECT * FROM events WHERE session_id = ? ORDER BY seq",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def count_events(session_id: str, event_type: str | None = None) -> int:
    if event_type:
        row = db().execute(
            "SELECT COUNT(*) FROM events WHERE session_id = ? AND event_type = ?",
            (session_id, event_type),
        ).fetchone()
    else:
        row = db().execute(
            "SELECT COUNT(*) FROM events WHERE session_id = ?",
            (session_id,),
        ).fetchone()
    return row[0]


# Tools whose input names a file we consider "touched" (modified).
FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def files_touched(session_id: str) -> list[str]:
    """Distinct file paths modified this session, in first-touch order.
    The single source of truth for 'files touched' — hooks + resume share it."""
    files: list[str] = []
    seen: set[str] = set()
    for event in list_events(session_id):
        if event["tool_name"] not in FILE_TOOLS or not event["tool_input"]:
            continue
        try:
            data = json.loads(event["tool_input"])
        except (ValueError, TypeError):
            continue
        if not isinstance(data, dict):
            continue
        path = data.get("file_path") or data.get("notebook_path")
        if path and path not in seen:
            seen.add(path)
            files.append(path)
    return files


# ── Checkpoints ───────────────────────────────────────────────────────────────

def write_checkpoint(
    session_id: str,
    step_done: str,
    *,
    step_next: str | None = None,
    files_touched: list[str] | None = None,
    diff_patch: str | None = None,
) -> int:
    conn = db()
    seq = _next_seq(conn, "checkpoints", session_id)
    conn.execute(
        """INSERT INTO checkpoints
               (session_id, seq, timestamp, step_done, step_next, files_touched, diff_patch)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            session_id,
            seq,
            _now(),
            step_done,
            step_next,
            json.dumps(files_touched) if files_touched is not None else None,
            diff_patch,
        ),
    )
    return seq


def get_latest_checkpoint(session_id: str) -> dict[str, Any] | None:
    row = db().execute(
        "SELECT * FROM checkpoints WHERE session_id = ? ORDER BY seq DESC LIMIT 1",
        (session_id,),
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    if result.get("files_touched"):
        result["files_touched"] = json.loads(result["files_touched"])
    return result


def list_checkpoints(session_id: str) -> list[dict[str, Any]]:
    rows = db().execute(
        "SELECT * FROM checkpoints WHERE session_id = ? ORDER BY seq",
        (session_id,),
    ).fetchall()
    result = []
    for row in rows:
        d = dict(row)
        if d.get("files_touched"):
            d["files_touched"] = json.loads(d["files_touched"])
        result.append(d)
    return result


def count_checkpoints(session_id: str) -> int:
    row = db().execute(
        "SELECT COUNT(*) FROM checkpoints WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    return row[0]


# ── Resume data ───────────────────────────────────────────────────────────────

def get_resume_data(session_id: str) -> dict[str, Any] | None:
    """Aggregate all data needed to generate a resume brief."""
    session = get_session(session_id)
    if session is None:
        return None
    checkpoint = get_latest_checkpoint(session_id)
    event_count = count_events(session_id)
    checkpoint_count = count_checkpoints(session_id)
    return {
        "session": session,
        "checkpoint": checkpoint,
        "event_count": event_count,
        "checkpoint_count": checkpoint_count,
    }


# ── Destructive ───────────────────────────────────────────────────────────────

def reset_all() -> None:
    """Drop all data. Requires explicit confirmation in the CLI."""
    conn = db()
    conn.execute("DELETE FROM events")
    conn.execute("DELETE FROM checkpoints")
    conn.execute("DELETE FROM sessions")
