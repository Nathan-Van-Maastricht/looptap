import time
from typing import Any

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (
    Button,
    DataTable,
    Input,
    Label,
    Static,
)

from ..protocol import (
    Manifest,
    check_limits,
)
from ..rundir import RunDir

AXIS_NAMES = {"x": "X", "y": "Y", "y_right": "Right Y"}


def flatten_config(config: dict[str, Any], prefix: str = "") -> list[tuple[str, Any]]:
    items: list[tuple[str, Any]] = []
    for key, value in config.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            items.extend(flatten_config(value, f"{name}."))
        else:
            items.append((name, value))

    return items


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

            yield Button("Apply", id="limit-apply", variant="primary")
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

    @on(Button.Pressed, "#limit-cancel")
    def cancel(self) -> None:
        self.dismiss(None)


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
