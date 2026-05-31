"""
Claude Replay — Starlette app: MCP SSE tools + JSON API + static dashboard.

A thin read/write surface over the SQLite store. All persistence lives in
store.py; this module exposes it three ways:
  - Five `replay_*` MCP tools over SSE at /sse (for Claude Code agents)
  - A JSON HTTP API under /api/* (for the dashboard or any client)
  - A static dashboard at / (claude_replay/web/index.html)

Run: `claude-replay serve` (port 8766 — Bridge is 8765, deliberately different).
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
    return {
        "id": s["id"],
        "id_short": s["id"][:8],
        "objective": s["objective"],
        "status": s["status"],
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
            description="Render a session as a self-contained HTML trace and return the output path.",
            inputSchema={
                "type": "object",
                "properties": {
                    "session_id": {"type": "string", "description": "Session ID (default: most recent)"},
                    "output": {"type": "string", "description": "Output directory (default: ~/.claude-replay/exports)"},
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
        data = store.get_resume_data(sid)
        s = data["session"]
        text = (
            f"✓ Claude Replay — current session\n"
            f"  Session:     {s['id']}\n"
            f"  Objective:   {s['objective'] or '(not recorded)'}\n"
            f"  Status:      {s['status']}\n"
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
            path = export.render_html(sid, output)
        except ValueError as e:
            return [TextContent(type="text", text=f"error: {e}")]
        return [TextContent(type="text", text=f"✓ Exported trace → {path}")]

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
    sid = request.path_params["session_id"]
    session = store.get_session(sid)
    if session is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({
        "session": session,
        "events": store.list_events(sid),
        "checkpoints": store.list_checkpoints(sid),
    })


async def api_resume(request: Request) -> JSONResponse:
    sid = request.path_params["session_id"]
    if store.get_session(sid) is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse({"session_id": sid, "brief": resume.generate_brief(sid)})


async def api_export(request: Request) -> JSONResponse:
    sid = request.path_params["session_id"]
    output = request.query_params.get("output") or str(Path.home() / ".claude-replay" / "exports")
    try:
        path = export.render_html(sid, output)
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


# ── App ───────────────────────────────────────────────────────────────────────

_routes = [
    Route("/status", endpoint=http_status),
    Route("/api/state", endpoint=api_state),
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
