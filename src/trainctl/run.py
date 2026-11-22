import argparse
import os
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

from .controller import Controller, NullController
from .protocol import Manifest, Param, Plot, Rate, Value, View
from .rundir import DEFAULT_ROOT, RunDir
from .server import Server

T = TypeVar("T")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--worker", action="store_true")
    mode.add_argument("--headless", action="store_true")
    args, _ = parser.parse_known_args(argv)
    return args


def _validate_views(views: Sequence[View], metrics: Sequence[str]) -> None:
    declared = set(metrics)
    for view in views:
        if not isinstance(view, (Value, Rate, Plot)):
            raise TypeError(f"Not a view: {view!r}")

        missing = [s for s in view.sources() if s not in declared]
        if missing:
            raise ValueError(
                f"{view!r} uses metrics not declared in metrics: {missing}"
            )


def _validate_controls(params: Sequence[Param], actions: Sequence[str]) -> None:
    for param in params:
        if not isinstance(param, Param):
            raise TypeError(f"not a Param: {param!r}")
    names = [p.name for p in params]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ValueError(f"duplicate param names: {duplicates}")
    duplicates = sorted({a for a in actions if list(actions).count(a) > 1})
    if duplicates:
        raise ValueError(f"duplicate action names: {duplicates}")
    for action in actions:
        if not isinstance(action, str) or not action:
            raise ValueError(f"action names must be non-empty strings, got {action!r}")


def run[T](
    main: Callable[[Controller | NullController], T],
    *,
    metrics: Sequence[str],
    views: Sequence[View],
    params: Sequence[Param] = (),
    actions: Sequence[str] = (),
    pausable: bool = False,
    name: str | None = None,
    runs_root: str | os.PathLike[str] = DEFAULT_ROOT,
    history_size: int = 500,
    argv: Sequence[str] | None = None,
) -> T:
    _validate_views(views, metrics)
    _validate_controls(params, actions)
    args = _parse_args(argv)

    if args.headless:
        null = NullController(params)
        try:
            return main(null)
        finally:
            null.close()

    rundir = RunDir.create(name or Path(sys.argv[0]).stem or "run", runs_root)
    manifest = Manifest(
        run_name=rundir.name,
        metrics=list(metrics),
        views=list(views),
        params=list(params),
        actions=list(actions),
        pausable=pausable,
    )
    rundir.write_manifest(manifest)

    server = Server(rundir.socket_path, manifest, history_size=history_size)
    server.start()

    try:
        ctl = Controller(rundir, server, metrics, params, actions, pausable)
        print(f"[trainctl] run directory: {rundir.path}", file=sys.stderr)
        print(f"trainctl] attach with: trainctl {rundir.path}", file=sys.stderr)
        try:
            return main(ctl)
        finally:
            ctl.close()
    finally:
        server.stop()
