"""
Claude Replay — Starlette app: MCP SSE tools + JSON API + static dashboard.

A thin read/write surface over the SQLite store. All persistence lives in
store.py; this module exposes it several ways:
  - Ten `replay_*` MCP tools over SSE at /sse (for Claude Code agents)
  - The same tools over stdio via run_stdio (for `uvx claude-replay mcp` / any client)
  - A JSON HTTP API under /api/* (for the dashboard or any client)
  - A static dashboard at / (claude_replay/web/index.html)

Run: `claude-replay serve` (port 8766 — Bridge is 8765, deliberately different)
or `claude-replay mcp` (stdio).
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anyio
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.types import TextContent, Tool
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import export, resume, store
from .store import VERSION

SERVER_STARTED_AT = datetime.now(timezone.utc)
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# Serialize manual-checkpoint writes so the per-session seq stays monotonic
# under concurrent API/MCP calls (the hook path is single-process per call,
# so it doesn't need this — only the server's async surface does).
_write_lock = asyncio.Lock()

# CORS: default allows only localhost/127.0.0.1/::1 on any port. Add origins
# via CLAUDE_REPLAY_CORS_ORIGIN (comma-separated). No wildcard default.
_CORS_ORIGIN_ENV = os.environ.get("CLAUDE_REPLAY_CORS_ORIGIN", "").strip()
CORS_EXTRA_ORIGINS = (
    [o.strip() for o in _CORS_ORIGIN_ENV.split(",") if o.strip()]
    if _CORS_ORIGIN_ENV
    else []
)
CORS_LOCALHOST_REGEX = r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"


# ── Helpers ───────────────────────────────────────────────────────────────────

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def uptime_seconds() -> int:
    return int((datetime.now(timezone.utc) - SERVER_STARTED_AT).total_seconds())


def format_uptime(s: int) -> str:
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{sec:02d}"


def _duration(started: str | None, ended: str | None) -> str:
    if not started or not ended:
        return "—"
    try:
        a = datetime.fromisoformat(started.replace("Z", "+00:00"))
        b = datetime.fromisoformat(ended.replace("Z", "+00:00"))
        secs = int((b - a).total_seconds())
        if secs < 0:
            return "—"
        return format_uptime(secs)
    except ValueError:
        return "—"


def _latest_session_id() -> str | None:
    sessions = store.list_sessions(1)
    return sessions[0]["id"] if sessions else None


async def _manual_checkpoint(session_id: str, note: str | None) -> int:
    """Write a checkpoint on demand (from the MCP tool or the JSON API)."""
    events = store.list_events(session_id)
    tool_calls = [e for e in events if e["event_type"] == "tool_result"]
    done = note or f"Manual checkpoint at {len(tool_calls)} tool calls."
    async with _write_lock:
        return store.write_checkpoint(
            session_id, done, files_touched=store.files_touched(session_id) or None
        )


def _session_summary(s: dict[str, Any]) -> dict[str, Any]:
    from . import classify

    death = classify.classify(s, store.list_events(s["id"]))
    return {
        "id": s["id"],
        "id_short": s["id"][:8],
        "objective": s["objective"],
        "name": s.get("name"),
        "tags": s.get("tags", []),
        "status": s["status"],
        "death_cause": death["cause"],
        "death_label": death["label"],
        "model": s["model"],
        "project_dir": s["project_dir"],
        "started_at": s["started_at"],
        "ended_at": s["ended_at"],
        "duration": _duration(s["started_at"], s["ended_at"]),
        "events": store.count_events(s["id"]),
        "checkpoints": store.count_checkpoints(s["id"]),
    }


# ── MCP Server ────────────────────────────────────────────────────────────────

server = Server("claude-replay")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="replay_status",
            description="Current session summary: objective, status, checkpoint/event counts, last activity.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="replay_checkpoint",
            description="Force a checkpoint of the current session right now, with an optional note.",
            inputSchema={
                "type": "object",
                "properties": {
                    "note": {"type": "string", "description": "Optional note describing the checkpoint"},
                },
            },
        ),
        Tool(
            name="replay_resume",
            description=(
                "Generate a structured resume brief for a session (default: the most recent). "
                "Paste the result into a new Claude Code session to continue where it left off."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Session ID (default: most recent)"},
                },
            },
        ),
        Tool(
            name="replay_sessions",
            description="List recent sessions with status, model, duration, and checkpoint count.",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max sessions to list (default: 10)", "default": 10},
                },
            },
        ),
        Tool(
            name="replay_export",
            description="Render a session as a self-contained trace (html, json, or md) and return the output path.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Session ID (default: most recent)"},
                    "output": {"type": "string", "description": "Output directory (default: ~/.claude-replay/exports)"},
                    "format": {"type": "string", "enum": ["html", "json", "md"], "description": "Export format (default: html)"},
                },
            },
        ),
        Tool(
            name="replay_search",
            description=(
                "Full-text search across recorded sessions (event payloads, objective, "
                "name, and tags), with optional filters. Ranked by match count. Omit the "
                "query to browse by filters alone."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Text to search for (optional if filtering)"},
                    "limit": {"type": "integer", "description": "Max sessions (default: 20)", "default": 20},
                    "tool": {"type": "string", "description": "Only sessions that used this tool"},
                    "cause": {"type": "string", "description": "Only sessions with this death cause"},
                    "since": {"type": "string", "description": "Only sessions started after this ISO date"},
                    "until": {"type": "string", "description": "Only sessions started before this ISO date"},
                    "project": {"type": "string", "description": "Only sessions whose project dir contains this"},
                },
            },
        ),
        Tool(
            name="replay_tag",
            description=(
                "Name or tag a session for later retrieval. Sets a name and/or adds/removes "
                "tags on a session (default: the most recent)."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Session ID (default: most recent)"},
                    "name": {"type": "string", "description": "Human-friendly name"},
                    "add": {"type": "array", "items": {"type": "string"}, "description": "Tags to add"},
                    "remove": {"type": "array", "items": {"type": "string"}, "description": "Tags to remove"},
                },
            },
        ),
        Tool(
            name="replay_insights",
            description=(
                "Per-session insight metrics: how it ended, duration, tool-call count, "
                "error count/rate, files touched, and the most-used tools."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Session ID (default: most recent)"},
                },
            },
        ),
        Tool(
            name="replay_diff",
            description=(
                "Compare two sessions side by side: metric deltas (tool calls, errors, "
                "duration, files) and which files each touched."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "session_a": {"type": "string", "description": "First session ID"},
                    "session_b": {"type": "string", "description": "Second session ID"},
                },
                "required": ["session_a", "session_b"],
            },
        ),
        Tool(
            name="replay_stats",
            description=(
                "Cross-session analytics across all recorded sessions: total tool calls, "
                "overall error rate, why sessions end (death-cause breakdown), the tool mix, "
                "and per-project rollups. Optional limit / project filter."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Only the N most recent sessions (default: all)"},
                    "project": {"type": "string", "description": "Only sessions whose project dir contains this"},
                },
            },
        ),
    ]


async def dispatch_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Plain dispatcher — also exposed for tests so they don't depend on the
    MCP @server.call_tool() decorator (the Bridge pattern)."""

    if name == "replay_status":
        sid = _latest_session_id()
        if sid is None:
            return [TextContent(type="text", text="No sessions recorded yet.")]
        from . import classify

        data = store.get_resume_data(sid)
        s = data["session"]
        death = classify.classify(s, store.list_events(sid))
        text = (
            f"✓ Claude Replay — current session\n"
            f"  Session:     {s['id']}\n"
            f"  Objective:   {s['objective'] or '(not recorded)'}\n"
            f"  Status:      {s['status']}\n"
            f"  How it ended:{' ' + death['label']}\n"
            f"  Model:       {s['model'] or '(unknown)'}\n"
            f"  Events:      {data['event_count']}\n"
            f"  Checkpoints: {data['checkpoint_count']}\n"
            f"  Started:     {s['started_at']}\n"
            f"  Ended:       {s['ended_at'] or '— (still running / interrupted)'}"
        )
        return [TextContent(type="text", text=text)]

    if name == "replay_checkpoint":
        sid = _latest_session_id()
        if sid is None:
            return [TextContent(type="text", text="No active session to checkpoint.")]
        seq = await _manual_checkpoint(sid, arguments.get("note"))
        return [TextContent(type="text", text=f"✓ Checkpoint #{seq} written to {sid[:8]}")]

    if name == "replay_resume":
        sid = arguments.get("session_id") or _latest_session_id()
        if sid is None:
            return [TextContent(type="text", text="No sessions recorded yet.")]
        return [TextContent(type="text", text=resume.generate_brief(sid))]

    if name == "replay_sessions":
        limit = int(arguments.get("limit", 10))
        sessions = store.list_sessions(limit)
        if not sessions:
            return [TextContent(type="text", text="No sessions recorded yet.")]
        lines = [f"Recent sessions ({len(sessions)}):"]
        for s in sessions:
            lines.append(
                f"  • {s['id'][:8]}  [{s['status']}]  {s['model'] or '—'}  "
                f"{store.count_checkpoints(s['id'])} ckpt  "
                f"{store.count_events(s['id'])} events  ({s['started_at']})"
            )
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "replay_export":
        sid = arguments.get("session_id") or _latest_session_id()
        if sid is None:
            return [TextContent(type="text", text="No sessions recorded yet.")]
        output = arguments.get("output") or str(Path.home() / ".claude-replay" / "exports")
        try:
            path = export.render(sid, output, arguments.get("format") or "html")
        except ValueError as e:
            return [TextContent(type="text", text=f"error: {e}")]
        return [TextContent(type="text", text=f"✓ Exported trace → {path}")]

    if name == "replay_search":
        query = str(arguments.get("query") or "").strip()
        filters = {k: arguments.get(k) for k in ("tool", "cause", "since", "until", "project")}
        if not query and not any(filters.values()):
            return [TextContent(type="text", text="Provide a query or at least one filter.")]
        results = store.search(query, int(arguments.get("limit", 20)), **filters)
        label = f"'{query}'" if query else "those filters"
        if not results:
            return [TextContent(type="text", text=f"No matches for {label}.")]
        lines = [f"{len(results)} session(s) match {label}:"]
        for r in results:
            s = r["session"]
            label = s["name"] or s["objective"] or "(no objective)"
            lines.append(f"  • {s['id'][:8]}  [{s['status']}]  {r['matches']} match  {label[:60]}")
            if r["snippet"]:
                lines.append(f"      … {r['snippet']}")
        return [TextContent(type="text", text="\n".join(lines))]

    if name == "replay_tag":
        sid = arguments.get("session_id") or _latest_session_id()
        if sid is None:
            return [TextContent(type="text", text="No sessions recorded yet.")]
        if store.get_session(sid) is None:
            return [TextContent(type="text", text=f"error: no session with id: {sid}")]
        if arguments.get("name") is not None:
            store.set_name(sid, str(arguments["name"]))
        if arguments.get("add"):
            store.add_tags(sid, [str(t) for t in arguments["add"]])
        if arguments.get("remove"):
            store.remove_tags(sid, [str(t) for t in arguments["remove"]])
        s = store.get_session(sid)
        tags = ", ".join(s["tags"]) or "—"
        return [TextContent(type="text", text=f"✓ {sid[:8]}  name: {s['name'] or '—'}  tags: {tags}")]

    if name == "replay_insights":
        from . import metrics

        sid = arguments.get("session_id") or _latest_session_id()
        if sid is None:
            return [TextContent(type="text", text="No sessions recorded yet.")]
        session = store.get_session(sid)
        if session is None:
            return [TextContent(type="text", text=f"error: no session with id: {sid}")]
        m = metrics.compute(session, store.list_events(sid))
        tools = ", ".join(f"{n}×{c}" for n, c in m["top_tools"]) or "—"
        text = (
            f"Insights for {sid[:8]}:\n"
            f"  How it ended: {m['death_label']}\n"
            f"  Duration:     {m['duration_human']}\n"
            f"  Tool calls:   {m['tool_calls']}  ({m['error_count']} errors, {m['error_rate']:.0%})\n"
            f"  Files touched: {m['files_touched']}\n"
            f"  Top tools:    {tools}"
        )
        return [TextContent(type="text", text=text)]

    if name == "replay_diff":
        cmp = store.compare(arguments.get("session_a", ""), arguments.get("session_b", ""))
        if cmp is None:
            return [TextContent(type="text", text="error: one or both sessions not found")]
        a, b, d = cmp["a"], cmp["b"], cmp["deltas"]
        text = (
            f"Compare {a['session']['id'][:8]} (A) vs {b['session']['id'][:8]} (B):\n"
            f"  how it ended: {a['metrics']['death_label']}  →  {b['metrics']['death_label']}\n"
            f"  tool calls:   {a['metrics']['tool_calls']}  →  {b['metrics']['tool_calls']}  (Δ {d['tool_calls']:+})\n"
            f"  errors:       {a['metrics']['error_count']}  →  {b['metrics']['error_count']}  (Δ {d['error_count']:+})\n"
            f"  files:        {a['metrics']['files_touched']}  →  {b['metrics']['files_touched']}  (Δ {d['files_touched']:+})\n"
            f"  only in A:    {', '.join(cmp['files']['only_a']) or '—'}\n"
            f"  only in B:    {', '.join(cmp['files']['only_b']) or '—'}"
        )
        return [TextContent(type="text", text=text)]

    if name == "replay_stats":
        from . import analytics

        limit = arguments.get("limit")
        items = store.sessions_with_events(int(limit) if limit else None)
        project = arguments.get("project")
        if project:
            needle = str(project).lower()
            items = [(s, e) for s, e in items if needle in (s.get("project_dir") or "").lower()]
        roll = analytics.aggregate(items)
        if roll["session_count"] == 0:
            return [TextContent(type="text", text="No sessions recorded yet.")]
        causes = ", ".join(f"{lbl}×{c}" for lbl, c in roll["death_causes"]) or "—"
        mix = ", ".join(f"{t}×{c}" for t, c in roll["tool_mix"][:6]) or "—"
        projects = "; ".join(
            f"{p['project'].rsplit('/', 1)[-1].rsplit(chr(92), 1)[-1]} "
            f"({p['sessions']}s, {p['error_rate']:.0%} err)"
            for p in roll["projects"][:5]
        ) or "—"
        text = (
            f"Analytics across {roll['session_count']} sessions:\n"
            f"  Tool calls:   {roll['total_tool_calls']}  (avg {roll['avg_tool_calls']}/session)\n"
            f"  Error rate:   {roll['overall_error_rate']:.0%}\n"
            f"  Why they end: {causes}\n"
            f"  Tool mix:     {mix}\n"
            f"  By project:   {projects}"
        )
        return [TextContent(type="text", text=text)]

    return [TextContent(type="text", text=f"Unknown tool: {name}")]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    return await dispatch_tool(name, arguments)


# ── HTTP / JSON API ───────────────────────────────────────────────────────────

async def http_status(request: Request) -> JSONResponse:
    return JSONResponse({
        "service": "claude-replay",
        "status": "online",
        "version": VERSION,
        "server_time": utc_now_iso(),
    })


async def api_state(request: Request) -> JSONResponse:
    sessions = [_session_summary(s) for s in store.list_sessions(100)]
    up = uptime_seconds()
    return JSONResponse({
        "service": "claude-replay",
        "status": "online",
        "version": VERSION,
        "uptime_seconds": up,
        "uptime_human": format_uptime(up),
        "total_sessions": len(sessions),
        "sessions": sessions,
        "server_time": utc_now_iso(),
    })


async def api_session(request: Request) -> JSONResponse:
    from . import classify, metrics

    sid = request.path_params["session_id"]
    session = store.get_session(sid)
    if session is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    events = store.list_events(sid)
    death = classify.classify(session, events)
    session = {**session, "death_cause": death["cause"], "death_label": death["label"]}
    return JSONResponse({
        "session": session,
        "events": events,
        "checkpoints": store.list_checkpoints(sid),
        "metrics": metrics.compute(session, events),
    })


async def api_search(request: Request) -> JSONResponse:
    qp = request.query_params
    query = qp.get("q", "").strip()
    try:
        limit = int(qp.get("limit", 20))
    except (TypeError, ValueError):
        limit = 20
    filters = {k: (qp.get(k) or None) for k in ("tool", "cause", "since", "until", "project")}
    results = store.search(query, limit, **filters) if (query or any(filters.values())) else []
    return JSONResponse({
        "query": query,
        "count": len(results),
        "results": [
            {"session": _session_summary(r["session"]),
             "matches": r["matches"], "snippet": r["snippet"]}
            for r in results
        ],
    })


async def api_diff(request: Request) -> JSONResponse:
    cmp = store.compare(request.query_params.get("a", ""), request.query_params.get("b", ""))
    if cmp is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(cmp)


async def api_stats(request: Request) -> JSONResponse:
    from . import analytics

    qp = request.query_params
    try:
        limit = int(qp["limit"]) if qp.get("limit") else None
    except (TypeError, ValueError):
        limit = None
    items = store.sessions_with_events(limit)
    project = qp.get("project")
    if project:
        needle = project.lower()
        items = [(s, e) for s, e in items if needle in (s.get("project_dir") or "").lower()]
    return JSONResponse(analytics.aggregate(items))


async def api_resume(request: Request) -> JSONResponse:
    sid = request.path_params["session_id"]
    if store.get_session(sid) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"session_id": sid, "brief": resume.generate_brief(sid)})


async def api_export(request: Request) -> JSONResponse:
    sid = request.path_params["session_id"]
    output = request.query_params.get("output") or str(Path.home() / ".claude-replay" / "exports")
    fmt = request.query_params.get("format") or "html"
    try:
        path = export.render(sid, output, fmt)
    except ValueError:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"session_id": sid, "path": str(path)})


async def api_checkpoint(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except (ValueError, TypeError):
        return JSONResponse({"error": "invalid JSON body"}, status_code=400)
    sid = body.get("session_id") or _latest_session_id()
    if sid is None:
        return JSONResponse({"error": "no active session"}, status_code=400)
    if store.get_session(sid) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    seq = await _manual_checkpoint(sid, body.get("note"))
    return JSONResponse({"session_id": sid, "seq": seq})


# ── MCP SSE transport ─────────────────────────────────────────────────────────

sse_transport = SseServerTransport("/messages/")


async def handle_sse(request: Request):
    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await server.run(streams[0], streams[1], server.create_initialization_options())
    return Response()


async def handle_post_message(scope, receive, send):
    try:
        await sse_transport.handle_post_message(scope, receive, send)
    except (anyio.ClosedResourceError, anyio.BrokenResourceError):
        pass


# ── MCP stdio transport ───────────────────────────────────────────────────────

async def run_stdio() -> None:
    """Serve the same MCP tools over stdio, so any MCP client can launch Replay
    directly (e.g. `uvx claude-replay mcp`) without the HTTP server. stdout is
    the protocol channel here — nothing else may write to it."""
    from mcp.server.stdio import stdio_server

    store.db()  # ensure the DB exists before the first tool call
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


# ── App ───────────────────────────────────────────────────────────────────────

_routes = [
    Route("/status", endpoint=http_status),
    Route("/api/state", endpoint=api_state),
    Route("/api/search", endpoint=api_search),
    Route("/api/diff", endpoint=api_diff),
    Route("/api/stats", endpoint=api_stats),
    Route("/api/session/{session_id}", endpoint=api_session),
    Route("/api/resume/{session_id}", endpoint=api_resume),
    Route("/api/export/{session_id}", endpoint=api_export),
    Route("/api/checkpoint", endpoint=api_checkpoint, methods=["POST"]),
    Route("/sse", endpoint=handle_sse),
    Mount("/messages/", app=handle_post_message),
]
if os.path.isdir(WEB_DIR) and not os.environ.get("CLAUDE_REPLAY_NO_DASHBOARD"):
    # Catch-all static mount goes LAST so it doesn't shadow API routes.
    _routes.append(Mount("/", app=StaticFiles(directory=WEB_DIR, html=True)))

_cors_kwargs: dict[str, object] = {
    "allow_methods": ["GET", "POST", "OPTIONS"],
    "allow_headers": ["Authorization", "Content-Type"],
}
if CORS_EXTRA_ORIGINS:
    _cors_kwargs["allow_origins"] = CORS_EXTRA_ORIGINS
else:
    _cors_kwargs["allow_origin_regex"] = CORS_LOCALHOST_REGEX

app = Starlette(
    routes=_routes,
    middleware=[Middleware(CORSMiddleware, **_cors_kwargs)],
)
