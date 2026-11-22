import time
import warnings
from collections.abc import Iterable, Mapping
from typing import Any

from .protocol import MetricValue, Report, encode
from .rundir import RunDir
from .server import Server


def _to_metric_value(name: str, value: Any) -> MetricValue:
    try:
        if hasattr(value, "tolist"):
            value = value.tolist()
        if isinstance(value, (list, tuple)):
            return [float(v) for v in value]
        return float(value)
    except (TypeError, ValueError) as e:
        raise TypeError(
            f"metric {name!r} must be a number or a 1-D sequence of numbers, got {type(value).__name__}"
        ) from e


class Controller:
    def __init__(self, rundir: RunDir, server: Server, metrics: Iterable[str]):
        self._server = server
        self._declared = set(metrics)
        self._warned: set[str] = set()
        self._metrics_file = open(rundir.metrics_path, "ab")  # noqa: SIM115

    def report(self, metrics: Mapping[str, Any]) -> None:
        values = {name: _to_metric_value(name, v) for name, v in metrics.items()}

        undeclared = values.keys() - self._declared - self._warned
        if undeclared:
            warnings.warn(
                f"Reporting metics not declared in run(): {sorted(undeclared)}; the TUI will not display them",
                stacklevel=2,
            )
            self._warned |= undeclared

        report = Report(time=time.time(), metrics=values)
        self._metrics_file.write(encode(report))
        self._metrics_file.flush()
        self._server.publish(report)

    def close(self) -> None:
        self._metrics_file.close()


class NullController:
    """Used for headless mode"""

    def report(self, metrics: Mapping[str, Any]) -> None:
        pass

    def close(self) -> None:
        pass
