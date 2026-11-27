import time
from enum import Enum, auto

from rich.text import Text
from textual.reactive import reactive
from textual.widget import Widget


class Conn(Enum):
    CONNECTING = auto()
    STARTING = auto()
    LIVE = auto()
    DISCONNECTED = auto()


_LABELS = {
    Conn.CONNECTING: "◌ Connecting",
    Conn.STARTING: "◌ Starting",
    Conn.LIVE: "● Live",
    Conn.DISCONNECTED: "○ Disconnected",
}


class StatusBar(Widget):
    DEFAULT_CSS = """
    StatusBar { height: 1; padding: 0 1; background: $boost; }
    """

    conn: reactive[Conn] = reactive(Conn.CONNECTING)
    paused: reactive[bool] = reactive(False)
    last_report_time: reactive[float | None] = reactive(None)
    notice: reactive[str] = reactive("")

    def on_mount(self) -> None:
        self.set_interval(1.0, self.refresh)

    def render(self) -> Text:
        live = self.conn is Conn.LIVE
        parts = ["⏸ Paused" if live and self.paused else _LABELS[self.conn]]

        if live and not self.paused and self.last_report_time is not None:
            parts.append(f"Last report {time.time() - self.last_report_time:.0f}s ago")

        if self.notice:
            parts.append(self.notice)

        return Text("  ·  ".join(parts))
