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
