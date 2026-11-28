import math
import time
from collections import deque

from textual.app import ComposeResult
from textual.widgets import (
    Static,
)

from ...protocol import (
    EMA,
    ETA,
    Rate,
    Report,
    Value,
    series_name,
)
from ..formatting import format_duration, format_value
from ..plotting import EMATracker, RateTracker
from .base import V, ViewWidget


class CompactWidget(ViewWidget[V]):
    COMPACT = True

    def compose(self) -> ComposeResult:
        yield Static("-")

    def show(self, text: str) -> None:
        self.current = text
        self.query_one(Static).update(text)


class ValueWidget(CompactWidget[Value]):
    def __init__(self, view: Value):
        super().__init__()
        self.view = view
        self.border_title = view.label or view.metric

    def apply_reports(self, reports: list[Report]) -> None:
        for report in reversed(reports):
            if self.view.metric in report.metrics:
                self.show(format_value(report.metrics[self.view.metric]))
                return


class RateWidget(CompactWidget[Rate]):
    def __init__(self, view: Rate):
        super().__init__()
        self.view = view
        self.border_title = series_name(view)
        self.tracker = RateTracker(view.window)

    def apply_reports(self, reports: list[Report]) -> None:
        rate: float | None = None

        for report in reports:
            value = report.metrics.get(self.view.metric)

            if isinstance(value, float):
                latest = self.tracker.add(report.time, value)

                if latest is not None:
                    rate = latest

        if rate is not None:
            self.show(f"{rate:,.2f}")


class EMAWidget(CompactWidget[EMA]):
    def __init__(self, view: EMA):
        super().__init__()
        self.view = view
        self.border_title = view.label or f"{view.metric} EMA"
        self.tracker = EMATracker(view.decay)

    def apply_reports(self, reports: list[Report]) -> None:
        average: float | None = None

        for report in reports:
            value = report.metrics.get(self.view.metric)

            if isinstance(value, float):
                latest = self.tracker.add(value)
                if latest is not None:
                    average = latest

        if average is not None:
            self.show(format_value(average))


class ETAWidget(CompactWidget[ETA]):
    def __init__(self, view: ETA):
        super().__init__()
        self.view = view
        self.border_title = view.label or "ETA"
        self.recent: deque[tuple[float, float]] = deque(maxlen=view.window + 1)
        self.total: float | None = (
            None if isinstance(view.total, str) else float(view.total)
        )

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

        (t0, v0), (t1, v1) = self.recent[0], self.recent[-1]

        if v1 >= self.total:
            self.show("done")
            return

        rate = (v1 - v0) / (t1 - t0) if t1 > t0 else 0.0
        if rate <= 0:
            self.show("-")
            return

        remaining = (self.total - v1) / rate
        finish = t1 + remaining
        same_day = time.localtime(finish)[:3] == time.localtime()[:3]
        clock = time.strftime(
            "%H:%M" if same_day else "%a %H:%M", time.localtime(finish)
        )

        self.show(f"{format_duration(finish - time.time())}  (~{clock})")
