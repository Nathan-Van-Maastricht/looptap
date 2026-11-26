from ...protocol import ETA, Bars, Histogram, Plot, Rate, UnknownView, Value, View
from .bars import BarsWidget, HistogramWidget
from .base import CompactGrid, UnknownViewWidget, ViewWidget
from .compact import ETAWidget, RateWidget, ValueWidget
from .plot import PlotWidget

VIEW_WIDGETS: dict[type, type[ViewWidget]] = {
    Value: ValueWidget,
    Rate: RateWidget,
    ETA: ETAWidget,
    Plot: PlotWidget,
    Bars: BarsWidget,
    Histogram: HistogramWidget,
    UnknownView: UnknownViewWidget,
}


def make_widget(view: View) -> ViewWidget:
    return VIEW_WIDGETS[type(view)](view)  # type: ignore


__all__ = ["CompactGrid", "PlotWidget", "ViewWidget", "make_widget"]
