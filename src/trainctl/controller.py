import sys
import time
import traceback
import warnings
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from .protocol import Command, MetricValue, Notice, Param, Report, State, encode
from .rundir import RunDir
from .server import Server

_PAUSE_WAIT = 0.5

T = TypeVar("T")


class StopRun(BaseException):
    pass


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
        resume_from: Path | None = None,
    ):
        self._server = server
        self._rundir = rundir
        self._declared = set(metrics)
        self._warned: set[str] = set()
        self._metrics_file = open(rundir.metrics_path, "ab")  # noqa: SIM115
        self._params = {p.name: p for p in params}
        self._values = {p.name: p.default for p in self._params.values()}
        self.params = Params(self._values)
        self._actions = set(actions)
        self._pausable = pausable
        self._paused = False

        self._save: Callable[[Path], None] | None = None
        self._stop_requested = False
        self._resume_from = resume_from
        self._loaded = False
        self.resume = resume_from is not None
        if resume_from is not None:
            self._restore_params(
                rundir.read_controller_state(resume_from).get("params", {})
            )

        self._server.publish_state(self._state())

    def report(self, metrics: Mapping[str, Any]) -> None:
        self._check_loaded()
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

    def checkpointing(
        self, save: Callable[[Path], None], load: Callable[[Path], T]
    ) -> T | None:
        if self._save is not None:
            raise RuntimeError("ctl.checkpoint() was already called")

        self._save = save
        result = None

        if self._resume_from is not None:
            result = load(self._resume_from)
        self._loaded = True

        self._server.publish_state(self._state())

        return result

    def poll(self) -> list[str]:
        self._check_loaded()

        triggered: list[str] = []
        self._apply(self._server.take_comamnds(), triggered)

        while True:
            while self._paused and not self._stop_requested:
                self._apply(self._server.wait_for_commands(_PAUSE_WAIT), triggered)
            if not self._stop_requested:
                break
            self._stop()
        return triggered

    def _stop(self) -> None:
        assert self._save is not None
        self._stop_requested = False
        self._server.publish_notice(
            Notice(level="info", text="stopping: saving checkpoint")
        )
        try:
            self._rundir.write_checkpoint(self._save, {"params": dict(self._values)})
        except Exception as e:  # noqa: BLE001 - a failed save must not lose the run
            traceback.print_exc(file=sys.stderr)
            self._server.publish_notice(
                Notice(level="error", text=f"checkpoint failed, not stopping: {e!r}")
            )
            return
        self._server.publish_notice(
            Notice(level="info", text="checkpoint saved; stopping")
        )
        raise StopRun

    def _check_loaded(self) -> None:
        if self.resume and not self._loaded:
            raise RuntimeError(
                "This run is resuming, but ctl.checkpoint() wasn't called before the loop started, so the checkpoint was never loaded."
            )

    def _restore_params(self, saved: dict[str, Any]) -> None:
        for name, value in saved.items():
            param = self._params.get(name)
            if param is None:
                continue

            try:
                self._values[name] = param.coerce(value)
            except ValueError as e:
                warnings.warn(f"Not restoring param {name!r}: {e}", stacklevel=3)

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
        if command.op == "stop":
            if self._save is None:
                return "rejected stop: this run can't be resumed, as it never called ctl.checkpointing()"
            self._stop_requested = True
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

    resuming = False

    def __init__(self, params: Iterable[Param] = ()):
        self.params = Params({p.name: p.default for p in params})

    def poll(self) -> list[str]:
        return []

    def checkpointing(
        self, save: Callable[[Path], None], load: Callable[[Path], T]
    ) -> T | None:
        return None

    def report(self, metrics: Mapping[str, Any]) -> None:
        pass

    def close(self) -> None:
        pass
