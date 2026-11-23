from __future__ import annotations

import argparse
from collections.abc import Sequence

from ..rundir import DEFAULT_ROOT, RunDir


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="trainctl", description="Browse and attach to training runs."
    )
    parser.add_argument(
        "run", nargs="?", help="run directory to open (default: show the run picker)"
    )
    parser.add_argument(
        "--root", default=str(DEFAULT_ROOT), help="directory containing runs"
    )
    args = parser.parse_args(argv)

    try:
        from .app import TrainctlApp
    except ModuleNotFoundError as e:
        if e.name and e.name.split(".")[0] == "textual":
            raise SystemExit(
                "The trainctl TUI needs the 'tui' extra: uv add 'trainctl[tui]'"
            ) from e
        raise

    rundir = None  # new
    if args.run:
        rundir = RunDir(args.run)
        if not rundir.manifest_path.is_file():
            raise SystemExit(
                f"Not a run directory (no {RunDir.MANIFEST}): {rundir.path}"
            )

    TrainctlApp(args.root, rundir).run()
