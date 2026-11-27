import bisect
import math
from collections import deque
from collections.abc import Iterable

from ..protocol import Histogram

Limits = tuple[float | None, float | None]
Y_FALLBACK_MARGIN = 0.01


def plot_colour(colour: str | None) -> str | tuple[int, int, int] | None:
    if colour is not None and colour.startswith("#"):
        return (int(colour[1:3], 16), int(colour[3:5], 16), int(colour[5:7], 16))
    return colour


def int_ticks(first: int, last: int, count: int = 5) -> list[float]:
    if last <= first:
        return [first]
    return sorted(
        {round(first + (last - first) * i / (count - 1)) for i in range(count)}
    )


def edge_ticks(edges: list[float], width: int) -> list[float]:
    min_gap = (edges[-1] - edges[0]) * 6 / max(width, 12)

    ticks = [edges[0]]
    for edge in edges[1:-1]:
        if edge - ticks[-1] >= min_gap and edges[-1] - edge >= min_gap:
            ticks.append(edge)
    ticks.append(edges[-1])

    return ticks


def histogram_edges(view: Histogram, values: list[float]) -> list[float]:
    if isinstance(view.bins, list):
        return view.bins

    low, high = view.limits if view.limits else (min(values), max(values))
    if high <= low:
        low, high = low - 0.5, high + 0.5

    return [low + (high - low) * i / view.bins for i in range(view.bins + 1)]


def histogram_heights(
    edges: list[float], values: list[float], density: bool = False
) -> list[float]:
    counts = [0] * (len(edges) - 1)

    for v in values:
        i = bisect.bisect_right(edges, v) - 1

        if v == edges[-1]:
            i = len(counts) - 1

        if 0 <= i < len(counts):
            counts[i] += 1

    if density:
        return [
            c / (len(values) * (b - a)) for c, a, b in zip(counts, edges, edges[1:])
        ]

    return [float(c) for c in counts]


def step_outline(
    edges: list[float], heights: list[float]
) -> tuple[list[float], list[float]]:
    xs, ys = [edges[0]], [0.0]
    for height, a, b in zip(heights, edges, edges[1:]):
        xs += [a, b]
        ys += [height, height]

    xs.append(edges[-1])
    ys.append(0.0)
    return xs, ys


def visible_points(
    xs: Iterable[float], ys: Iterable[float], x_limits: Limits
) -> list[tuple[float, float]]:

    low, high = x_limits

    return [
        (x, y)
        for x, y in zip(xs, ys)
        if math.isfinite(x)
        and math.isfinite(y)
        and (low is None or x >= low)
        and (high is None or x <= high)
    ]


def fit_y_limits(ys: list[float], limits: Limits) -> Limits:

    low, high = limits
    if not ys or (low is None and high is None):
        return low, high

    def distance(y: float) -> float:
        if low is not None and y < low:
            return low - y

        if high is not None and y > high:
            return y - high

        return 0.0

    nearest = min(ys, key=distance)
    if distance(nearest) == 0.0:
        return low, high

    return nearest - Y_FALLBACK_MARGIN, nearest + Y_FALLBACK_MARGIN


def outside_limits(points: list[tuple[float, float]], limits: Limits) -> bool:
    low, high = limits

    return bool(points) and (
        (high is not None and all(y > high for _, y in points))
        or (low is not None and all(y < low for _, y in points))
    )


def integer_x_ticks(xs: list[float], x_limits: Limits) -> list[float] | None:

    if not xs or not all(x.is_integer() for x in xs):
        return None

    low, high = x_limits
    first = low if low is not None else min(xs)
    last = high if high is not None else max(xs)

    return int_ticks(math.ceil(first), math.floor(last))


class RateTracker:
    def __init__(self, window: int):
        self.recent: deque[tuple[float, float]] = deque(maxlen=window + 1)

    def add(self, time: float, value: float) -> float | None:
        if not math.isfinite(value):
            return None

        self.recent.append((time, value))
        (t0, v0), (t1, v1) = self.recent[0], self.recent[-1]

        return (v1 - v0) / (t1 - t0) if t1 > t0 else None


class EMATracker:
    def __init__(self, decay: float):
        self.decay = decay
        self.average = 0.0
        self.weight = 0.0

    def add(self, value: float) -> float | None:
        if not math.isfinite(value):
            return None

        self.average = self.decay * self.average + (1 - self.decay) * value
        self.weight = self.decay * self.weight + (1 - self.decay)
        return self.average / self.weight
