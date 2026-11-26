import bisect
import math
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message as TextualMessage
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    Switch,
)
from textual.worker import get_current_worker
from textual_plotext import PlotextPlot

from ..client import Client
from ..protocol import (
    ETA,
    PROTOCOL_VERSION,
    Bars,
    Command,
    Histogram,
    History,
    Manifest,
    Message,
    Notice,
    Param,
    Plot,
    ProtocolError,
    Rate,
    Report,
    State,
    UnknownView,
    Value,
    View,
    check_limits,
)
from ..rundir import RunDir
from ..supervisor import log_tail, resume
from .formatting import format_duration, format_value
from .picker import PickerScreen

STARTUP_TIMEOUT = 600.0
SHOW_CONTROLS = False
Y_FALLBACK_MARGIN = 0.01


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
        self.recent: deque[tuple[float, float]] = deque(maxlen=max(1, view.window) + 1)

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


class ETAWidget(ViewWidget):
    COMPACT = True

    def __init__(self, view: ETA):
        super().__init__()
        self.view = view
        self.border_title = view.label or "ETA"
        self.recent: deque[tuple[float, float]] = deque(maxlen=view.window + 1)
        self.total: float | None = (
            None if isinstance(view.total, str) else float(view.total)
        )

    def compose(self) -> ComposeResult:
        yield Static("-")

    def apply_reports(self, reports: list[Report]) -> None:
        for report in reports:
            value = report.metrics.get(self.view.metric)
            if isinstance(value, float) and math.isfinite(value):
                self.recent.append((report.time, value))
            if isinstance(self.view.total, str):
                total = report.metrics.get(self.view.total)
                if isinstance(total, float) and math.isfinite(total):
                    self.total = total
        if not self.recent or self.total is None:
            return

        text = self.query_one(Static)
        (t0, v0), (t1, v1) = self.recent[0], self.recent[-1]
        if v1 >= self.total:
            text.update("done")
            return
        rate = (v1 - v0) / (t1 - t0) if t1 > t0 else 0.0
        if rate <= 0:
            text.update("-")  # not enough reports yet, or not progressing
            return
        remaining = (self.total - v1) / rate
        finish = t1 + remaining
        # Clock time it should finish; with the day too if that isn't today.
        same_day = time.localtime(finish)[:3] == time.localtime()[:3]
        clock = time.strftime(
            "%H:%M" if same_day else "%a %H:%M", time.localtime(finish)
        )
        text.update(f"{format_duration(finish - time.time())}  (~{clock})")


class PlotWidget(ViewWidget):
    DEFAULT_CSS = """
    PlotWidget PlotextPlot {height: 8; }
    PlotWidget:focus { border: round $accent; }
    """

    can_focus = True

    def __init__(self, view: Plot):
        super().__init__()
        self.view = view
        self.names = view.all_ys
        self.sides = ["left"] * len(view.ys) + ["right"] * len(view.y_rights)
        self.title = view.title or ", ".join(self.names)
        self.border_title = self.title
        self.series = [
            (deque(maxlen=view.window), deque(maxlen=view.window)) for _ in self.names
        ]
        self.latest: list[float | None] = [None] * len(self.names)
        self.overrides: dict[str, tuple[float | None, float | None]] = {}

    def compose(self) -> ComposeResult:
        yield PlotextPlot()

    def declared_limits(self, axis: str) -> tuple[float | None, float | None]:
        return {
            "x": (self.view.x_min, self.view.x_max),
            "y": (self.view.y_min, self.view.y_max),
            "y_right": (self.view.y_right_min, self.view.y_right_max),
        }[axis]

    def limits(self, axis: str) -> tuple[float | None, float | None]:
        return self.overrides.get(axis, self.declared_limits(axis))

    def action_set_limits(self, axis: str) -> None:
        def apply(result: tuple[float | None, float | None] | str | None) -> None:
            if result is None:
                return

            if result == "reset":
                self.overrides.pop(axis, None)
            else:
                self.overrides[axis] = result  # ty: ignore[invalid-assignment]
            self.redraw()

        self.app.push_screen(
            LimitsScreen(
                axis, self.title, self.limits(axis), self.declared_limits(axis)
            ),
            apply,
        )

    def apply_reports(self, reports: list[Report]) -> None:
        changed = False
        for report in reports:
            x = report.metrics.get(self.view.x)
            for i, name in enumerate(self.names):
                y = report.metrics.get(name)
                if isinstance(y, float):
                    self.latest[i] = y
                    changed = True

                if isinstance(x, float) and isinstance(y, float):
                    self.series[i][0].append(x)
                    self.series[i][1].append(y)

        if not changed:
            return

        self.border_title = self.describe()
        self.redraw()

    def describe(self) -> str:
        values = [
            f"{name}: {format_value(value)}"
            for name, value in zip(self.names, self.latest)
            if value is not None
        ]
        return f"{' | '.join(values)}" if values else self.title

    def fit_y_limits(
        self, ys: list[float], limits: tuple[float | None, float | None]
    ) -> tuple[float | None, float | None]:
        low, high = limits
        if not ys or (low is None and high is None):
            return low, high

        def distance(y: float) -> float:
            if low is not None and y < low:
                return low - y

            if high is not None and y > high:
                return y - high

            return 0.0

        nearest = min(ys, key=distance)
        if distance(nearest) == 0.0:
            return low, high

        return nearest - Y_FALLBACK_MARGIN, nearest + Y_FALLBACK_MARGIN

    def redraw(self) -> None:
        plot = self.query_one(PlotextPlot)
        plt = plot.plt
        plt.clear_data()
        plt.xlabel(self.view.x)
        x_low, x_high = self.limits("x")
        points = [
            [
                (x, y)
                for x, y in zip(xs, ys)
                if math.isfinite(x)
                and math.isfinite(y)
                and (x_low is None or x >= x_low)
                and (x_high is None or x <= x_high)
            ]
            for xs, ys in self.series
        ]
        plt.xlim(x_low, x_high)

        y_limits = {}
        for side, axis in (("left", "y"), ("right", "y_right")):
            side_ys = [
                y
                for finite, s in zip(points, self.sides)
                if s == side
                for _, y in finite
            ]
            y_limits[side] = self.fit_y_limits(side_ys, self.limits(axis))
            plt.ylim(*y_limits[side], yside=side)  # ty: ignore[invalid-argument-type]

        all_xs: list[float] = []
        multiple = len(self.names) > 1
        for finite, name, colour, side in zip(
            points, self.names, self.view.colours, self.sides
        ):
            low, high = y_limits[side]
            if finite and (
                (high is not None and all(y > high for _, y in finite))
                or (low is not None and all(y < low for _, y in finite))
            ):
                continue

            if finite:
                px, py = zip(*finite)

                plt.plot(
                    px,
                    py,
                    marker="braille",
                    color=plot_colour(colour),
                    label=name if multiple else None,
                    yside=side,
                )

                all_xs.extend(px)

        if all_xs and all(x.is_integer() for x in all_xs):
            first = x_low if x_low is not None else min(all_xs)
            last = x_high if x_high is not None else max(all_xs)
            ticks = int_ticks(math.ceil(first), math.floor(last))
            plt.xticks(ticks, [f"{t:,}" for t in ticks])

        plot.refresh()


class BarsWidget(ViewWidget):
    DEFAULT_CSS = """
    BarsWidget PlotextPlot { height: 10; }
    """

    def __init__(self, view: Bars | Histogram):
        super().__init__()
        self.view = view
        self.border_title = view.title or view.metric

    def compose(self) -> ComposeResult:
        yield PlotextPlot()

    def apply_reports(self, reports: list[Report]) -> None:
        latest = None
        for report in reversed(reports):
            if self.view.metric in report.metrics:
                latest = report.metrics[self.view.metric]
                break

        if latest is None:
            return

        if not isinstance(latest, list):
            latest = [latest]

        plot = self.query_one(PlotextPlot)
        plot.plt.clear_data()
        self.draw(plot.plt, latest)
        plot.refresh()

    def draw(self, plt: object, values: list[float]) -> None:
        assert isinstance(self.view, Bars)

        labels = self.view.labels

        if labels is None or len(labels) != len(values):
            labels = [str(i) for i in range(len(values))]

        plt.bar(  # ty: ignore[missing-attribute]
            labels,
            [v if math.isfinite(v) else 0.0 for v in values],
            color=plot_colour(self.view.colour),
        )


def plot_colour(colour: str | None) -> str | tuple[int, int, int] | None:
    if colour is not None and colour.startswith("#"):
        return (int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16))
    return colour


def histogram_edges(view: Histogram, values: list[float]) -> list[float]:
    if isinstance(view.bins, list):
        return view.bins

    low, high = view.limits if view.limits else (min(values), max(values))
    if high <= low:
        low, high = low - 0.5, high + 0.5

    return [low + (high - low) * i / view.bins for i in range(view.bins + 1)]


def edge_ticks(edges: list[float], width: int) -> list[float]:
    min_gap = (edges[-1] - edges[0]) * 6 / max(width, 12)

    ticks = [edges[0]]
    for edge in edges[1:-1]:
        if edge - ticks[-1] >= min_gap and edges[-1] - edge >= min_gap:
            ticks.append(edge)
    ticks.append(edges[-1])

    return ticks


class HistogramWidget(BarsWidget):
    def draw(self, plt: object, values: list[float]) -> None:
        assert isinstance(self.view, Histogram)

        finite = [v for v in values if math.isfinite(v)]
        if not finite:
            return

        edges = histogram_edges(self.view, finite)
        counts = [0] * (len(edges) - 1)

        for v in finite:
            i = bisect.bisect_right(edges, v) - 1
            if v == edges[-1]:
                i = len(counts) - 1
            if 0 <= i < len(counts):
                counts[i] += 1

        heights: list[float] = [float(c) for c in counts]
        if self.view.density:
            heights = [
                c / (len(finite) * (b - a)) for c, a, b in zip(counts, edges, edges[1:])
            ]

        xs, ys = [edges[0]], [0.0]
        for height, a, b in zip(heights, edges, edges[1:]):
            xs += [a, b]
            ys += [height, height]

        xs.append(edges[-1])
        ys.append(0.0)
        plt.plot(xs, ys, fillx=True, color=plot_colour(self.view.colour))  # ty: ignore[missing-attribute]
        plt.xlim(edges[0], edges[-1])  # ty: ignore[missing-attribute]
        ticks = edge_ticks(edges, self.query_one(PlotextPlot).size.width or 80)
        plt.xticks(ticks, [f"{t:.3g}" for t in ticks])  # ty: ignore[missing-attribute]


class UnknownViewWidget(ViewWidget):
    def __init__(self, view: UnknownView):
        super().__init__()
        self.border_title = view.kind

    def compose(self) -> ComposeResult:
        yield Static("This TUI doesn't support this view")


VIEW_WIDGETS: dict[type, type[ViewWidget]] = {
    Value: ValueWidget,
    Rate: RateWidget,
    ETA: ETAWidget,
    Plot: PlotWidget,
    Bars: BarsWidget,
    Histogram: HistogramWidget,
    UnknownView: UnknownViewWidget,
}


def make_widget(view: View) -> ViewWidget:
    return VIEW_WIDGETS[type(view)](view)  # type: ignore


_INPUT_TYPES = {"int": "integer", "float": "number", "str": "text"}


class ParamControl(Vertical):
    DEFAULT_CSS = """
    ParamControl { height: auto; margin-bottom: 1; }
    ParamControl .confirmed { color: $text-muted; }
    """

    def __init__(self, param: Param):
        super().__init__()
        self.param = param
        self.confirmed: Any = param.default

    def compose(self) -> ComposeResult:
        yield Label(self.param.name)
        yield Static(self._describe(), classes="confirmed")
        if self.param.type == "bool":
            yield Switch(value=bool(self.confirmed))
        else:
            yield Input(value=str(self.confirmed), type=_INPUT_TYPES[self.param.type])  # type: ignore

    def _describe(self) -> str:
        limits = [
            f"{label} {limit:g}"
            for label, limit in (("min", self.param.min), ("max", self.param.max))
            if limit is not None
        ]

        suffix = f"  ({', '.join(limits)})" if limits else ""
        return f"current: {self.confirmed}{suffix}"

    def set_confirmed(self, value: Any) -> None:
        self.confirmed = value
        self.query_one(".confirmed", Static).update(self._describe())
        if self.param.type == "bool":
            switch = self.query_one(Switch)
            if switch.value != value:
                switch.value = bool(value)
        else:
            text_input = self.query_one(Input)
            if not text_input.has_focus:
                text_input.value = str(value)


def flatten_config(config: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    for key, value in config.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            items.extend(flatten_config(value, f"{name}."))
        else:
            items.append((name, value))

    return items


class ControlsPanel(VerticalScroll):
    DEFAULT_CSS = """
    ControlsPanel {
        width: 36;
        height: 1fr;
        border-left: solid $primary;
        padding: 1 1 0 1;
    }
    ControlsPanel .heading { text-style: bold; margin-bottom: 1; }
    ControlsPanel Button { width: 100%; margin-bottom: 1; }
    ControlsPanel Input { margin-top: 1; }
    """

    def __init__(self, manifest: Manifest):
        super().__init__()
        self.manifest = manifest
        self.param_controls = {p.name: ParamControl(p) for p in manifest.params}
        self.stop_armed = False

    def compose(self) -> ComposeResult:
        stop = Button("Stop", id="stop", variant="error")
        stop.display = False
        yield stop

        if self.manifest.pausable:
            yield Button("Pause", id="pause-toggle", variant="warning")
        if self.param_controls:
            yield Label("Params", classes="heading")
            yield from self.param_controls.values()
        if self.manifest.actions:
            yield Label("Actions", classes="heading")
            for action in self.manifest.actions:
                yield Button(action, name=action, classes="action")

    def apply_state(self, state: State, pause_pending: bool = False) -> None:
        self.query_one("#stop", Button).display = state.resumable
        for widget in self.query(
            "ParamControl Input, ParamControl Switch, Button.action"
        ):
            widget.disabled = state.paused

        if self.manifest.pausable:
            toggle = self.query_one("#pause-toggle", Button)
            if not pause_pending:
                toggle.label = "Resume" if state.paused else "Pause"
                toggle.disabled = False

        for name, value in state.params.items():
            if name in self.param_controls:
                self.param_controls[name].set_confirmed(value)

    def set_enabled(self, enabled: bool) -> None:
        for widget in self.query("Button, Input, Switch"):
            widget.disabled = not enabled

    def show_pause_pending(self, op: str) -> None:
        toggle = self.query_one("#pause-toggle", Button)
        toggle.label = "Pausing..." if op == "pause" else "Resuming..."
        toggle.disabled = True

    def arm_stop(self) -> None:
        self.stop_armed = True
        self.query_one("#stop", Button).label = "Confirm stop"
        self.set_timer(3.0, self.disarm_stop)

    def disarm_stop(self) -> None:
        self.stop_armed = False
        self.query_one("#stop", Button).label = "Stop"


AXIS_NAMES = {"x": "X", "y": "Y", "y_right": "Right Y"}


class LimitsScreen(ModalScreen[tuple[float | None, float | None] | str | None]):
    BINDINGS = [("escape", "dismiss", "Cancel")]  # noqa: RUF012
    DEFAULT_CSS = """
    LimitsScreen { align: center middle; }
    #limits {
        width: 50;
        height: auto;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #limits .heading { text-style: bold; margin-bottom: 1; }
    #limits .hint { color: $text-muted; margin-bottom: 1; }
    #limits Horizontal { height: auto; margin-top: 1; }
    #limits Button { width: 1fr; margin-right: 1; }
    """

    def __init__(
        self,
        axis: str,
        title: str,
        current: tuple[float | None, float | None],
        declared: tuple[float | None, float | None],
    ):
        super().__init__()
        self.axis = axis
        self.plot_title = title
        self.current = current
        self.declared = declared

    def compose(self) -> ComposeResult:
        def text(limit: float | None) -> str:
            return "" if limit is None else f"{limit:g}"

        with Vertical(id="limits"):
            yield Label(
                f"{AXIS_NAMES[self.axis]} range: {self.plot_title}", classes="heading"
            )
            yield Static("Leave either blank to fit the data", classes="hint")
            yield Input(
                text(self.current[0]),
                placeholder="min",
                type="number",
                valid_empty=True,
                id="limit-min",
            )
            yield Input(
                text(self.current[1]),
                placeholder="max",
                type="number",
                valid_empty=True,
                id="limit-max",
            )

            yield Button("Apply", id="y-apply", variant="primary")
            with Horizontal():
                yield Button("Reset", id="limit-reset")
                yield Button("Cancel", id="limit-cancel")

    @on(Button.Pressed, "#limit-apply")
    @on(Input.Submitted)
    def apply(self) -> None:
        try:
            low, high = (
                float(value)
                if (value := self.query_one(f"#{name}", Input).value.strip())
                else None
                for name in ("limit-min", "limit-max")
            )
            check_limits(low, high, self.axis)
        except ValueError as e:
            self.notify(str(e), severity="error")
            return

        self.dismiss((low, high))

    @on(Button.Pressed, "#limit-reset")
    def reset(self) -> None:
        self.dismiss("reset")

    @on(Button.Pressed, "#y-cancel")
    def cancel(self) -> None:
        self.dismiss(None)


class CompactGrid(Container):
    DEFAULT_CSS = """
    CompactGrid { layout: grid; height: auto; grid-rows: 3; grid-gutter: 1 1; padding: 1 1 1 1; }
    CompactGrid ViewWidget { height: 3; margin: 0; }
    """

    MIN_CELL_WIDTH = 24
    MAX_CELLS_PER_ROW = 4

    def on_resize(self) -> None:
        self.update_columns()

    def update_columns(self) -> None:
        count = len(self.children)
        if count:
            fit = max(1, self.size.width // self.MIN_CELL_WIDTH)
            self.styles.grid_size_columns = min(count, fit, self.MAX_CELLS_PER_ROW)


class InfoScreen(ModalScreen[None]):
    BINDINGS = [("i", "dismiss", "Close")]  # noqa: RUF012
    DEFAULT_CSS = """
    InfoScreen { align: center middle; }
    #info {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #info .heading { text-style: bold; margin: 1 0 0 0; }
    #info DataTable { height: auto; max-height: 30; }
    #info .hint { color: $text-muted; margin-top: 1; }
    """

    def __init__(self, rundir: RunDir, manifest: Manifest):
        super().__init__()
        self.rundir = rundir
        self.manifest = manifest

    def compose(self) -> ComposeResult:
        status = self.rundir.read_status() or {}
        details = [f"Directory: {self.rundir.path}"]

        if "started" in status:
            details.append(
                f"Started: {time.strftime('%Y-%m-%d %H:%M', time.localtime(status['started']))}"
            )

        if "host" in status:
            details.append(f"Host:    {status['host']}")

        with VerticalScroll(id="info"):
            yield Label(self.manifest.run_name, classes="heading")
            yield Static("\n".join(details), markup=False)
            yield Label("Config", classes="heading")

            if self.manifest.config:
                yield DataTable(show_cursor=False, id="config")
            else:
                yield Static("No config recorded for this run.")

            yield Static("i to close", classes="hint")

    def on_mount(self) -> None:
        if self.manifest.config:
            table = self.query_one("#config", DataTable)
            table.add_columns("Setting", "Value")
            for name, value in flatten_config(self.manifest.config):
                table.add_row(name, str(value))


class RunScreen(Screen[None]):
    BINDINGS = [  # noqa: RUF012
        ("escape", "app.quit", "Quit"),
        ("r", "back", "Runs"),
        Binding("p", "toggle_pause", "Pause/Resume"),
        ("c", "toggle_controls", "Controls"),
        ("i", "show_info", "Info"),
        ("x", "set_x_limits", "X range"),
        ("y", "set_y_limits", "Y range"),
        ("u", "set_y_right_limits", "Right Y range"),
    ]
    CSS = """
    #status { height: 1; padding: 0 1; background: $boost; }
    #plots { padding: 1 1 0 1; }
    #body { height: 1fr; }
    #main { width: 1fr; }
    #resume-bar { height: auto; padding: 0 1; display: none; }
    #resume-text { width: 1fr; height: 3; content-align: left middle; }
    """

    class Incoming(TextualMessage):
        def __init__(self, payload: Message):
            super().__init__()
            self.payload = payload

    class Closed(TextualMessage):
        def __init__(self, reason: str, show_log: bool = False):
            super().__init__()
            self.reason = reason
            self.show_log = show_log

    def __init__(self, rundir: RunDir):
        super().__init__()
        self.rundir = rundir
        self.client = Client(rundir)
        self.view_widgets: list[ViewWidget] = []
        self.last_report_time: float | None = None
        self.connected = False
        self.notice = ""
        self.manifest: Manifest | None = None
        self.paused = False
        self.pause_target: bool | None = None
        self.controls: ControlsPanel | None = None
        self.show_controls: bool = SHOW_CONTROLS
        self.process: subprocess.Popen[bytes] | None = None
        self.starting = False
        self.exiting = False

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Connecting...", id="status")

        with Horizontal(id="resume-bar"):
            yield Static("This run stopped with a checkpoint.", id="resume-text")
            yield Button("Resume", id="resume", variant="success")

        with Horizontal(id="body"), Vertical(id="main"):
            yield CompactGrid(id="compact")
            yield VerticalScroll(id="plots")
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = self.rundir.name
        self.listen()
        self.set_interval(1.0, self.refresh_status)

    def action_back(self) -> None:
        self.app.pop_screen()

    def on_unmount(self) -> None:
        self.exiting = True
        self.client.close()

    @work(thread=True, exclusive=True)
    def listen(self, wait_for_worker: float = 0.0) -> None:
        worker = get_current_worker()
        self.client = Client(self.rundir)
        deadline = time.monotonic() + wait_for_worker

        while True:
            try:
                self.client.connect()
                break
            except (
                FileNotFoundError,
                ConnectionRefusedError,
            ) as e:
                if worker.is_cancelled:
                    return
                exit_code = self.process.poll() if self.process is not None else None
                if exit_code is not None:
                    self.post_message(
                        self.Closed(
                            f"Worker exited with code {exit_code} while starting",
                            show_log=True,
                        )
                    )
                    return
                if time.monotonic() < deadline:
                    time.sleep(0.5)
                    continue
                if isinstance(e, ConnectionRefusedError):
                    self.post_message(self.Closed(f"Could not connect: {e}"))
                    return

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
        show_log = False
        if self.process is not None and not self.exiting:
            try:
                code = self.process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                code = None
            if code:
                reason += f" (exit code {code}; see worker.log)"
                show_log = True

        self.post_message(self.Closed(reason, show_log=show_log))

    @on(Incoming)
    async def handle_incoming(self, event: Incoming) -> None:
        message = event.payload
        if isinstance(message, Manifest):
            await self.apply_manifest(message)
        elif isinstance(message, History):
            self.apply_reports(self.full_history(message.reports))
        elif isinstance(message, Report):
            self.apply_reports([message])
        elif isinstance(message, State):
            await self.apply_state(message)
        elif isinstance(message, Notice):
            self.notify(
                message.text,
                severity="error" if message.level == "error" else "information",
            )

    @on(Closed)
    async def handle_closed(self, event: Closed) -> None:
        self.connected = False
        self.starting = False
        self.notice = event.reason
        self.pause_target = None

        if self.rundir.resume_checkpoint() is not None:
            self.notice += f"; resumable with --resume {self.rundir.path}"

        if self.controls is not None:
            self.controls.set_enabled(False)

        if event.show_log:
            self.notify(
                "\n".join(log_tail(self.rundir)) or "worker.log is empty",
                title="End of worker.log",
                severity="error",
                timeout=30,
                markup=False,
            )

        self.update_resume_bar()

        if self.manifest is None:
            await self.load_saved_run()

        self.refresh_bindings()
        self.refresh_status()

    async def apply_manifest(self, manifest: Manifest) -> None:
        self.connected = True
        self.sub_title = manifest.run_name
        self.starting = False
        self.notice = ""
        self.paused = False
        self.pause_target = None
        self.last_report_time = None
        self.update_resume_bar()

        if manifest.protocol_version != PROTOCOL_VERSION:
            self.notice = f"Protocol v{manifest.protocol_version}, TUI expects v{PROTOCOL_VERSION}"

        await self.build_views(manifest)

        if manifest.params or manifest.actions or manifest.pausable:
            await self.ensure_controls()

        self.refresh_bindings()
        self.refresh_status()

    async def build_views(self, manifest: Manifest) -> None:
        compact = self.query_one("#compact", CompactGrid)
        plots = self.query_one("#plots", VerticalScroll)
        await compact.remove_children()
        await plots.remove_children()

        self.view_widgets = [make_widget(view) for view in manifest.views]
        compact_widgets = [w for w in self.view_widgets if w.COMPACT]
        await compact.mount(*compact_widgets)
        await plots.mount(*(w for w in self.view_widgets if not w.COMPACT))
        compact.display = bool(compact_widgets)
        compact.update_columns()

        self.manifest = manifest
        if self.controls is not None:
            await self.controls.remove()
            self.controls = None

    async def load_saved_run(self) -> None:
        try:
            manifest = self.rundir.read_manifest()
        except (OSError, ValueError, ProtocolError) as e:
            self.notice += f"; couldn't read it's manifest: {e}"
            return

        self.sub_title = manifest.run_name
        await self.build_views(manifest)

        self.apply_reports(self.rundir.read_reports(None))
        self.notice += "; showing its saved metrics"

    def apply_reports(self, reports: list[Report]) -> None:
        if not reports:
            return

        for widget in self.view_widgets:
            widget.apply_reports(reports)
        self.last_report_time = reports[-1].time

        self.refresh_status()

    def full_history(self, recent: list[Report]) -> list[Report]:
        whole_run = self.manifest is not None and any(
            isinstance(view, Plot) and view.window is None
            for view in self.manifest.views
        )

        if not whole_run or not recent:
            return recent

        cutoff = recent[-1].time

        return [r for r in self.rundir.read_reports(None) if r.time <= cutoff]

    async def ensure_controls(self) -> None:
        if self.controls is None and self.manifest is not None:
            self.controls = ControlsPanel(self.manifest)
            self.controls.display = self.show_controls
            await self.query_one("#body", Horizontal).mount(self.controls)
            self.refresh_bindings()

    async def apply_state(self, state: State) -> None:
        self.paused = state.paused

        if self.pause_target is not None and state.paused == self.pause_target:
            self.pause_target = None

        if state.resumable:
            await self.ensure_controls()

        if self.controls is not None:
            self.controls.apply_state(
                state, pause_pending=self.pause_target is not None
            )

        self.refresh_status()

    def send(self, command: Command) -> bool:
        if not self.connected:
            self.notify("Not connected", severity="warning")
            return False

        try:
            self.client.send(command)
        except OSError as e:
            self.notify(f"Could not send: {e}", severity="error")
            return False

        return True

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "toggle_pause":
            if self.manifest is None or not self.manifest.pausable:
                return False
            return True if self.connected else None

        if action == "toggle_controls":
            return self.controls is not None

        if action == "show_info":
            return self.manifest is not None

        if action in ("set_x_limits", "set_y_limits"):
            return isinstance(self.focused, PlotWidget)

        if action == "set_y_right_limits":
            return isinstance(self.focused, PlotWidget) and bool(
                self.focused.view.y_rights
            )

        return True

    def action_set_x_limits(self) -> None:
        if isinstance(self.focused, PlotWidget):
            self.focused.action_set_limits("x")

    def action_set_y_limits(self) -> None:
        if isinstance(self.focused, PlotWidget):
            self.focused.action_set_limits("y")

    def action_set_y_right_limits(self) -> None:
        if isinstance(self.focused, PlotWidget) and self.focused.view.y_rights:
            self.focused.action_set_limits("y_right")

    def on_descendant_focus(self) -> None:
        self.refresh_bindings()

    def on_descendant_blur(self) -> None:
        self.refresh_bindings()

    def action_toggle_controls(self) -> None:
        self.show_controls = not self.show_controls

        if self.controls is not None:
            self.controls.display = self.show_controls

    def action_show_info(self) -> None:
        if self.manifest is not None:
            self.app.push_screen(InfoScreen(self.rundir, self.manifest))

    def action_toggle_pause(self) -> None:
        if self.pause_target is not None:
            return
        op = "resume" if self.paused else "pause"
        if not self.send(Command(op)):
            return

        self.pause_target = op == "pause"

        if self.controls is not None:
            self.controls.show_pause_pending(op)

    @on(Button.Pressed, "#pause-toggle")
    def pause_pressed(self) -> None:
        self.action_toggle_pause()

    def update_resume_bar(self) -> None:
        resumable = (
            not self.connected
            and not self.starting
            and self.rundir.resume_checkpoint() is not None
            and self.rundir.read_launch() is not None
        )
        self.query_one("#resume-bar").display = resumable

    @on(Button.Pressed, "#resume")
    def resume_pressed(self) -> None:
        try:
            self.process = resume(self.rundir)
        except (OSError, RuntimeError) as e:
            self.notify(f"Could not start the worker: {e}", severity="error")
            return

        self.starting = True
        self.notice = "waiting for the worker to start; its output goes to worker.log"
        self.update_resume_bar()
        self.refresh_status()
        self.listen(wait_for_worker=STARTUP_TIMEOUT)

    @on(Button.Pressed, "#stop")
    def stop_pressed(self) -> None:
        assert self.controls is not None
        if not self.controls.stop_armed:
            self.controls.arm_stop()
            return
        self.controls.disarm_stop()
        self.send(Command("stop"))

    @on(Button.Pressed, ".action")
    def action_pressed(self, event: Button.Pressed) -> None:
        self.send(Command("action", event.button.name))

    @on(Input.Submitted)
    def param_submitted(self, event: Input.Submitted) -> None:
        control = event.input.parent
        if isinstance(control, ParamControl):
            self.submit_param(control, event.value)

    @on(Switch.Changed)
    def param_switched(self, event: Switch.Changed) -> None:
        control = event.switch.parent
        if isinstance(control, ParamControl) and event.value != control.confirmed:
            self.submit_param(control, event.value)

    def submit_param(self, control: ParamControl, raw: Any) -> None:
        try:
            value = control.param.coerce(raw)
        except ValueError as e:
            self.notify(f"{control.param.name}: {e}", severity="error")
            return
        self.send(Command("set_param", control.param.name, value))

    def refresh_status(self) -> None:
        if self.starting:
            parts = ["◌ Starting"]
        elif not self.connected:
            parts = ["○ Disconnected"]
        elif self.paused:
            parts = ["⏸ Paused"]
        else:
            parts = ["● Live"]

        if self.connected and not self.paused and self.last_report_time is not None:
            parts.append(f"Last report {time.time() - self.last_report_time:.0f}s ago")

        if self.notice:
            parts.append(self.notice)

        self.query_one("#status", Static).update("  .  ".join(parts))


class LooptapApp(App[None]):
    TITLE = "looptap"

    def __init__(self, root: str | Path | None, open_run: RunDir | None = None):
        super().__init__()
        self.root = root
        self.initial_run = open_run

    def on_mount(self) -> None:
        self.push_screen(PickerScreen(self.root))
        if self.initial_run is not None:
            self.open_run(self.initial_run)

    def open_run(self, rundir: RunDir) -> None:
        self.push_screen(RunScreen(rundir))
