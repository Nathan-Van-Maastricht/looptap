import time
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Header, Static

from ..protocol import Value
from ..rundir import RunDir
from ..runs import RunInfo, list_run_infos
from .app import format_value
from .formatting import format_age

REFRESH_INTERVAL = 2.0

STATE_LABELS = {
    "live": "● live",
    "stopped": "■ stopped",
    "finished": "✓ finished",
    "crashed": "✗ crashed",
    "died": "✗ died",
    "interrupted": "■ interrupted",
    "terminated": "■ terminated",
    "hung up": "■ hung up",
    "exited": "■ exited",
    "elsewhere": "? another host",
    "unknown": "? unknown",
}


def _state_label(info: RunInfo) -> str:
    label = STATE_LABELS.get(info.state, info.state)

    if info.state == "elsewhere" and info.host:
        label = f"? on {info.host}"

    if info.resumable and info.state != "live":
        label += ", resumable"

    return label


def _latest(info: RunInfo) -> str:
    if info.manifest is None or info.last_report is None:
        return ""

    parts = []
    for view in info.manifest.views:
        if isinstance(view, Value) and view.metric in info.last_report.metrics:
            value = info.last_report.metrics[view.metric]
            parts.append(f"{view.label or view.metric} {format_value(value)}")

    return "  ".join(parts)


class PickerScreen(Screen[None]):
    BINDINGS = [("escape", "app.quit", "Quit"), ("r", "refresh_runs", "Refresh")]  # noqa: RUF012
    DEFAULT_CSS = """
    #picker-status { height: 1; padding: 0 1; background: $boost; }
    #runs { height: 1fr; }
    """

    def __init__(self, root: str | Path):
        super().__init__()
        self.root = Path(root)

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="picker-status")
        yield DataTable(id="runs", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        self.sub_title = f"runs in {self.root}"
        table = self.query_one("#runs", DataTable)
        table.add_columns("Run", "State", "Last Report", "Latest")
        self.action_refresh_runs()
        self.set_interval(REFRESH_INTERVAL, self.refresh_if_visible)

    def on_screen_resume(self) -> None:
        self.action_refresh_runs()

    def refresh_if_visible(self) -> None:
        if self.is_current:
            self.action_refresh_runs()

    def action_refresh_runs(self) -> None:
        infos = list_run_infos(self.root)
        table = self.query_one("#runs", DataTable)

        selected = None
        if table.row_count:
            selected = table.coordinate_to_cell_key(
                table.cursor_coordinate
            ).row_key.value

        table.clear()
        now = time.time()
        for info in infos:
            age = format_age(now - info.last_report.time) if info.last_report else ""
            table.add_row(
                info.rundir.name,
                _state_label(info),
                age,
                _latest(info),
                key=str(info.rundir.path),
            )

        if selected is not None:
            try:
                table.move_cursor(row=table.get_row_index(selected))
            except Exception:  # noqa: BLE001, S110
                pass

        status = self.query_one("#picker-status", Static)
        if infos:
            live = sum(info.state == "live" for info in infos)
            status.update(f"{len(infos)} run, {live} live . Enter to open")
        else:
            status.update(f"No runs in {self.root}")

    @on(DataTable.RowSelected)
    def open_run(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value is not None:
            self.app.open_run(RunDir(event.row_key.value))  # ty: ignore[missing-attribute]
