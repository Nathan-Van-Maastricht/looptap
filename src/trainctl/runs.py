import os
import socket
from dataclasses import dataclass
from pathlib import Path

from .protocol import Manifest, ProtocolError, Report
from .rundir import DEFAULT_ROOT, RunDir, list_runs


@dataclass
class RunInfo:
    rundir: RunDir
    state: str
    resumable: bool
    manifest: Manifest | None
    last_report: Report | None
    host: str | None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True

    return True


def _state(rundir: RunDir, status: dict | None, resumable: bool) -> str:
    if status is None:
        if rundir.socket_path.exists():
            return "live"
        return "stopped" if resumable else "unknown"

    state = status.get("state", "unknown")
    if state != "running":
        return state

    if status.get("host") != socket.gethostname():
        return "elsewhere"

    pid = status.get("pid")

    return "live" if isinstance(pid, int) and _pid_alive(pid) else "died"


def inspect_run(rundir: RunDir) -> RunInfo:
    status = rundir.read_status()
    resumable = rundir.resume_checkpoint() is not None

    try:
        manifest: Manifest | None = rundir.read_manifest()
    except (OSError, ValueError, ProtocolError):
        manifest = None

    return RunInfo(
        rundir=rundir,
        state=_state(rundir, status, resumable),
        resumable=resumable,
        manifest=manifest,
        last_report=rundir.read_last_report(),
        host=status.get("host") if status else None,
    )


def list_run_infos(root: str | os.PathLike[str] = DEFAULT_ROOT) -> list[RunInfo]:
    return [inspect_run(rundir) for rundir in list_runs(Path(root))]
