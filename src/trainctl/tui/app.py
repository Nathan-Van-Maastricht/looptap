import math
import time
from collections import deque

from textual import on, work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message as TextualMessage
from textual.widgets import Footer, Header, Static
from textual_plotext import PlotextPlot

from trainctl import Rate, Value

from ..client import Client
from ..protocol import (
    PROTOCOL_VERSION,
    History,
    Manifest,
    Message,
    MetricValue,
    Plot,
    Report,
    UnknownView,
    View,
)
from ..rundir import RunDir

PLOT_POINTS = 500


def format_value(value: MetricValue) -> str:
    if isinstance(value, list):
        return " ".join(f"[{i}] {format_value(v)}" for i, v in enumerate(value))
    if not math.isfinite(value):
        return str(value)
    if value.is_integer() and abs(value) < 1e15:
        return f"{int(value):,}"
    if value != 0 and (abs(value) < 1e-3 or abs(value) >= 1e5):
        return f"{value:.4e}"
    return f"{value:.6f}"


def int_ticks(first: int, last: int, count: int = 5) -> list[float]:
    if last <= first:
        return [first]
    return sorted(
        {round(first + (last - first) * i / (count - 1)) for i in range(count)}
    )


class ViewWidget(Vertical):
    DEFAULT_CSS = """
    ViewWidget {
        height: auto;
        border: round $primary;
        padding: 0 1;
        margin-bottom: 1;
    }
    """

    COMPACT = False

    def apply_reports(self, reports: list[Report]) -> None:
        pass


class ValueWidget(ViewWidget):
    COMPACT = True

    def __init__(self, view: Value):
        super().__init__()
        self.view = view
        self.border_title = view.label or view.metric

    def compose(self) -> ComposeResult:
        yield Static("-")

    def apply_reports(self, reports: list[Report]) -> None:
        for report in reversed(reports):
            if self.view.metric in report.metrics:
                self.query_one(Static).update(
                    format_value(report.metrics[self.view.metric])
                )
                return


class RateWidget(ViewWidget):
    COMPACT = True

    def __init__(self, view: Rate):
        super().__init__()
        self.view = view
        self.border_title = view.label or f"{view.metric}/s"
        self.recent: deque[tuple[float, float]] = deque(maxlen=max(2, view.window))

    def compose(self) -> ComposeResult:
        yield Static("-")

    def apply_reports(self, reports: list[Report]) -> None:
        for report in reports:
            value = report.metrics.get(self.view.metric)
            if isinstance(value, float) and math.isfinite(value):
                self.recent.append((report.time, value))

        if len(self.recent) < 2:
            return
        (t0, v0), (t1, v1) = self.recent[0], self.recent[-1]
        if t1 > t0:
            self.query_one(Static).update(f"{(v1 - v0) / (t1 - t0):,.2f}")


class PlotWidget(ViewWidget):
    DEFAULT_CSS = """
    PlotWidget PlotextPlot {height: 8; }
    """

    def __init__(self, view: Plot):
        super().__init__()
        self.view = view
        self.border_title = view.title or view.y
        self.xs: deque[float] = deque(maxlen=PLOT_POINTS)
        self.ys: deque[float] = deque(maxlen=PLOT_POINTS)

    def compose(self) -> ComposeResult:
        yield Static("-", classes="value")
        yield PlotextPlot()

    def apply_reports(self, reports: list[Report]) -> None:
        latest = None
        for report in reports:
            x = report.metrics.get(self.view.x)
            y = report.metrics.get(self.view.y)

            if y is not None:
                latest = y

            if isinstance(x, float) and isinstance(y, float):
                self.xs.append(x)
                self.ys.append(y)

        if latest is None:
            return

        self.query_one(".value", Static).update(format_value(latest))
        self.redraw()

    def redraw(self) -> None:
        finite = [
            (x, y)
            for x, y in zip(self.xs, self.ys)
            if math.isfinite(x) and math.isfinite(y)
        ]
        plot = self.query_one(PlotextPlot)
        plt = plot.plt
        plt.clear_data()
        plt.xlabel(self.view.x)
        if finite:
            xs, ys = zip(*finite)
            plt.plot(xs, ys, marker="braille")

            if all(x.is_integer() for x in xs):
                ticks = int_ticks(int(min(xs)), int(max(xs)))
                plt.xticks(ticks, [f"{t:,}" for t in ticks])
        plot.refresh()


class UnknownViewWidget(ViewWidget):
    def __init__(self, view: UnknownView):
        super().__init__()
        self.border_title = view.kind

    def compose(self) -> ComposeResult:
        yield Static("This TUI doesn't support this view")


VIEW_WIDGETS: dict[type, type[ViewWidget]] = {
    Value: ValueWidget,
    Rate: RateWidget,
    Plot: PlotWidget,
    UnknownView: UnknownViewWidget,
}


def make_widget(view: View) -> ViewWidget:
    return VIEW_WIDGETS[type(view)](view)  # type: ignore


class TrainctlApp(App[None]):
    TITLE = "trainctl"
    BINDINGS = [("q", "quit", "Quit")]  # noqa: RUF012
    CSS = """
    #status { height: 1; padding: 0 1; background: $boost; }
    #compact { height: auto; padding: 1 1 0 1; }
    #compact ViewWidget { width: 1fr; margin-right: 1; }
    #plots { padding: 1 1 0 1; }
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
        self.view_widgets: list[ViewWidget] = []
        self.last_report_time: float | None = None
        self.connected = False
        self.notice = ""

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Connecting...", id="status")
        yield Horizontal(id="compact")
        yield VerticalScroll(id="plots")
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

        compact = self.query_one("#compact", Horizontal)
        plots = self.query_one("#plots", VerticalScroll)
        await compact.remove_children()
        await plots.remove_children()

        self.view_widgets = [make_widget(view) for view in manifest.views]
        compact_widgets = [w for w in self.view_widgets if w.COMPACT]
        await compact.mount(*compact_widgets)
        await plots.mount(*(w for w in self.view_widgets if not w.COMPACT))
        compact.display = bool(compact_widgets)
        self.refresh_status()

        self.refresh_status()

    def apply_reports(self, reports: list[Report]) -> None:
        if not reports:
            return

        for widget in self.view_widgets:
            widget.apply_reports(reports)
        self.last_report_time = reports[-1].time

        self.refresh_status()

    def refresh_status(self) -> None:
        parts = ["● Live" if self.connected else "○ Disconnected"]

        if self.connected and self.last_report_time is not None:
            parts.append(f"Last report {time.time() - self.last_report_time:.0f}s ago")

        if self.notice:
            parts.append(self.notice)

        self.query_one("#status", Static).update("  .  ".join(parts))
