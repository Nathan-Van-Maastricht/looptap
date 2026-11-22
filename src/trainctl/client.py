import os
import socket
import threading
from collections.abc import Iterator
from typing import BinaryIO, Self

from .protocol import Command, Message, read_message, send_message
from .rundir import RunDir


class Client:
    def __init__(self, rundir: RunDir | str | os.PathLike[str]):
        self.rundir = rundir if isinstance(rundir, RunDir) else RunDir(rundir)
        self._sock: socket.socket | None = None
        self._stream: BinaryIO | None = None
        self._send_lock = threading.Lock()

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

    def send(self, command: Command) -> None:
        if self._sock is None:
            raise RuntimeError("Not connected")

        with self._send_lock:
            send_message(self._sock, command)

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
