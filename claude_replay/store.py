"""
Claude Replay — SQLite store.

All database access lives here. Nothing else in this package calls sqlite3
directly. Schema: three tables (sessions, checkpoints, events) plus an FTS5
index (events_fts) kept in sync by triggers for full-text search. Older DBs are
migrated on open (name/tags columns added, FTS backfilled). WAL mode,
synchronous=NORMAL — survives process crashes without corruption.

Default DB location: ~/.claude-replay/sessions.db
Override:           CLAUDE_REPLAY_DB environment variable
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

VERSION = "0.1.0"

# Whether the SQLite build has FTS5. Set during schema setup; search() falls
# back to a LIKE scan when it's False, so search works on any build.
_FTS_ENABLED = False

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
        _migrate(_conn)
        _setup_fts(_conn)
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
            error_msg   TEXT,
            name        TEXT,
            tags        TEXT
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


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring an older DB up to the current schema. CREATE TABLE IF NOT EXISTS
    never adds columns to a table that already exists, so a 0.1.0 sessions
    table needs the new columns added explicitly (idempotent)."""
    have = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
    for column in ("name", "tags"):
        if column not in have:
            conn.execute(f"ALTER TABLE sessions ADD COLUMN {column} TEXT")


def _setup_fts(conn: sqlite3.Connection) -> None:
    """Create the FTS5 index over events + keep-in-sync triggers, backfilling
    existing rows on first creation. Best-effort: a SQLite build without FTS5
    leaves `_FTS_ENABLED` False and search() falls back to a LIKE scan."""
    global _FTS_ENABLED
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events_fts'"
    ).fetchone()
    try:
        if not exists:
            conn.executescript("""
                CREATE VIRTUAL TABLE events_fts USING fts5(
                    tool_name, tool_input, tool_result,
                    content='events', content_rowid='id'
                );
                INSERT INTO events_fts(rowid, tool_name, tool_input, tool_result)
                    SELECT id, tool_name, tool_input, tool_result FROM events;

                CREATE TRIGGER events_fts_ai AFTER INSERT ON events BEGIN
                    INSERT INTO events_fts(rowid, tool_name, tool_input, tool_result)
                    VALUES (new.id, new.tool_name, new.tool_input, new.tool_result);
                END;
                CREATE TRIGGER events_fts_ad AFTER DELETE ON events BEGIN
                    INSERT INTO events_fts(events_fts, rowid, tool_name, tool_input, tool_result)
                    VALUES ('delete', old.id, old.tool_name, old.tool_input, old.tool_result);
                END;
            """)
        _FTS_ENABLED = True
    except sqlite3.OperationalError:
        _FTS_ENABLED = False


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
        "name": None,
        "tags": [],
    }


def _row_to_session(row: sqlite3.Row | None) -> dict[str, Any] | None:
    """Turn a sessions row into a dict, decoding the JSON `tags` column into a
    list so callers (and the JSON API) never see the raw stored string."""
    if row is None:
        return None
    data = dict(row)
    raw = data.get("tags")
    if raw:
        try:
            data["tags"] = json.loads(raw)
        except (ValueError, TypeError):
            data["tags"] = []
    else:
        data["tags"] = []
    return data


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
    return _row_to_session(row)


def list_sessions(limit: int = 20) -> list[dict[str, Any]]:
    rows = db().execute(
        "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_row_to_session(r) for r in rows]


# ── Naming & tagging ──────────────────────────────────────────────────────────

def set_name(session_id: str, name: str | None) -> None:
    """Set (or clear, with None/empty) a session's human-friendly name."""
    db().execute(
        "UPDATE sessions SET name = ? WHERE id = ?",
        ((name or None), session_id),
    )


def set_tags(session_id: str, tags: list[str]) -> list[str]:
    """Replace a session's tags with `tags` (deduped, order-preserved). Returns
    the stored list."""
    cleaned = _clean_tags(tags)
    db().execute(
        "UPDATE sessions SET tags = ? WHERE id = ?",
        ((json.dumps(cleaned) if cleaned else None), session_id),
    )
    return cleaned


def get_tags(session_id: str) -> list[str]:
    session = get_session(session_id)
    return session["tags"] if session else []


def add_tags(session_id: str, tags: list[str]) -> list[str]:
    """Merge `tags` into the session's existing tags. Returns the merged list."""
    return set_tags(session_id, get_tags(session_id) + list(tags))


def remove_tags(session_id: str, tags: list[str]) -> list[str]:
    drop = {t.strip().lower() for t in tags if t.strip()}
    keep = [t for t in get_tags(session_id) if t.lower() not in drop]
    return set_tags(session_id, keep)


def _clean_tags(tags: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for tag in tags:
        norm = str(tag).strip()
        if norm and norm.lower() not in seen:
            seen.add(norm.lower())
            out.append(norm)
    return out


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


# ── Search ────────────────────────────────────────────────────────────────────

def search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    """Full-text search across event payloads + session metadata.

    Returns sessions ranked by match count (recency breaks ties), each as
    ``{'session', 'matches', 'snippet'}``. Uses FTS5 when available, else a
    LIKE scan — same results, just slower on a large DB."""
    q = query.strip()
    if not q:
        return []
    conn = db()
    hits: dict[str, dict[str, Any]] = {}

    if _FTS_ENABLED:
        try:
            rows = conn.execute(
                """SELECT e.session_id AS sid,
                          snippet(events_fts, -1, '«', '»', '…', 10) AS snip
                   FROM events_fts
                   JOIN events e ON e.id = events_fts.rowid
                   WHERE events_fts MATCH ?
                   ORDER BY rank""",
                (_fts_query(q),),
            ).fetchall()
            for r in rows:
                _record_hit(hits, r["sid"], r["snip"])
        except sqlite3.OperationalError:
            pass  # an unparsable FTS expression — fall through to LIKE below
    if not _FTS_ENABLED or not hits:
        like = f"%{q}%"
        rows = conn.execute(
            """SELECT session_id AS sid, tool_input, tool_result FROM events
               WHERE tool_name LIKE ? OR tool_input LIKE ? OR tool_result LIKE ?""",
            (like, like, like),
        ).fetchall()
        for r in rows:
            _record_hit(hits, r["sid"], r["tool_input"] or r["tool_result"])

    # Session metadata (objective / name / tags) matches too — short fields, LIKE.
    like = f"%{q.lower()}%"
    for r in conn.execute(
        """SELECT id FROM sessions
           WHERE lower(COALESCE(objective, '')) LIKE ?
              OR lower(COALESCE(name, ''))      LIKE ?
              OR lower(COALESCE(tags, ''))      LIKE ?""",
        (like, like, like),
    ).fetchall():
        hits.setdefault(r["id"], {"matches": 0, "snippet": None})

    results: list[dict[str, Any]] = []
    for sid, info in hits.items():
        session = get_session(sid)
        if session is not None:
            results.append(
                {"session": session, "matches": info["matches"], "snippet": info["snippet"]}
            )
    results.sort(
        key=lambda r: (r["matches"], r["session"]["started_at"] or ""), reverse=True
    )
    return results[:limit]


def _record_hit(hits: dict[str, dict[str, Any]], sid: str, snippet: Any) -> None:
    entry = hits.setdefault(sid, {"matches": 0, "snippet": None})
    entry["matches"] += 1
    if entry["snippet"] is None and snippet:
        text = str(snippet).replace("\n", " ").strip()
        entry["snippet"] = text[:200] if text else None


def _fts_query(query: str) -> str:
    """Turn free text into a safe FTS5 MATCH expression: each whitespace token
    becomes a quoted term (ANDed), so user input can't trip FTS5 operators."""
    tokens = [t for t in re.split(r"\s+", query.strip()) if t]
    return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)


# ── Retention ─────────────────────────────────────────────────────────────────

def prune(older_than_days: int, *, vacuum: bool = True) -> int:
    """Delete sessions whose last activity is older than `older_than_days`, with
    their events + checkpoints. Returns the number of sessions removed. Runs a
    VACUUM afterwards (unless disabled) so the file actually shrinks."""
    if older_than_days < 0:
        raise ValueError("older_than_days must be >= 0")
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=older_than_days)
    ).isoformat(timespec="seconds").replace("+00:00", "Z")
    conn = db()
    ids = [
        row[0]
        for row in conn.execute(
            "SELECT id FROM sessions WHERE COALESCE(ended_at, started_at) < ?",
            (cutoff,),
        ).fetchall()
    ]
    for sid in ids:
        conn.execute("DELETE FROM events WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM checkpoints WHERE session_id = ?", (sid,))
        conn.execute("DELETE FROM sessions WHERE id = ?", (sid,))
    if vacuum and ids:
        conn.execute("VACUUM")
    return len(ids)


# ── Destructive ───────────────────────────────────────────────────────────────

def reset_all() -> None:
    """Drop all data. Requires explicit confirmation in the CLI."""
    conn = db()
    conn.execute("DELETE FROM events")  # AFTER DELETE trigger keeps events_fts in sync
    conn.execute("DELETE FROM checkpoints")
    conn.execute("DELETE FROM sessions")
