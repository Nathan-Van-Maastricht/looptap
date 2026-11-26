import math

from textual.app import ComposeResult
from textual_plotext import PlotextPlot

from ...protocol import (
    Bars,
    Histogram,
    Report,
)
from ..plotting import (
    edge_ticks,
    histogram_edges,
    histogram_heights,
    plot_colour,
    step_outline,
)
from .base import ViewWidget


class BarsWidget(ViewWidget):
    DEFAULT_CSS = """
    BarsWidget PlotextPlot { height: 10; }
    """

    def __init__(self, view: Bars | Histogram):
        super().__init__()
        self.view = view
        self.border_title = view.title or view.metric

    def compose(self) -> ComposeResult:
        yield PlotextPlot()

    def apply_reports(self, reports: list[Report]) -> None:
        latest = None
        for report in reversed(reports):
            if self.view.metric in report.metrics:
                latest = report.metrics[self.view.metric]
                break

        if latest is None:
            return

        if not isinstance(latest, list):
            latest = [latest]

        plot = self.query_one(PlotextPlot)
        plot.plt.clear_data()
        self.draw(plot, latest)
        plot.refresh()

    def draw(self, plot: PlotextPlot, values: list[float]) -> None:
        assert isinstance(self.view, Bars)

        labels = self.view.labels

        if labels is None or len(labels) != len(values):
            labels = [str(i) for i in range(len(values))]

        plot.plt.bar(
            labels,
            [v if math.isfinite(v) else 0.0 for v in values],
            color=plot_colour(self.view.colour),
        )


class HistogramWidget(BarsWidget):
    def draw(self, plot: PlotextPlot, values: list[float]) -> None:
        assert isinstance(self.view, Histogram)

        finite = [v for v in values if math.isfinite(v)]
        if not finite:
            return

        edges = histogram_edges(self.view, finite)
        heights = histogram_heights(edges, finite, self.view.density)
        xs, ys = step_outline(edges, heights)

        plot.plt.plot(xs, ys, fillx=True, color=plot_colour(self.view.colour))
        plot.plt.xlim(edges[0], edges[-1])
        ticks = edge_ticks(edges, plot.size.width or 80)
        plot.plt.xticks(ticks, [f"{t:.3g}" for t in ticks])
