import subprocess
import time
from typing import Any

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message as TextualMessage
from textual.screen import Screen
from textual.widgets import (
    Button,
    Footer,
    Header,
    Input,
    Static,
    Switch,
)
from textual.worker import get_current_worker

from looptap import EMA

from ..client import Client
from ..protocol import (
    PROTOCOL_VERSION,
    Command,
    History,
    Manifest,
    Message,
    Notice,
    Plot,
    ProtocolError,
    Report,
    State,
)
from ..rundir import RunDir
from ..supervisor import log_tail, resume
from .controls import ControlsPanel, ParamControl
from .modals import InfoScreen, LimitsScreen
from .views import CompactGrid, PlotWidget, ViewWidget, make_widget

STARTUP_TIMEOUT = 600.0
SHOW_CONTROLS = False


class RunScreen(Screen[None]):
    BINDINGS = [  # noqa: RUF012
        ("escape", "app.quit", "Quit"),
        ("r", "back", "Runs"),
        Binding("p", "toggle_pause", "Pause/Resume"),
        ("c", "toggle_controls", "Controls"),
        ("i", "show_info", "Info"),
        ("x", "set_limits('x')", "X range"),
        ("y", "set_limits('y')", "Y range"),
        ("u", "set_limits('y_right')", "Right Y range"),
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
            self.notice += f"; couldn't read its manifest: {e}"
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
            (isinstance(view, Plot) and view.window is None) or isinstance(view, EMA)
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

        if action == "set_limits":
            if not isinstance(self.focused, PlotWidget):
                return False

            return parameters != ("y_right",) or bool(self.focused.view.y_rights)

        return True

    def action_set_limits(self, axis: str) -> None:
        plot = self.focused

        if not isinstance(plot, PlotWidget):
            return

        if axis == "y_right" and not plot.view.y_rights:
            return

        def apply(result: tuple[float | None, float | None] | str | None) -> None:
            if result is None:
                return

            plot.set_override(axis, result if isinstance(result, tuple) else None)

        self.app.push_screen(
            LimitsScreen(
                axis, plot.title, plot.limits(axis), plot.declared_limits(axis)
            ),
            apply,
        )

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

        self.query_one("#status", Static).update("  ·  ".join(parts))
