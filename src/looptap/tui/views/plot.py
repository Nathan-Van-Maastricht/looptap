from collections import deque

from textual.app import ComposeResult
from textual_plotext import PlotextPlot

from ...protocol import (
    Plot,
    Rate,
    Report,
    series_metric,
    series_name,
)
from ..formatting import format_value
from ..plotting import (
    Axis,
    Limits,
    RateTracker,
    Side,
    fit_y_limits,
    integer_x_ticks,
    outside_limits,
    plot_colour,
    visible_points,
)
from .base import ViewWidget

SIDE_AXES: tuple[tuple[Side, Axis], ...] = (("left", "y"), ("right", "y_right"))  # new


class PlotWidget(ViewWidget):
    DEFAULT_CSS = """
    PlotWidget PlotextPlot {height: 8; }
    PlotWidget:focus { border: round $accent; }
    """

    can_focus = True

    def __init__(self, view: Plot):
        super().__init__()
        self.view = view
        self.names = [series_name(s) for s in view.all_ys]
        self.metrics = [series_metric(s) for s in view.all_ys]
        self.trackers = [
            RateTracker(s.window) if isinstance(s, Rate) else None for s in view.all_ys
        ]
        self.sides: list[Side] = ["left"] * len(view.ys) + ["right"] * len(
            view.y_rights
        )
        self.title = view.title or ", ".join(self.names)
        self.border_title = self.title
        self.series = [
            (deque(maxlen=view.window), deque(maxlen=view.window)) for _ in self.names
        ]
        self.latest: list[float | None] = [None] * len(self.names)
        self.overrides: dict[Axis, Limits] = {}

    def compose(self) -> ComposeResult:
        yield PlotextPlot()

    def set_override(self, axis: Axis, limits: Limits | None) -> None:
        if limits is None:
            self.overrides.pop(axis, None)
        else:
            self.overrides[axis] = limits
        self.redraw()

    def declared_limits(self, axis: Axis) -> tuple[float | None, float | None]:
        return {
            "x": (self.view.x_min, self.view.x_max),
            "y": (self.view.y_min, self.view.y_max),
            "y_right": (self.view.y_right_min, self.view.y_right_max),
        }[axis]

    def limits(self, axis: Axis) -> tuple[float | None, float | None]:
        return self.overrides.get(axis, self.declared_limits(axis))

    def apply_reports(self, reports: list[Report]) -> None:
        changed = False
        for report in reports:
            x = report.metrics.get(self.view.x)

            for i, (metric, tracker) in enumerate(zip(self.metrics, self.trackers)):
                value = report.metrics.get(metric)

                if not isinstance(value, float):
                    continue

                y = value if tracker is None else tracker.add(report.time, value)

                if y is None:
                    continue

                self.latest[i] = y
                changed = True

                if isinstance(x, float):
                    self.series[i][0].append(x)
                    self.series[i][1].append(y)

        if not changed:
            return

        self.border_title = self.describe()
        self.redraw()

    def describe(self) -> str:
        values = [
            f"{name}: {format_value(value)}"
            for name, value in zip(self.names, self.latest)
            if value is not None
        ]
        return f"{' | '.join(values)}" if values else self.title

    def redraw(self) -> None:
        plot = self.query_one(PlotextPlot)
        plt = plot.plt
        plt.clear_data()
        plt.xlabel(self.view.x)

        x_limits = self.limits("x")
        plt.xlim(*x_limits)
        points = [visible_points(xs, ys, x_limits) for xs, ys in self.series]

        y_limits = self.y_limits(points)
        for side, limits in y_limits.items():
            plot.plt.ylim(*limits, yside=side)  # ty: ignore[invalid-argument-type]

        drawn_xs = self.draw_series(plot, points, y_limits)

        ticks = integer_x_ticks(drawn_xs, x_limits)
        if ticks is not None:
            plot.plt.xticks(ticks, [f"{t:,}" for t in ticks])

        plot.refresh()

    def y_limits(self, points: list[list[tuple[float, float]]]) -> dict[Side, Limits]:
        limits: dict[Side, Limits] = {}
        for side, axis in SIDE_AXES:
            side_ys = [
                y
                for series_points, s in zip(points, self.sides)
                if s == side
                for _, y in series_points
            ]
            limits[side] = fit_y_limits(side_ys, self.limits(axis))
        return limits

    def draw_series(
        self,
        plot: PlotextPlot,
        points: list[list[tuple[float, float]]],
        y_limits: dict[Side, Limits],
    ) -> list[float]:
        drawn_xs: list[float] = []
        multiple = len(self.names) > 1
        for series_points, name, colour, side in zip(
            points, self.names, self.view.colours, self.sides
        ):
            if not series_points or outside_limits(series_points, y_limits[side]):
                continue

            px, py = zip(*series_points)
            plot.plt.plot(
                px,
                py,
                marker="braille",
                color=plot_colour(colour),
                label=name if multiple else None,
                yside=side,
            )
            drawn_xs.extend(px)
        return drawn_xs
