import os
import socket
from collections.abc import Iterator
from typing import BinaryIO, Self

from .protocol import Message, read_message
from .rundir import RunDir


class Client:
    def __init__(self, rundir: RunDir | str | os.PathLike[str]):
        self.rundir = rundir if isinstance(rundir, RunDir) else RunDir(rundir)
        self._sock: socket.socket | None = None
        self._stream: BinaryIO | None = None

    def connect(self) -> Self:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(str(self.rundir.socket_path))
        except OSError:
            sock.close()
            raise

        self._sock = sock
        self._stream = sock.makefile("rb")
        return self

    def messages(self) -> Iterator[Message]:
        if self._stream is None:
            raise RuntimeError("Not connected")
        yield from read_message(self._stream)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._sock.close()

        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass

    def __enter__(self) -> Self:
        return self.connect()

    def __exit__(self, *exc: object) -> None:
        self.close()
