from pathlib import Path

from textual.app import App

from ..rundir import RunDir
from .picker import PickerScreen
from .run_screen import RunScreen


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
