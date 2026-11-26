from __future__ import annotations

import argparse
from collections.abc import Sequence

from ..rundir import RunDir


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="looptap", description="Browse and attach to training runs."
    )
    parser.add_argument(
        "run", nargs="?", help="run directory to open (default: show the run picker)"
    )
    parser.add_argument("--root", help="directory containing runs")
    args = parser.parse_args(argv)

    try:
        from .app import LooptapApp
    except ModuleNotFoundError as e:
        if e.name and e.name.split(".")[0] == "textual":
            raise SystemExit(
                "The looptap TUI needs the 'tui' extra: uv add 'looptap[tui]'"
            ) from e
        raise

    rundir = None
    if args.run:
        rundir = RunDir(args.run)
        if not rundir.manifest_path.is_file():
            raise SystemExit(
                f"Not a run directory (no {RunDir.MANIFEST}): {rundir.path}"
            )

    root = (
        args.root
        if args.root is not None
        else (rundir.path.parent if rundir is not None else None)
    )

    LooptapApp(root, rundir).run()
