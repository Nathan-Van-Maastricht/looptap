import math
import time
from collections import deque

from textual.app import ComposeResult
from textual.widgets import (
    Static,
)

from looptap.tui.plotting import RateTracker

from ...protocol import (
    ETA,
    Rate,
    Report,
    Value,
    series_name,
)
from ..formatting import format_duration, format_value
from .base import ViewWidget


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
        self.border_title = series_name(view)
        self.tracker = RateTracker(view.window)

    def compose(self) -> ComposeResult:
        yield Static("-")

    def apply_reports(self, reports: list[Report]) -> None:
        for report in reports:
            value = report.metrics.get(self.view.metric)

            if isinstance(value, float):
                latest = self.tracker.add(report.time, value)

                if latest is not None:
                    rate = latest

        if rate is not None:
            self.query_one(Static).update(f"{rate:,.2f}")


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
