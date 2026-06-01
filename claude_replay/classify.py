"""
Claude Replay — death-cause classification.

Infers *why* a session ended from its recorded tail, turning the flat
'running' / 'completed' status into something actionable on a resume. Before
this, every interrupted session looked identical ("interrupted"); now a rate
limit, a context overflow, and a clean finish are told apart.

Pure functions over a session row + its event list — NO DB access lives here,
so it's trivially testable and safe to call from resume / cli / server alike.
The single source of truth for death cause; everything that displays it calls
`classify()` rather than re-deriving.
"""

from __future__ import annotations

import json
from typing import Any

# cause key → human-facing label
LABELS: dict[str, str] = {
    "clean_finish": "Clean finish",
    "interrupted": "Interrupted",
    "rate_limit": "Rate limited",
    "context_overflow": "Context overflow",
    "api_error": "API error",
    "never_started": "No activity recorded",
    "unknown": "Unknown",
}

# Substrings that point at a specific failure, checked in priority order and
# matched against EXPLICIT error text only (session.error_msg / event.error_msg)
# — never raw tool output, so normal results mentioning "rate limit" can't
# trigger a false positive. Compared lower-cased.
_SIGNATURES: list[tuple[tuple[str, ...], str]] = [
    (("rate limit", "rate_limit", "429", "too many requests"), "rate_limit"),
    (
        (
            "context length",
            "context_length",
            "prompt is too long",
            "maximum context",
            "context window",
            "too many tokens",
        ),
        "context_overflow",
    ),
    (
        (
            "overloaded",
            "internal server error",
            "service unavailable",
            "api error",
            "bad gateway",
            "529",
            "503",
            "502",
        ),
        "api_error",
    ),
]


def classify(session: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Return ``{'cause', 'label', 'detail'}`` describing why a session ended.

    `detail` is a short human string (the matched error, or None) suitable for
    a resume brief; `cause` is a stable key from `LABELS`.
    """
    status = (session.get("status") or "").lower()
    explicit = _explicit_error(session, events)

    # 1. An explicit error message is the strongest signal — match it.
    if explicit:
        cause = _match_signature(explicit) or "api_error"
        return _result(cause, explicit)

    # 2. status drives the rest.
    if status == "completed":
        return _result("clean_finish", None)
    if status == "error":
        return _result("unknown", None)
    if not events:
        return _result("never_started", None)

    # 3. Still 'running' with activity → cut off mid-flight. Surface the last
    #    tool error (if any) as detail without over-claiming it as the cause.
    return _result("interrupted", last_error(events))


def event_is_error(event: dict[str, Any]) -> bool:
    """Whether a single event represents a tool/agent error. Strict — used for
    counting, so a normal result with incidental stderr does NOT count. True
    only on an explicit `error_msg`, `is_error: true`, or a top-level `error`."""
    if event.get("error_msg"):
        return True
    raw = event.get("tool_result")
    if not raw:
        return False
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return False
    return isinstance(data, dict) and (data.get("is_error") is True or bool(data.get("error")))


def last_error(events: list[dict[str, Any]]) -> str | None:
    """Best human string for the most recent recorded error, or None.

    Looks at explicit `error_msg` fields first, then a JSON error marker in the
    last tool result (`is_error: true` or a top-level `error`/`stderr`)."""
    for event in reversed(events):
        if event.get("error_msg"):
            return str(event["error_msg"]).strip() or None
    err = _tool_result_error(events)
    if err:
        tool, text = err
        snippet = text.strip().splitlines()[0][:160] if text.strip() else ""
        return f"{tool}: {snippet}" if snippet else f"{tool} reported an error"
    return None


# ── internals ─────────────────────────────────────────────────────────────────

def _result(cause: str, detail: str | None) -> dict[str, Any]:
    return {"cause": cause, "label": LABELS.get(cause, LABELS["unknown"]), "detail": detail}


def _explicit_error(session: dict[str, Any], events: list[dict[str, Any]]) -> str | None:
    if session.get("error_msg"):
        return str(session["error_msg"]).strip() or None
    for event in reversed(events):
        if event.get("error_msg"):
            return str(event["error_msg"]).strip() or None
    return None


def _match_signature(text: str) -> str | None:
    low = text.lower()
    for needles, cause in _SIGNATURES:
        if any(n in low for n in needles):
            return cause
    return None


def _tool_result_error(events: list[dict[str, Any]]) -> tuple[str, str] | None:
    """Parse the last tool result; return (tool_name, error_text) if it carries
    a structured error marker, else None. Substring noise is ignored — only
    explicit JSON error fields count."""
    for event in reversed(events):
        raw = event.get("tool_result")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, dict):
            return None
        is_error = data.get("is_error") is True
        err = data.get("error") or data.get("stderr")
        if is_error or err:
            tool = event.get("tool_name") or "tool"
            text = err if isinstance(err, str) else json.dumps(err) if err else ""
            return (tool, text)
        return None
    return None
