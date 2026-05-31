"""
Claude Replay — HTML trace export.

Renders a recorded session as a single self-contained HTML file (no CDN, no
external assets) — shareable as a file, gist, or link. The template lives at
claude_replay/web/trace.html; this module fills it with rendered markup.
"""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Any

from . import resume, store

WEB_DIR = Path(__file__).resolve().parent / "web"
TEMPLATE_PATH = WEB_DIR / "trace.html"

# How much of a tool input/result preview to show per event.
_PREVIEW_CHARS = 400


def render_html(session_id: str, output_dir: str | Path) -> Path:
    data = store.get_resume_data(session_id)
    if data is None:
        raise ValueError(f"No session found with id: {session_id}")

    session = data["session"]
    events = store.list_events(session_id)
    checkpoints = store.list_checkpoints(session_id)
    brief = resume.generate_brief(session_id)

    template = _load_template()
    title = f"Claude Replay — {session_id[:8]}"
    body = _render_body(session, events, checkpoints, data)

    rendered = (
        template
        .replace("{{TITLE}}", html.escape(title))
        .replace("{{BODY}}", body)
        .replace("{{RESUME_BRIEF}}", html.escape(brief))
    )

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"claude-replay-{session_id[:8]}-{_date_str(session['started_at'])}.html"
    out_path.write_text(rendered, encoding="utf-8")
    return out_path


# ── Template ──────────────────────────────────────────────────────────────────

def _load_template() -> str:
    try:
        return TEMPLATE_PATH.read_text(encoding="utf-8")
    except OSError:
        # Defensive fallback so export never hard-fails on a missing template.
        return (
            "<!DOCTYPE html><html lang=\"en\"><head><meta charset=\"utf-8\">"
            "<title>{{TITLE}}</title></head><body>{{BODY}}"
            "<pre id=\"resume-brief\">{{RESUME_BRIEF}}</pre></body></html>"
        )


# ── Body sections ─────────────────────────────────────────────────────────────

def _render_body(
    session: dict[str, Any],
    events: list[dict[str, Any]],
    checkpoints: list[dict[str, Any]],
    data: dict[str, Any],
) -> str:
    return "\n".join([
        _render_header(session, data),
        _render_timeline(events, checkpoints),
        _render_filemap(session, checkpoints),
    ])


def _render_header(session: dict[str, Any], data: dict[str, Any]) -> str:
    status = session["status"] or "unknown"
    badge = f'<span class="badge status-{html.escape(status)}">{html.escape(status)}</span>'
    rows = [
        ("Objective", session["objective"] or "(not recorded)"),
        ("Model", session["model"] or "(unknown)"),
        ("Project dir", session["project_dir"] or "—"),
        ("Started", session["started_at"] or "—"),
        ("Ended", session["ended_at"] or "—"),
        ("Events", str(data["event_count"])),
        ("Checkpoints", str(data["checkpoint_count"])),
    ]
    if session["error_msg"]:
        rows.append(("Error", session["error_msg"]))
    items = "\n".join(
        f"<dt>{html.escape(k)}</dt><dd>{html.escape(str(v))}</dd>" for k, v in rows
    )
    return (
        "<header>\n"
        f"<h1>Claude Replay Trace</h1>\n"
        f'<div class="sub">{badge}</div>\n'
        f'<dl class="meta">\n{items}\n</dl>\n'
        "</header>"
    )


def _render_timeline(events: list[dict[str, Any]], checkpoints: list[dict[str, Any]]) -> str:
    items: list[tuple[str, str, dict[str, Any]]] = []
    for e in events:
        items.append((e["timestamp"], "event", e))
    for c in checkpoints:
        items.append((c["timestamp"], "checkpoint", c))
    # ISO-8601 strings sort chronologically; events before checkpoints on a tie.
    items.sort(key=lambda x: (x[0], x[1] == "checkpoint"))

    rows = [_render_event(obj) if kind == "event" else _render_checkpoint(obj) for _, kind, obj in items]
    inner = "\n".join(rows) if rows else '<div class="evt"><div class="body">(no events recorded)</div></div>'
    return f'<section><h2>Timeline</h2>\n<div class="timeline">\n{inner}\n</div>\n</section>'


def _render_event(e: dict[str, Any]) -> str:
    etype = e["event_type"] or "event"
    name = e["tool_name"] or etype
    preview = e["tool_input"] or e["tool_result"] or e["error_msg"] or ""
    preview_html = ""
    if preview:
        text = preview[:_PREVIEW_CHARS]
        if len(preview) > _PREVIEW_CHARS:
            text += " …"
        preview_html = f'<pre class="preview">{html.escape(text)}</pre>'
    return (
        f'<div class="evt evt-{html.escape(etype)}">'
        f'<span class="seq">#{e["seq"]}</span>'
        f'<span class="ts">{html.escape(e["timestamp"])}</span>'
        f'<div class="body"><span class="etype">{html.escape(etype)}</span>'
        f'<span class="tname">{html.escape(name)}</span>{preview_html}</div>'
        f'</div>'
    )


def _render_checkpoint(c: dict[str, Any]) -> str:
    done = c["step_done"] or ""
    nxt = f"\n→ next: {c['step_next']}" if c.get("step_next") else ""
    return (
        f'<div class="evt evt-checkpoint">'
        f'<span class="seq">cp{c["seq"]}</span>'
        f'<span class="ts">{html.escape(c["timestamp"])}</span>'
        f'<div class="body"><span class="etype">checkpoint</span>'
        f'<pre class="preview">{html.escape(done + nxt)}</pre></div>'
        f'</div>'
    )


def _render_filemap(session: dict[str, Any], checkpoints: list[dict[str, Any]]) -> str:
    files = store.files_touched(session["id"])
    latest = checkpoints[-1] if checkpoints else None
    diff = latest["diff_patch"] if latest else None

    if files:
        items = "\n".join(f"<li>{html.escape(f)}</li>" for f in files)
        files_html = f'<ul class="files">\n{items}\n</ul>'
    else:
        files_html = "<p>(no files modified)</p>"

    diff_html = ""
    if diff:
        diff_html = (
            "<h2>Diff (latest checkpoint)</h2>\n"
            f'<pre class="diff">{html.escape(diff)}</pre>'
        )
    return f"<section><h2>Files touched</h2>\n{files_html}\n{diff_html}\n</section>"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _date_str(iso: str | None) -> str:
    if not iso:
        return "00000000"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        return dt.strftime("%Y%m%d")
    except ValueError:
        return "00000000"
