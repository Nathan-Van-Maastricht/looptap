from typing import TypeVar

from textual.app import ComposeResult
from textual.containers import Container, Vertical
from textual.widgets import (
    Static,
)

from ...protocol import (
    Report,
    UnknownView,
    View,
)

V = TypeVar("V", bound=View)


class ViewWidget[V](Vertical):
    DEFAULT_CSS = """
    ViewWidget {
        height: auto;
        border: round $primary;
        padding: 0 1;
        margin-bottom: 1;
    }
    ViewWidget:focus { border: round $accent; }
    """

    COMPACT = False
    can_focus = True
    view: V
    current: str | None = None

    def apply_reports(self, reports: list[Report]) -> None:
        pass


class UnknownViewWidget(ViewWidget[UnknownView]):
    def __init__(self, view: UnknownView):
        super().__init__()
        self.view = view
        self.border_title = view.kind

    def compose(self) -> ComposeResult:
        yield Static("This TUI doesn't support this view")


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
