import shlex
import subprocess
from collections import deque
from datetime import datetime

from .rundir import RunDir


def resume(rundir: RunDir):
    launch = rundir.read_launch()

    if launch is None:
        raise RuntimeError(
            f"{rundir.name} has no launch.json, so it can't be started from here"
        )

    if rundir.resume_checkpoint() is None:
        raise RuntimeError(f"{rundir.name} has no complete checkpoint to resume from")

    command = [*launch["argv"], "--worker", "--resume", str(rundir.path)]
    with open(rundir.worker_log_path, "ab") as log:
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        log.write(f"\n=== {stamp} {shlex.join(command)}\n".encode())
        log.flush()

        return subprocess.Popen(
            command,
            cwd=launch["cwd"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )


def log_tail(rundir: RunDir, lines: int = 5) -> list[str]:
    try:
        with open(rundir.worker_log_path, errors="replace") as f:
            return [line.rstrip("\n") for line in deque(f, maxlen=lines)]
    except FileNotFoundError:
        return []
