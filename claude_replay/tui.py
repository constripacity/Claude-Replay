"""Claude Replay — Terminal UI.

A Textual-based companion to the web dashboard. Connects to a running
`claude-replay serve` over the same JSON API the dashboard uses (defaults to
http://127.0.0.1:8766) and provides a live session browser: a session list,
an event feed, a checkpoint/detail inspector, and one-key resume + export.

Usage:
    python -m claude_replay.tui                  # connect to 127.0.0.1:8766
    python -m claude_replay.tui --url http://... # point at a remote server
    claude-replay tui                            # same, via the CLI

Keys:  ↑↓ navigate · Tab switch panel · r resume (copy brief) · e export
       space pause · q quit · ? help

It talks HTTP to the server, never the DB directly — same separation Bridge
uses (tui_client.py is the client, tui.py is the view). The server must be
running first.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

import httpx
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import (
    DataTable,
    Footer,
    Label,
    ListItem,
    ListView,
    Static,
)

from .tui_client import (
    ReplayClient,
    ReplayError,
    death_color,
    event_color,
    event_glyph,
    short_id,
    status_color,
    status_glyph,
)

POLL_INTERVAL = 2.0
DEFAULT_URL = "http://127.0.0.1:8766"


def _short_uptime(seconds: int) -> str:
    """Coarsest still-useful unit, so the top bar doesn't tick every poll."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    rem = minutes % 60
    if hours < 24:
        return f"{hours}h{rem:02d}m" if rem else f"{hours}h"
    days = hours // 24
    rem_h = hours % 24
    return f"{days}d{rem_h}h" if rem_h else f"{days}d"


def _event_detail(event: dict[str, Any]) -> str:
    """One-line summary of an event for the feed's DETAIL column."""
    if event.get("error_msg"):
        return str(event["error_msg"])
    raw = event.get("tool_input") or event.get("tool_result") or ""
    return raw.replace("\n", " ").strip() if isinstance(raw, str) else str(raw)


# ── Widgets ──────────────────────────────────────────────────────────────────

class TopBar(Static):
    """Top status bar — online dot, url, uptime, session count."""

    _last_markup: str = ""

    def set_state(
        self,
        *,
        online: bool,
        url: str,
        uptime: str,
        n_sessions: int,
    ) -> None:
        status = (
            "[#3fb950]●[/] [bold #3fb950]ONLINE[/]" if online
            else "[#f85149]○[/] [bold #f85149]OFFLINE[/]"
        )
        markup = (
            f"[bold #e6edf3]CLAUDE REPLAY[/]   {status}   "
            f"[#8b949e]{url}[/]   "
            f"[#8b949e]uptime[/] [#e6edf3]{uptime}[/]   "
            f"[#8b949e]sessions[/] [#e6edf3]{n_sessions}[/]"
        )
        if markup == self._last_markup:
            return
        self._last_markup = markup
        self.update(markup)


class SessionList(ListView):
    """Sidebar list of sessions, newest first."""

    BINDINGS = [
        Binding("up", "cursor_up", show=False),
        Binding("down", "cursor_down", show=False),
    ]

    async def populate(
        self, sessions: list[dict[str, Any]], active: str | None
    ) -> None:
        await self.clear()
        if not sessions:
            await self.append(ListItem(Label("[#484f58]no sessions yet[/]")))
            return
        active_index: int | None = None
        for idx, s in enumerate(sessions):
            sid = s["id"]
            scolor = status_color(s.get("status"))
            glyph = status_glyph(s.get("status"))
            marker = "▶" if sid == active else " "
            model = (s.get("model") or "—")[:14].ljust(14)
            ckpts = str(s.get("checkpoints", 0)).rjust(3)
            item = ListItem(
                Label(
                    f"[#58a6ff]{marker}[/] [{scolor}]{glyph}[/] "
                    f"[#e6edf3]{short_id(sid)}[/] [#6e7681]{model}[/] "
                    f"[#bc8cff]{ckpts}c[/]"
                )
            )
            item.session_id = sid  # type: ignore[attr-defined]
            await self.append(item)
            if sid == active:
                active_index = idx
        if active_index is not None:
            self.index = active_index


class FeedTable(DataTable):
    """Event feed for the active session."""

    def on_mount(self) -> None:
        self.cursor_type = "row"
        self.zebra_stripes = False
        self.add_columns("SEQ", "TIME", "TYPE", "TOOL", "DETAIL")

    def render_events(self, events: list[dict[str, Any]]) -> None:
        self.clear()
        for e in events:
            etype = e.get("event_type") or ""
            ecolor = event_color(etype)
            ts = (e.get("timestamp") or "")[-8:]  # HH:MM:SS tail
            seq_text = Text(f"{e.get('seq', ''):>4}", style="#6e7681")
            ts_text = Text(ts, style="#6e7681")
            type_text = Text(f"{event_glyph(etype)} {etype}", style=f"bold {ecolor}")
            tool_text = Text((e.get("tool_name") or "—")[:16], style="#e6edf3")
            detail_text = Text(_event_detail(e)[:80], style="#8b949e")
            self.add_row(seq_text, ts_text, type_text, tool_text, detail_text)


class Inspector(VerticalScroll):
    """Detail pane — session metadata + latest checkpoint."""

    def show_empty(self) -> None:
        self.remove_children()
        self.mount(Label("[#6e7681]no session selected[/]"))

    def show_session(self, detail: dict[str, Any]) -> None:
        self.remove_children()
        s = detail.get("session", {})
        checkpoints = detail.get("checkpoints", [])
        events = detail.get("events", [])
        scolor = status_color(s.get("status"))

        self.mount(Label("[bold #e6edf3]SESSION[/]"))
        self.mount(Static(f"[#6e7681]id        [/] [#e6edf3]{s.get('id', '—')}[/]"))
        if s.get("name"):
            self.mount(Static(f"[#6e7681]name      [/] [#58a6ff]{s['name']}[/]"))
        self.mount(Static(f"[#6e7681]status    [/] [bold {scolor}]{s.get('status', '—')}[/]"))
        if s.get("death_label"):
            self.mount(Static(f"[#6e7681]ended     [/] [{death_color(s.get('death_cause'))}]{s['death_label']}[/]"))
        self.mount(Static(f"[#6e7681]model     [/] [#e6edf3]{s.get('model') or '—'}[/]"))
        self.mount(Static(f"[#6e7681]project   [/] [#7dd3fc]{s.get('project_dir') or '—'}[/]"))
        self.mount(Static(f"[#6e7681]started   [/] [#e6edf3]{s.get('started_at') or '—'}[/]"))
        self.mount(Static(f"[#6e7681]ended at  [/] [#e6edf3]{s.get('ended_at') or '— (running)'}[/]"))
        self.mount(Static(f"[#6e7681]events    [/] [#e6edf3]{len(events)}[/]"))
        self.mount(Static(f"[#6e7681]ckpts     [/] [#bc8cff]{len(checkpoints)}[/]"))
        tags = s.get("tags") or []
        if tags:
            chips = "  ".join(f"[#7dd3fc]#{t}[/]" for t in tags)
            self.mount(Static(f"[#6e7681]tags      [/] {chips}"))
        self.mount(Static(""))
        self.mount(Label("[bold #e6edf3]OBJECTIVE[/]"))
        self.mount(Static(Text(s.get("objective") or "(not recorded)", style="#e6edf3")))

        if checkpoints:
            latest = checkpoints[-1]
            self.mount(Static(""))
            self.mount(Label(f"[bold #e6edf3]LATEST CHECKPOINT[/] [#6e7681]#{latest.get('seq', '?')}[/]"))
            self.mount(Static(f"[#6e7681]done [/] [#e6edf3]{latest.get('step_done') or '—'}[/]"))
            if latest.get("step_next"):
                self.mount(Static(f"[#6e7681]next [/] [#d97706]{latest['step_next']}[/]"))
            files = latest.get("files_touched") or []
            if files:
                self.mount(Static(""))
                self.mount(Label("[bold #e6edf3]FILES TOUCHED[/]"))
                for f in files[:20]:
                    self.mount(Static(f"[#3fb950]·[/] [#e6edf3]{f}[/]"))


# ── Modal ────────────────────────────────────────────────────────────────────

class MessageModal(ModalScreen[bool]):
    """Dismissable info box (help / resume / export confirmations)."""

    DEFAULT_CSS = """
    MessageModal {
        align: center middle;
    }
    MessageModal > Vertical {
        width: 80%;
        max-width: 110;
        height: auto;
        background: #161b22;
        border: thick #58a6ff;
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss_box", "close"),
        Binding("enter", "dismiss_box", show=False),
        Binding("q", "dismiss_box", show=False),
    ]

    def __init__(self, markup: str) -> None:
        super().__init__()
        self._markup = markup

    def compose(self) -> ComposeResult:
        with Vertical():
            yield Static(self._markup)
            yield Label("[#6e7681]enter / esc to close[/]")

    def action_dismiss_box(self) -> None:
        self.dismiss(True)


# ── Main app ─────────────────────────────────────────────────────────────────

class ReplayTUI(App):
    """Terminal UI for Claude Replay."""

    CSS = """
    Screen {
        background: #0d1117;
        color: #e6edf3;
    }
    TopBar {
        height: 1;
        background: #161b22;
        color: #e6edf3;
        padding: 0 2;
    }
    #body {
        height: 1fr;
    }
    #sidebar {
        width: 38;
        background: #0d1117;
        border-right: solid #21262d;
    }
    #sidebar-header {
        height: 1;
        padding: 0 2;
        color: #e6edf3;
        text-style: bold;
        background: #161b22;
    }
    SessionList {
        background: #0d1117;
    }
    SessionList > ListItem {
        padding: 0 1;
        background: #0d1117;
    }
    SessionList > ListItem.--highlight {
        background: #1c2333;
    }
    #feed-pane {
        width: 2fr;
    }
    #feed-header {
        height: 1;
        background: #161b22;
        padding: 0 2;
        color: #e6edf3;
        text-style: bold;
        border-bottom: solid #21262d;
    }
    FeedTable {
        background: #0d1117;
    }
    FeedTable > .datatable--header {
        background: #161b22;
        color: #8b949e;
    }
    FeedTable > .datatable--cursor {
        background: #1c2333;
    }
    Inspector {
        width: 1fr;
        background: #0d1117;
        border-left: solid #21262d;
        padding: 1 2;
    }
    #status {
        height: 1;
        background: #161b22;
        color: #8b949e;
        padding: 0 2;
        border-top: solid #21262d;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("ctrl+c", "quit", show=False),
        Binding("tab", "focus_next", "panel"),
        Binding("shift+tab", "focus_previous", show=False),
        Binding("r", "resume", "resume"),
        Binding("e", "export", "export"),
        Binding("space", "toggle_pause", "pause"),
        Binding("question_mark", "help", "help"),
        Binding("g", "refresh", show=False),
    ]

    active_session: reactive[str | None] = reactive(None)
    paused: reactive[bool] = reactive(False)

    def __init__(self, url: str = DEFAULT_URL) -> None:
        super().__init__()
        self._url = url
        self._client: ReplayClient | None = None
        self._sessions: list[dict[str, Any]] = []

    def compose(self) -> ComposeResult:
        yield TopBar(id="topbar")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield Static("[bold #e6edf3]SESSIONS[/]", id="sidebar-header")
                yield SessionList(id="sessions")
            with Vertical(id="feed-pane"):
                yield Static(
                    "[bold #e6edf3]FEED[/]  [#6e7681]select a session[/]",
                    id="feed-header",
                )
                yield FeedTable(id="feed")
            yield Inspector(id="inspector")
        yield Static(
            "[bold #58a6ff][R][/] resume  "
            "[bold #58a6ff][E][/] export  "
            "[bold #58a6ff][Space][/] pause  "
            "[bold #58a6ff][?][/] help  "
            "[bold #58a6ff][Q][/] quit",
            id="status",
        )
        yield Footer()

    async def on_mount(self) -> None:
        self._client = ReplayClient(self._url)
        self.query_one(TopBar).set_state(
            online=True, url=self._url, uptime="—", n_sessions=0
        )
        self.query_one(Inspector).show_empty()
        self.set_interval(POLL_INTERVAL, self.refresh_state)
        await self.refresh_state()

    async def on_unmount(self) -> None:
        if self._client:
            await self._client.aclose()

    # ── Polling ──

    async def refresh_state(self) -> None:
        if self.paused or not self._client or not self.is_running:
            return
        try:
            topbar = self.query_one(TopBar)
        except NoMatches:
            return  # mid-teardown — the poll outran the screen
        try:
            state = await self._client.state()
        except (httpx.HTTPError, ReplayError):
            topbar.set_state(online=False, url=self._url, uptime="—", n_sessions=0)
            return

        sessions = state.get("sessions", [])
        self._sessions = sessions
        topbar.set_state(
            online=True,
            url=self._url,
            uptime=_short_uptime(state.get("uptime_seconds", 0)),
            n_sessions=len(sessions),
        )
        await self.query_one(SessionList).populate(sessions, self.active_session)

        if self.active_session is None and sessions:
            self.active_session = sessions[0]["id"]
            self._update_feed_header(sessions[0])
            self.run_worker(self._load_session(sessions[0]["id"]), exclusive=True)
        elif self.active_session:
            # refresh the active session's feed in place
            self.run_worker(self._load_session(self.active_session), exclusive=True)

    async def _load_session(self, session_id: str) -> None:
        if not self._client:
            return
        try:
            detail = await self._client.session(session_id)
        except (httpx.HTTPError, ReplayError):
            return
        if self.active_session != session_id or not self.is_running:
            return
        try:
            self.query_one(FeedTable).render_events(detail.get("events", []))
            self.query_one(Inspector).show_session(detail)
        except NoMatches:
            return  # screen torn down between the await and the render

    def _update_feed_header(self, session: dict[str, Any]) -> None:
        hdr = self.query_one("#feed-header", Static)
        hdr.update(
            f"[bold #e6edf3]FEED[/]  [bold #7dd3fc]{short_id(session['id'])}[/]  "
            f"[#6e7681]·  {session.get('events', 0)} events  ·  "
            f"{session.get('checkpoints', 0)} ckpts  ·  {session.get('duration', '—')}[/]"
            + ("  [bold #d97706]· PAUSED[/]" if self.paused else "")
        )

    # ── Event handlers ──

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        item = event.item
        if item is None:
            return
        sid = getattr(item, "session_id", None)
        if not sid:
            return
        self.active_session = sid
        for s in self._sessions:
            if s["id"] == sid:
                self._update_feed_header(s)
                break
        self.run_worker(self._load_session(sid), exclusive=True)

    # ── Actions ──

    def action_toggle_pause(self) -> None:
        self.paused = not self.paused
        for s in self._sessions:
            if s["id"] == self.active_session:
                self._update_feed_header(s)
                break

    def action_refresh(self) -> None:
        self.run_worker(self.refresh_state(), exclusive=True)

    @work
    async def action_resume(self) -> None:
        if not self.active_session or not self._client:
            self.bell()
            return
        try:
            data = await self._client.resume(self.active_session)
        except (httpx.HTTPError, ReplayError) as e:
            self.notify(f"resume failed: {e}", severity="error")
            return
        brief = data.get("brief", "")
        try:
            self.copy_to_clipboard(brief)
            self.notify("Resume brief copied to clipboard.", timeout=4)
        except Exception:
            self.notify("Resume brief generated (clipboard unavailable).", timeout=4)
        await self.push_screen_wait(MessageModal(
            "[bold #e6edf3]RESUME BRIEF[/]  "
            f"[#6e7681]{short_id(self.active_session)}[/]\n\n"
            + Text(brief).markup
        ))

    @work
    async def action_export(self) -> None:
        if not self.active_session or not self._client:
            self.bell()
            return
        try:
            data = await self._client.export(self.active_session)
        except (httpx.HTTPError, ReplayError) as e:
            self.notify(f"export failed: {e}", severity="error")
            return
        path = data.get("path", "—")
        self.notify(f"Exported → {path}", timeout=6)

    @work
    async def action_help(self) -> None:
        await self.push_screen_wait(MessageModal(
            "[bold #e6edf3]Claude Replay TUI[/]\n\n"
            "[#8b949e]↑↓[/] navigate sessions   [#8b949e]Tab[/] switch panel   [#8b949e]Space[/] pause\n"
            "[#8b949e]R[/] resume (copy brief)   [#8b949e]E[/] export HTML trace   [#8b949e]Q[/] quit\n\n"
            f"[#6e7681]connected to {self._url}[/]"
        ))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Claude Replay — Terminal UI")
    p.add_argument(
        "--url", default=DEFAULT_URL,
        help=f"Replay server base URL (default: {DEFAULT_URL})",
    )
    args = p.parse_args(argv)
    ReplayTUI(url=args.url).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
