import math

from ..protocol import MetricValue


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
