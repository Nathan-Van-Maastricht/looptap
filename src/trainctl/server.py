from __future__ import annotations

import logging
import os
import queue
import socket
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path

from .protocol import History, Manifest, Message, Report, send_message

log = logging.getLogger(__name__)

_POLL_INTERVAL = 0.5


class _ClientConnection:
    def __init__(
        self,
        sock: socket.socket,
        initial: list[Message],
        queue_size: int,
        on_done: Callable[[_ClientConnection], None],
    ):
        self.sock = sock
        self._initial = initial
        self._queue: queue.Queue[Message] = queue.Queue(maxsize=queue_size)
        self._closing = threading.Event()
        self._on_done = on_done
        self.thread = threading.Thread(
            target=self._run, name="trainctl-client", daemon=True
        )

    def start(self) -> None:
        self.thread.start()

    def offer(self, message: Message) -> bool:
        try:
            self._queue.put_nowait(message)
            return True
        except queue.Full:
            return False

    def close(self) -> None:
        self._closing.set()

    def abort(self) -> None:
        self._closing.set()

        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass

    def _run(self) -> None:
        try:
            for message in self._initial:
                send_message(self.sock, message)
            self._initial = []

            while True:
                try:
                    message = self._queue.get(timeout=_POLL_INTERVAL)
                except queue.Empty:
                    if self._closing.is_set():
                        break
                    continue
                send_message(self.sock, message)
        except OSError as e:
            log.debug(f"client disconnected: {e}")
        finally:
            self._closing.set()
            try:
                self.sock.close()
            except OSError:
                pass

            self._on_done(self)


class Server:
    def __init__(
        self,
        socket_path: Path,
        manifest: Manifest,
        history_size: int = 500,
        client_queue_size: int = 256,
        send_timeout: float = 5.0,
    ):
        self.socket_path = socket_path
        self.manifest = manifest
        self.client_queue_size = client_queue_size
        self.send_timeout = send_timeout

        self._lock = threading.Lock()
        self._history: deque[Report] = deque(maxlen=history_size)
        self._clients: set[_ClientConnection] = set()

        self._stop = threading.Event()
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None

    def start(self) -> None:
        if self.socket_path.exists():
            self.socket_path.unlink()

        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)
        try:
            listener.bind(str(self.socket_path))
        finally:
            os.umask(old_umask)
        listener.listen()
        listener.settimeout(_POLL_INTERVAL)
        self._listener = listener

        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="trainctl-accept", daemon=True
        )
        self._accept_thread.start()

    def publish(self, report: Report) -> None:
        too_slow = []
        with self._lock:
            self._history.append(report)

            for client in self._clients:
                if not client.offer(report):
                    too_slow.append(client)

            self._clients.difference_update(too_slow)

        for client in too_slow:
            log.warning("dropping a client that is not keeping up")
            client.abort()

    def stop(self) -> None:
        self._stop.set()
        if self._accept_thread is not None:
            self._accept_thread.join()

        if self._listener is not None:
            self._listener.close()

        with self._lock:
            clients = list(self._clients)

        for client in clients:
            client.close()

        for client in clients:
            client.thread.join(timeout=self.send_timeout + 2 * _POLL_INTERVAL)
            if client.thread.is_alive():
                client.abort()

        try:
            self.socket_path.unlink()
        except FileNotFoundError:
            pass

    def _accept_loop(self) -> None:
        assert self._listener is not None
        while not self._stop.is_set():
            try:
                sock, _ = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if not self._stop.is_set():
                    log.exception("accept failed; no longer accepting clients")
                break

            sock.settimeout(self.send_timeout)
            with self._lock:
                initial: list[Message] = [
                    self.manifest,
                    History(reports=list(self._history)),
                ]
                client = _ClientConnection(
                    sock, initial, self.client_queue_size, self._remove
                )
                self._clients.add(client)
            client.start()

    def _remove(self, client: _ClientConnection) -> None:
        with self._lock:
            self._clients.discard(client)
