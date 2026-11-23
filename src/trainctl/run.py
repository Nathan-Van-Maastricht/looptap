import argparse
import os
import shlex
import signal
import sys
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

from .controller import Controller, NullController, StopRun
from .protocol import Manifest, Param, Plot, Rate, Value, View
from .rundir import DEFAULT_ROOT, RunDir
from .server import Server

T = TypeVar("T")


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--worker", action="store_true")
    mode.add_argument("--headless", action="store_true")
    parser.add_argument("--resume", metavar="DIR")
    args, _ = parser.parse_known_args(argv)
    if args.headless and args.resume:
        parser.error("--resume can't be combined with --headless")
    return args


def _install_sigterm_handler() -> Callable[[], None]:
    if threading.current_thread() is not threading.main_thread():
        return lambda: None

    previous = signal.getsignal(signal.SIGTERM)

    def handler(signum: int, frame: object) -> None:
        raise SystemExit(128 + signum)

    signal.signal(signal.SIGTERM, handler)
    return lambda: signal.signal(signal.SIGTERM, previous)  # type: ignore


def _strip_trainctl_args(argv: Sequence[str]) -> list[str]:
    kept: list[str] = []
    skip_next = False

    for arg in argv:
        if skip_next:
            skip_next = False
        elif arg == "--resume":
            skip_next = True
        elif arg not in ("--worker", "--headless") and not arg.startswith("--resume="):
            kept.append(arg)
    return kept


def _resume_command(rundir: RunDir) -> str:
    launch = rundir.read_launch()
    argv = launch["argv"] if launch else _strip_trainctl_args(sys.orig_argv)
    return shlex.join([*argv, "--resume", str(rundir.path)])


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

    resume_from: Path | None = None
    if args.resume:
        rundir = RunDir(args.resume)
        resume_from = rundir.resume_checkpoint()
        if resume_from is None:
            raise SystemExit(
                f"Nothing to resume: {rundir.path} has no complete checkpoint"
            )

        offset = rundir.read_controller_state(resume_from).get("metrics_offset")
        if offset is not None:
            discarded = rundir.rewind_metrics(offset)
            if discarded:
                print(
                    f"[trainctl] set aside {discarded} bytes of reports from after the checkpoint in {rundir.DISCARDED_METRICS}",
                    file=sys.stderr,
                )
    else:
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

    if resume_from is None or rundir.read_launch() is None:
        rundir.write_launch(_strip_trainctl_args(sys.orig_argv), os.getcwd())

    server = Server(
        rundir.socket_path,
        manifest,
        history_size=history_size,
        initial_history=rundir.read_reports(history_size) if resume_from else (),
    )
    restore_sigterm = _install_sigterm_handler()
    server.start()

    try:
        ctl = Controller(
            rundir, server, metrics, params, actions, pausable, resume_from
        )
        print(f"[trainctl] run directory: {rundir.path}", file=sys.stderr)
        if resume_from is not None:
            print(f"[trainctl] resuming from: {resume_from}", file=sys.stderr)
        print(f"trainctl] attach with: trainctl {rundir.path}", file=sys.stderr)
        try:
            return main(ctl)
        except StopRun:
            ctl.close()
        finally:
            ctl.close()
    finally:
        server.stop()
        restore_sigterm()

    print("[trainctl] stopped with a checkpoint. Resume with:", file=sys.stderr)
    print(f"    {_resume_command(rundir)}", file=sys.stderr)
    raise SystemExit(0)
