import math
import subprocess
import time
from collections import deque
from pathlib import Path
from typing import Any

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message as TextualMessage
from textual.screen import Screen
from textual.widgets import Button, Footer, Header, Input, Label, Static, Switch
from textual.worker import get_current_worker
from textual_plotext import PlotextPlot

from ..client import Client
from ..protocol import (
    PROTOCOL_VERSION,
    Command,
    History,
    Manifest,
    Message,
    Notice,
    Param,
    Plot,
    Rate,
    Report,
    State,
    UnknownView,
    Value,
    View,
)
from ..rundir import RunDir
from ..supervisor import log_tail, resume
from .formatting import format_value
from .picker import PickerScreen

STARTUP_TIMEOUT = 600.0


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


class PlotWidget(ViewWidget):
    DEFAULT_CSS = """
    PlotWidget PlotextPlot {height: 8; }
    """

    def __init__(self, view: Plot):
        super().__init__()
        self.view = view
        self.border_title = view.title or view.y
        self.xs: deque[float] = deque(maxlen=view.window)
        self.ys: deque[float] = deque(maxlen=view.window)

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


class RunScreen(Screen[None]):
    BINDINGS = [  # noqa: RUF012
        ("escape", "back", "Runs"),
        ("q", "app.quit", "Quit"),
        Binding("p", "toggle_pause", "Pause/Resume"),
    ]
    CSS = """
    #status { height: 1; padding: 0 1; background: $boost; }
    #compact { height: auto; padding: 1 1 0 1; }
    #compact ViewWidget { width: 1fr; margin-right: 1; }
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
            yield Horizontal(id="compact")
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
    def handle_closed(self, event: Closed) -> None:
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

        compact = self.query_one("#compact", Horizontal)
        plots = self.query_one("#plots", VerticalScroll)
        await compact.remove_children()
        await plots.remove_children()

        self.view_widgets = [make_widget(view) for view in manifest.views]
        compact_widgets = [w for w in self.view_widgets if w.COMPACT]
        await compact.mount(*compact_widgets)
        await plots.mount(*(w for w in self.view_widgets if not w.COMPACT))
        compact.display = bool(compact_widgets)

        self.manifest = manifest
        if self.controls is not None:
            await self.controls.remove()
            self.controls = None
        if manifest.params or manifest.actions or manifest.pausable:
            await self.ensure_controls()

        self.refresh_bindings()
        self.refresh_status()

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
            await self.query_one("#body", Horizontal).mount(self.controls)

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

        return True

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

    def __init__(self, root: str | Path, open_run: RunDir | None = None):
        super().__init__()
        self.root = root
        self.initial_run = open_run

    def on_mount(self) -> None:
        self.push_screen(PickerScreen(self.root))
        if self.initial_run is not None:
            self.open_run(self.initial_run)

    def open_run(self, rundir: RunDir) -> None:
        self.push_screen(RunScreen(rundir))
