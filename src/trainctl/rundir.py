import hashlib
import os
from datetime import datetime
from pathlib import Path
from typing import Self

from .protocol import Manifest, decode, encode

DEFAULT_ROOT = Path("runs")

# Mac has 104 socket path length
_MAX_SOCKET_PATH = 100

_SOCKET_FALLBACK_DIR = Path("/tmp")


class RunDir:
    MANIFEST = "manifest.json"
    METRICS = "metrics.jsonl"
    SOCKET = "control.sock"

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
        return _SOCKET_FALLBACK_DIR / f"trainctl-{digest}.sock"

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
