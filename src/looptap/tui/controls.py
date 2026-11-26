from typing import Any

from textual.app import ComposeResult
from textual.containers import Vertical, VerticalScroll
from textual.widgets import (
    Button,
    Input,
    Label,
    Static,
    Switch,
)

from ..protocol import (
    Manifest,
    Param,
    State,
)

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
