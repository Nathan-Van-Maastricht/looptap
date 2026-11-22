import math
import time
from collections import deque

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.message import Message as TextualMessage
from textual.widgets import Footer, Header, Sparkline, Static

from ..client import Client
from ..protocol import PROTOCOL_VERSION, History, Manifest, Message, MetricValue, Report
from ..rundir import RunDir

SPARKLINE_POINTS = 200
RATE_WINDOW = 10


def format_value(value: float) -> str:
    if not math.isfinite(value):
        return str(value)
    if value != 0 and (abs(value) < 1e-3 or abs(value) >= 1e5):
        return f"{value:.4e}"
    return f"{value:.6f}"


class MetricPanel(Vertical):
    DEFAULT_CSS = """
    MetricPanel {
        height: auto;
        border: round $primary;
        padding: 0 1;
        margin-bottom: 1;
    }
    MetricPanel .value {height: auto; }
    MetricPanel Sparkline {height: 3; margin-top: 1;}
    """

    def __init__(self, metric_name: str):
        super().__init__()
        self.metric_name = metric_name
        self.border_title = metric_name
        self.values: deque[float] = deque(maxlen=SPARKLINE_POINTS)

    def compose(self) -> ComposeResult:
        yield Static("-", classes="value")
        yield (Sparkline([], summary_function=max))

    def push(self, values: list[MetricValue]) -> None:
        if not values:
            return
        latest = values[-1]
        if isinstance(latest, list):
            self.query_one(".value", Static).update(
                " ".join(f"[{i}] {format_value(v)}" for i, v in enumerate(latest))
            )
            return

        self.values.extend(v for v in values if not isinstance(v, list))
        self.query_one(".value", Static).update(format_value(latest))
        self.query_one(Sparkline).data = [v for v in self.values if math.isfinite(v)]


class TrainctlApp(App[None]):
    TITLE = "trainctl"
    BINDINGS = [("q", "quit", "Quit")]  # noqa: RUF012
    CSS = """
    #status { height: 1; padding: 0 1; background: $boost; }
    #metrics { padding: 1 1 0 1; }
    """

    class Incoming(TextualMessage):
        def __init__(self, payload: Message):
            super().__init__()
            self.payload = payload

    class Closed(TextualMessage):
        def __init__(self, reason: str):
            super().__init__()
            self.reason = reason

    def __init__(self, rundir: RunDir):
        super().__init__()
        self.rundir = rundir
        self.client = Client(rundir)
        self.panels: dict[str, MetricPanel] = {}
        self.recent: deque[tuple[int, float]] = deque(maxlen=RATE_WINDOW)
        self.connected = False
        self.notice = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Connecting...", id="status")
        yield VerticalScroll(id="metrics")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self.rundir.name
        self.listen()
        self.set_interval(1.0, self.refresh_status)

    def on_unmount(self) -> None:
        self.client.close()

    @work(thread=True, exclusive=True)
    def listen(self) -> None:
        try:
            self.client.connect()
        except FileNotFoundError:
            self.post_message(
                self.Closed("No Socket: the run has finished or not started")
            )
            return
        except OSError as e:
            self.post_message(self.Closed(f"Could not connect: {e}"))
            return
        try:
            for message in self.client.messages():
                self.post_message(self.Incoming(message))

            reason = "Worker closed the connection"
        except Exception as e:  # noqa: BLE001 - never crash UI
            reason = f"Connect error: {e}"
        self.post_message(self.Closed(reason))

    @on(Incoming)
    async def handle_incoming(self, event: Incoming) -> None:
        message = event.payload
        if isinstance(message, Manifest):
            await self.apply_manifest(message)
        elif isinstance(message, History):
            self.apply_reports(message.reports)
        elif isinstance(message, Report):
            self.apply_reports([message])

    @on(Closed)
    def handle_closed(self, event: Closed) -> None:
        self.connected = False
        self.notice = event.reason
        self.refresh_status()

    async def apply_manifest(self, manifest: Manifest) -> None:
        self.connected = True
        self.sub_title = manifest.run_name
        if manifest.protocol_version != PROTOCOL_VERSION:
            self.notice = f"Protocol v{manifest.protocol_version}, TUI expects v{PROTOCOL_VERSION}"

        container = self.query_one("#metrics", VerticalScroll)
        await container.remove_children()
        self.panels = {name: MetricPanel(name) for name in manifest.metrics}
        await container.mount(*self.panels.values())
        self.refresh_status()

    def apply_reports(self, reports: list[Report]) -> None:
        if not reports:
            return
        for name, panel in self.panels.items():
            panel.push([r.metrics[name] for r in reports if name in r.metrics])

        for r in reports:
            self.recent.append((r.step, r.time))
        self.refresh_status()

    def refresh_status(self) -> None:
        parts = ["● Live" if self.connected else "○ Disconnected"]

        if self.recent:
            step, t = self.recent[-1]
            parts.append(f"Step {step:,}")
            (s0, t0) = self.recent[0]
            if len(self.recent) > 1 and t > t0:
                parts.append(f"{(step - s0) / (t - t0):.2f} steps/s")
            if self.connected:
                parts.append(f"Last report {time.time() - t:.0f}s ago")

        if self.notice:
            parts.append(self.notice)

        self.query_one("#status", Static).update("  .  ".join(parts))
