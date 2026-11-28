import dataclasses
import math
from typing import Any

from ..protocol import MetricValue, Rate, UnknownView, View, series_name


def format_value(value: MetricValue) -> str:
    if isinstance(value, list):
        return " ".join(f"[{i}] {format_value(v)}" for i, v in enumerate(value))

    if not math.isfinite(value):
        return str(value)

    if value.is_integer() and abs(value) < 1e15:
        return f"{int(value):,}"

    if value != 0 and (abs(value) < 1e-3 or abs(value) >= 1e5):
        return f"{value:.4e}"

    return f"{value:.6f}"


def format_age(seconds: float) -> str:
    seconds = max(0.0, seconds)

    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds / size:.0f}{unit} ago"
    return f"{seconds:.0f}s ago"


def format_duration(seconds: float) -> str:
    seconds = max(0, round(seconds))
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def view_kind(view: View) -> str:
    return view.kind if isinstance(view, UnknownView) else type(view).__name__


def view_details(view: View) -> list[tuple[str, str]]:
    if isinstance(view, UnknownView):
        items = list(view.data.items())
    else:
        items = [(f.name, getattr(view, f.name)) for f in dataclasses.fields(view)]

    return [(name, _format_detail(value)) for name, value in items if value is not None]


def _format_detail(value: Any) -> str:
    if isinstance(value, Rate):
        return f"{series_name(value)} (rate, window) {value.window}"

    if isinstance(value, list):
        return ", ".join(_format_detail(v) for v in value)

    if isinstance(value, float):
        return f"{value:g}"

    return str(value)
