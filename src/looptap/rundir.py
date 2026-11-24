import hashlib
import json
import os
import shutil
from collections import deque
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Self

from .protocol import Manifest, ProtocolError, Report, decode, encode

DEFAULT_ROOT = Path("runs")

# Mac has 104 socket path length, Linux 108
_MAX_SOCKET_PATH = 100

_SOCKET_FALLBACK_DIR = Path("/tmp")


class RunDir:
    MANIFEST = "manifest.json"
    METRICS = "metrics.jsonl"
    SOCKET = "control.sock"
    CHECKPOINT = "checkpoint"
    LAUNCH = "launch.json"
    WORKER_LOG = "worker.log"
    DISCARDED_METRICS = "metrics.discarded.jsonl"
    STATUS = "status.json"
    CONTROLLER_STATE = "controller.json"

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).resolve()

    @classmethod
    def create(cls, name: str, root: str | os.PathLike[str] = DEFAULT_ROOT) -> Self:
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        path = root / f"{name}-{stamp}"
        suffix = 1
        while True:
            try:
                path.mkdir()
                return cls(path)
            except FileExistsError:
                suffix += 1
                path = root / f"{name}-{stamp}-{suffix}"

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def manifest_path(self) -> Path:
        return self.path / self.MANIFEST

    @property
    def metrics_path(self) -> Path:
        return self.path / self.METRICS

    @property
    def socket_path(self) -> Path:
        preferred = self.path / self.SOCKET

        if len(os.fsencode(preferred)) <= _MAX_SOCKET_PATH:
            return preferred

        digest = hashlib.sha1(os.fsencode(self.path)).hexdigest()[:16]
        return _SOCKET_FALLBACK_DIR / f"looptap-{digest}.sock"

    @property
    def launch_path(self) -> Path:
        return self.path / self.LAUNCH

    @property
    def worker_log_path(self) -> Path:
        return self.path / self.WORKER_LOG

    def write_launch(self, argv: list[str], cwd: str) -> None:
        fd = os.open(self.launch_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"argv": argv, "cwd": cwd}, f)

    def read_launch(self) -> dict | None:
        try:
            launch = json.loads(self.launch_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

        if (
            not isinstance(launch, dict)
            or not launch.get("argv")
            or "cwd" not in launch
        ):
            return None

        return launch

    @property
    def checkpoint_dir(self) -> Path:
        return self.path / self.CHECKPOINT

    def _complete(self, directory: Path) -> bool:
        return (directory / self.CONTROLLER_STATE).is_file()

    def resume_checkpoint(self) -> Path | None:
        for directory in (self.checkpoint_dir, self.path / f"{self.CHECKPOINT}.old"):
            if self._complete(directory):
                return directory
        return None

    def write_checkpoint(
        self, save: Callable[[Path], None], controller_state: dict
    ) -> None:
        """Save a checkpoint without ever leaving a half-written one in place.

        save() writes the loop's files into a temporary directory; controller.json
        is written after it returns. Only then does the temporary directory
        replace the previous checkpoint, so a crash mid-save leaves the old
        checkpoint intact.
        """
        tmp = self.path / f"{self.CHECKPOINT}.tmp"
        old = self.path / f"{self.CHECKPOINT}.old"
        shutil.rmtree(tmp, ignore_errors=True)
        tmp.mkdir()
        save(tmp)
        (tmp / self.CONTROLLER_STATE).write_text(json.dumps(controller_state))
        shutil.rmtree(old, ignore_errors=True)
        if self.checkpoint_dir.exists():
            self.checkpoint_dir.rename(old)
        tmp.rename(self.checkpoint_dir)
        shutil.rmtree(old, ignore_errors=True)

    def read_controller_state(self, checkpoint: Path) -> dict:
        return json.loads((checkpoint / self.CONTROLLER_STATE).read_text())

    def rewind_metrics(self, offset: int) -> int:
        if not self.metrics_path.is_file():
            return 0

        with open(self.metrics_path, "r+b") as f:
            f.seek(offset)
            tail = f.read()
            if not tail:
                return 0

            with open(self.path / self.DISCARDED_METRICS, "ab") as discarded:
                discarded.write(tail)
                discarded.flush()
                os.fsync(discarded.fileno())

            f.truncate(offset)
        return len(tail)

    @property
    def status_path(self) -> Path:
        return self.path / self.STATUS

    def write_status(self, status: dict) -> None:
        tmp = self.path / f"{self.STATUS}.tmp"
        tmp.write_text(json.dumps(status))
        os.replace(tmp, self.status_path)

    def read_status(self) -> dict | None:
        try:
            status = json.loads(self.status_path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return None

        return status if isinstance(status, dict) else None

    def read_last_report(self) -> Report | None:
        try:
            f = open(self.metrics_path, "rb")  # noqa: SIM115
        except FileNotFoundError:
            return None

        with f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 65536))
            lines = f.read().splitlines()

        for line in reversed(lines):
            try:
                message = decode(line)
            except ProtocolError:
                continue

            if isinstance(message, Report):
                return message

        return None

    def read_reports(self, last: int | None) -> list[Report]:
        if not self.metrics_path.is_file():
            return []
        reports: deque[Report] = deque(maxlen=last)
        with open(self.metrics_path, "rb") as f:
            for line in f:
                try:
                    message = decode(line)
                except ProtocolError:
                    continue
                if isinstance(message, Report):
                    reports.append(message)
        return list(reports)

    def write_manifest(self, manifest: Manifest) -> None:
        self.manifest_path.write_bytes(encode(manifest))

    def read_manifest(self) -> Manifest:
        message = decode(self.manifest_path.read_bytes())
        if not isinstance(message, Manifest):
            raise TypeError(f"{self.manifest_path} does not contain a manifest")
        return message

    def __repr__(self) -> str:
        return f"RunDir({str(self.path)!r})"


def list_runs(root: str | os.PathLike[str] = DEFAULT_ROOT) -> list[RunDir]:
    root = Path(root)
    if not root.is_dir():
        return []

    runs = [RunDir(p) for p in root.iterdir() if (p / RunDir.MANIFEST).is_file()]
    runs.sort(key=lambda r: r.manifest_path.stat().st_mtime, reverse=True)
    return runs
