import time
from collections.abc import Iterable
from pathlib import Path

from textual import on
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    DirectoryTree,
    Footer,
    Header,
    Input,
    Label,
    Static,
)

from ..protocol import Value
from ..rundir import DEFAULT_ROOT, RunDir
from ..runs import RunInfo, list_run_infos
from .formatting import format_age, format_value

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


class DirectoriesOnly(DirectoryTree):
    def filter_paths(self, paths: Iterable[Path]) -> Iterable[Path]:
        return [
            path for path in paths if path.is_dir() and not path.name.startswith(".")
        ]


class DirectoryScreen(ModalScreen[Path | None]):
    BINDINGS = [("d", "dismiss", "Cancel")]  # noqa: RUF012
    DEFAULT_CSS = """
    DirectoryScreen { align: center middle; }
    #directory {
        width: 80%;
        max-width: 100;
        height: 80%;
        border: round $primary;
        background: $surface;
        padding: 1 2;
    }
    #directory .heading { text-style: bold; margin-bottom: 1; }
    #directory DirectoriesOnly { height: 1fr; margin: 1 0; }
    #directory Horizontal { height: auto; }
    #directory Button { width: 1fr; margin-right: 1; }
    #directory #directory-cancel { margin-right: 0; }
    """

    def __init__(self, start: Path):
        super().__init__()
        self.start = start.expanduser().resolve()

    def compose(self) -> ComposeResult:
        with Vertical(id="directory"):
            yield Label("Directory of runs", classes="heading")
            yield Input(str(self.start), id="directory-path")
            yield DirectoriesOnly(self.start, id="directory-tree")
            with Horizontal():
                yield Button("Up", id="directory-up")
                yield Button("Select", id="directory-select", variant="primary")
                yield Button("Cancel", id="directory-cancel")

    def on_mount(self) -> None:
        self.query_one("#directory-tree", DirectoriesOnly).focus()

    @on(DirectoryTree.NodeHighlighted)
    def highlighted(self, event: DirectoryTree.NodeHighlighted) -> None:
        if event.node.data is not None:
            self.query_one("#directory-path", Input).value = str(event.node.data.path)

    @on(Button.Pressed, "#directory-up")
    def up(self) -> None:
        tree = self.query_one("#directory-tree", DirectoriesOnly)
        parent = Path(tree.path).parent
        tree.path = parent
        self.query_one("#directory-path", Input).value = str(parent)

    @on(Button.Pressed, "#directory-select")
    @on(Input.Submitted, "#directory-path")
    def select(self) -> None:
        path = Path(self.query_one("#directory-path", Input).value.strip()).expanduser()
        if not path.is_dir():
            self.notify(f"Not a directory: {path}", severity="error")
            return

        self.dismiss(path.resolve())

    @on(Button.Pressed, "#directory-cancel")
    def cancel(self) -> None:
        self.dismiss(None)


class PickerScreen(Screen[None]):
    BINDINGS = [  # noqa: RUF012
        ("escape", "app.quit", "Quit"),
        ("r", "refresh_runs", "Refresh"),
        ("d", "change_directory", "Directory"),
    ]
    DEFAULT_CSS = """
    #picker-status { height: 1; padding: 0 1; background: $boost; }
    #runs { height: 1fr; }
    """

    def __init__(self, root: str | Path | None):
        super().__init__()
        self.root = Path(root) if root is not None else None

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("", id="picker-status")
        yield DataTable(id="runs", cursor_type="row", zebra_stripes=True)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#runs", DataTable)
        table.add_columns("Run", "State", "Last Report", "Latest")

        if self.root is None and DEFAULT_ROOT.is_dir():
            self.root = DEFAULT_ROOT.resolve()

        self.action_refresh_runs()
        self.set_interval(REFRESH_INTERVAL, self.refresh_if_visible)

        if self.root is None:
            self.action_change_directory()

    def action_change_directory(self) -> None:
        def chosen(path: Path | None) -> None:
            if path is not None:
                self.root = path
                self.action_refresh_runs()

        self.app.push_screen(DirectoryScreen(self.root or Path.cwd()), chosen)

    def on_screen_resume(self) -> None:
        self.action_refresh_runs()

    def refresh_if_visible(self) -> None:
        if self.is_current:
            self.action_refresh_runs()

    def action_refresh_runs(self) -> None:
        status = self.query_one("#picker-status", Static)
        if self.root is None:
            self.sub_title = "No directory chosen"
            status.update("Press d to choose a directory of runs")
            return

        self.sub_title = f"Runs in {self.root}"
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

        if infos:
            live = sum(info.state == "live" for info in infos)
            status.update(f"{len(infos)} run, {live} live . Enter to open")
        else:
            status.update(f"No runs in {self.root}")

    @on(DataTable.RowSelected)
    def open_run(self, event: DataTable.RowSelected) -> None:
        if event.row_key.value is not None:
            self.app.open_run(RunDir(event.row_key.value))  # ty: ignore[missing-attribute]
