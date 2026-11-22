import time
import warnings
from collections.abc import Iterable, Mapping
from typing import Any

from .protocol import Command, MetricValue, Notice, Param, Report, State, encode
from .rundir import RunDir
from .server import Server

_PAUSE_WAIT = 0.5


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


class Params:
    def __init__(self, values: dict[str, Any]):
        object.__setattr__(self, "_values", values)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._values[name]
        except KeyError:
            raise AttributeError(f"No param named {name!r}") from None

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError(
            "Params are read only; they change through comamnds from the TUI"
        )

    def as_dict(self) -> dict[str, Any]:
        return dict(self._values)

    def __repr__(self) -> str:
        return f"Params({self._values!r})"


class Controller:
    def __init__(
        self,
        rundir: RunDir,
        server: Server,
        metrics: Iterable[str],
        params: Iterable[Param] = (),
        actions: Iterable[str] = (),
        pausable: bool = False,
    ):
        self._server = server
        self._declared = set(metrics)
        self._warned: set[str] = set()
        self._metrics_file = open(rundir.metrics_path, "ab")  # noqa: SIM115
        self._params = {p.name: p for p in params}
        self._values = {p.name: p.default for p in self._params.values()}
        self.params = Params(self._values)
        self._actions = set(actions)
        self._pausable = pausable
        self._paused = False
        self._server.publish_state(self._state())

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

    def poll(self) -> list[str]:
        triggered: list[str] = []
        self._apply(self._server.take_comamnds(), triggered)

        while self._paused:
            self._apply(self._server.wait_for_commands(_PAUSE_WAIT), triggered)
        return triggered

    def _apply(self, commands: list[Command], triggered: list[str]) -> None:
        accepted = False
        for command in commands:
            error = self._apply_one(command, triggered)
            if error is None:
                accepted = True
            else:
                self._server.publish_notice(Notice(level="error", text=error))

        if accepted:
            self._server.publish_state(self._state())

    def _apply_one(self, command: Command, triggered: list[str]) -> str | None:
        if command.op == "set_param":
            param = self._params.get(command.name or "")
            if param is None:
                return f"rejected set {command.name}: no such param"
            try:
                self._values[param.name] = param.coerce(command.value)
            except ValueError as e:
                return f"rejected set {param.name}={command.value!r}: {e}"
            return None
        if command.op in ("pause", "resume"):
            if not self._pausable:
                return f"rejected {command.op}: this run is not pausable"
            self._paused = command.op == "pause"
            return None
        if command.op == "action":
            if command.name not in self._actions:
                return f"rejected action {command.name}: no such action"
            triggered.append(command.name)
            when = "; it runs after resume" if self._paused else ""
            self._server.publish_notice(
                Notice(level="info", text=f"triggered {command.name}{when}")
            )
            return None
        return f"rejected {command.op}: unknown command"

    def _state(self) -> State:
        return State(params=dict(self._values), paused=self._paused)

    def close(self) -> None:
        self._metrics_file.close()


class NullController:
    """Used for headless mode"""

    def __init__(self, params: Iterable[Param] = ()):
        self.params = Params({p.name: p.default for p in params})

    def poll(self) -> list[str]:
        return []

    def report(self, metrics: Mapping[str, Any]) -> None:
        pass

    def close(self) -> None:
        pass
