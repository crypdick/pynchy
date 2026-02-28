"""Standalone TUI client — connects to a running pynchy instance via HTTP."""

from __future__ import annotations

import asyncio
import json
import re
from datetime import UTC, datetime

import aiohttp
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.selection import Selection
from textual.widgets import Footer, Header, Input, ListItem, ListView, RichLog, Static


class PynchyTUI(App):
    """Textual app that connects to a running pynchy HTTP server."""

    TITLE = "🦞 pynchy"

    CSS = """
    #group-bar {
        height: 10;
        padding: 0 1;
    }
    #group-bar-label {
        width: auto;
        padding: 0 1 0 0;
        content-align-vertical: middle;
    }
    #group-list {
        height: 10;
    }
    #chat-header {
        height: 1;
        padding: 0 1;
        background: $primary;
        color: $text;
    }
    ChatLog {
        height: 1fr;
    }
    #message-input {
        dock: bottom;
    }
    """

    BINDINGS = [
        Binding("ctrl+n", "next_group", "Next Group"),
        Binding("ctrl+p", "prev_group", "Prev Group"),
        Binding("ctrl+q", "quit", "Quit"),
    ]

    def __init__(self, base_url: str) -> None:
        super().__init__()
        self._base_url = base_url.rstrip("/")
        self._session: aiohttp.ClientSession | None = None
        self._groups: list[dict] = []
        self._active_jid: str | None = None
        self._sse_task: asyncio.Task | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="group-bar"):
            yield Static("Group:", id="group-bar-label")
            yield ListView(id="group-list")
        yield Static("Connecting...", id="chat-header")
        yield ChatLog(highlight=True, markup=True)
        yield Input(placeholder="Type a message...", id="message-input")
        yield Footer()

    async def on_mount(self) -> None:
        self._session = aiohttp.ClientSession()
        try:
            await self._load_groups()
            self._sse_task = asyncio.create_task(self._listen_sse())
            await self._update_status()
        except aiohttp.ClientError as exc:
            self.query_one("#chat-header", Static).update(f"[red]Connection failed: {exc}[/red]")

    async def on_unmount(self) -> None:
        if self._sse_task:
            self._sse_task.cancel()
        if self._session:
            await self._session.close()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, **params: str) -> dict | list:
        async with self._session.get(f"{self._base_url}{path}", params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _post(self, path: str, data: dict) -> dict:
        async with self._session.post(f"{self._base_url}{path}", json=data) as resp:
            resp.raise_for_status()
            return await resp.json()

    async def _update_status(self) -> None:
        """Fetch /health and show connection status in the sub-title."""
        try:
            health = await self._get("/health")
            connected = health.get("channels_connected", False)
            status = "Connected" if connected else "Disconnected"
            self.sub_title = status
        except aiohttp.ClientError:
            self.sub_title = "Server unreachable"

    # ------------------------------------------------------------------
    # Group list
    # ------------------------------------------------------------------

    async def _load_groups(self) -> None:
        self._groups = await self._get("/api/groups")
        group_list = self.query_one("#group-list", ListView)
        group_list.clear()
        for g in self._groups:
            group_list.append(ListItem(Static(g["name"]), id=f"grp-{_sanitize(g['jid'])}"))
        if not self._active_jid and self._groups:
            await self._switch_to_group(self._groups[0]["jid"])

    async def _switch_to_group(self, jid: str) -> None:
        group = next((g for g in self._groups if g["jid"] == jid), None)
        if not group:
            return
        self._active_jid = jid
        self.query_one("#chat-header", Static).update(f"Chat: {group['name']}")

        chat_log = self.query_one(ChatLog)
        chat_log.clear()
        messages = await self._get("/api/messages", jid=jid, limit="100")
        for msg in messages:
            _render_message(chat_log, msg["sender_name"], msg["content"], msg["timestamp"])

    # ------------------------------------------------------------------
    # Widget events
    # ------------------------------------------------------------------

    async def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id or ""
        prefix = "grp-"
        if item_id.startswith(prefix):
            sanitized = item_id[len(prefix) :]
            for g in self._groups:
                if _sanitize(g["jid"]) == sanitized:
                    await self._switch_to_group(g["jid"])
                    break

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text or not self._active_jid:
            return
        event.input.value = ""

        # Render immediately for responsiveness
        chat_log = self.query_one(ChatLog)
        _render_message(chat_log, "You", text, datetime.now(UTC).isoformat())

        # Send to server
        try:
            await self._post("/api/send", {"jid": self._active_jid, "content": text})
        except aiohttp.ClientError as exc:
            chat_log.write(f"[red]Send failed: {exc}[/red]")

    # ------------------------------------------------------------------
    # SSE listener
    # ------------------------------------------------------------------

    async def _listen_sse(self) -> None:
        """Listen for server-sent events and update the UI."""
        connected_before = False
        while True:
            try:
                async with self._session.get(
                    f"{self._base_url}/api/events",
                    headers={"Accept": "text/event-stream"},
                    timeout=aiohttp.ClientTimeout(total=None, sock_read=None),
                ) as resp:
                    if connected_before and self._active_jid:
                        # Reconnected after a drop — reload messages to fill the gap
                        await self._switch_to_group(self._active_jid)
                        await self._update_status()
                    connected_before = True
                    async for line in resp.content:
                        decoded = line.decode("utf-8").strip()
                        if not decoded.startswith("data: "):
                            continue
                        try:
                            event = json.loads(decoded[6:])
                        except json.JSONDecodeError:
                            continue
                        self._handle_sse_event(event)
            except (aiohttp.ClientError, TimeoutError):
                # Reconnect after a brief pause
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                return

    def _handle_sse_event(self, event: dict) -> None:
        if event.get("type") == "message":
            if event.get("chat_jid") == self._active_jid:
                # Skip messages from TUI user (already rendered locally)
                if not event.get("is_bot") and event.get("sender_name") == "You":
                    return
                chat_log = self.query_one(ChatLog)
                _render_message(
                    chat_log,
                    event["sender_name"],
                    event["content"],
                    event["timestamp"],
                )
        elif event.get("type") == "agent_trace" and event.get("chat_jid") == self._active_jid:
            chat_log = self.query_one(ChatLog)
            trace_type = event.get("trace_type", "")
            if trace_type == "thinking":
                chat_log.write("[dim italic]\U0001f4ad thinking...[/dim italic]")
            elif trace_type == "tool_use":
                name = event.get("tool_name", "tool")
                tool_input = event.get("tool_input", {})
                preview = str(tool_input)[:120]
                chat_log.write(f"[dim]\U0001f527 {name}[/dim] [dim italic]{preview}[/dim italic]")
            elif trace_type == "text":
                text = event.get("text", "")
                if text:
                    chat_log.write(f"[dim]{text}[/dim]")
        elif event.get("type") == "agent_activity" and event.get("chat_jid") == self._active_jid:
            group = next((g for g in self._groups if g["jid"] == self._active_jid), None)
            name = group["name"] if group else "?"
            suffix = " [dim][thinking...][/dim]" if event.get("active") else ""
            self.query_one("#chat-header", Static).update(f"Chat: {name}{suffix}")

    # ------------------------------------------------------------------
    # Keybindings
    # ------------------------------------------------------------------

    async def action_next_group(self) -> None:
        await self._cycle_group(1)

    async def action_prev_group(self) -> None:
        await self._cycle_group(-1)

    async def _cycle_group(self, direction: int) -> None:
        if not self._groups:
            return
        jids = [g["jid"] for g in self._groups]
        try:
            idx = jids.index(self._active_jid) if self._active_jid else -1
        except ValueError:
            idx = -1
        new_idx = (idx + direction) % len(jids)
        await self._switch_to_group(jids[new_idx])


class ChatLog(RichLog):
    """Message display area with text selection support.

    RichLog inherits from ScrollView (a container), so Textual disables
    widget-level selection by default.  We override allow_select and
    get_selection so mouse-drag selects only within this widget instead
    of falling through to the terminal's native (full-line) selection.
    """

    @property
    def allow_select(self) -> bool:  # noqa: D102
        return True

    def get_selection(self, selection: Selection) -> tuple[str, str] | None:
        """Extract plain text from the selected region of the log."""
        if not self.lines:
            return None
        text = "\n".join(strip.text for strip in self.lines)
        return selection.extract(text), "\n"


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _sanitize(jid: str) -> str:
    """Convert a JID to a valid Textual widget ID."""
    return re.sub(r"[^a-zA-Z0-9_-]", "-", jid)


def _render_message(log: ChatLog, sender: str, content: str, timestamp: str) -> None:
    try:
        dt = datetime.fromisoformat(timestamp).astimezone()
        time_str = dt.strftime("%H:%M")
    except (ValueError, TypeError):
        time_str = "??:??"
    # Bot messages are stored with "pynchy: <text>" baked into content.
    # Strip the redundant prefix to avoid displaying "pynchy: pynchy: ...".
    prefix = f"{sender}: "
    display = content[len(prefix) :] if content.startswith(prefix) else content
    log.write(f"[dim]{time_str}[/dim] [bold]{sender}[/bold]: {display}")


def run_tui(host: str) -> None:
    """Entry point for the standalone TUI client."""
    url = host if host.startswith("http") else f"http://{host}"
    app = PynchyTUI(base_url=url)
    app.run()
