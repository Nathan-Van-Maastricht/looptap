from __future__ import annotations

import argparse
from collections.abc import Sequence

from ..rundir import DEFAULT_ROOT, RunDir, list_runs


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="trainctl", description="Attach to a training run."
    )
    parser.add_argument(
        "run", nargs="?", help="run directory (default: newest run in --root)"
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

    if args.run:
        rundir = RunDir(args.run)
        if not rundir.manifest_path.is_file():
            raise SystemExit(
                f"Not a run directory (no {RunDir.MANIFEST}): {rundir.path}"
            )
    else:
        runs = list_runs(args.root)
        if not runs:
            raise SystemExit(f"No runs found in {args.root}")
        rundir = runs[0]

    TrainctlApp(rundir).run()
