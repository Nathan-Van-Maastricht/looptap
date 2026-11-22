from __future__ import annotations

import logging
import os
import queue
import socket
import threading
from collections import deque
from collections.abc import Callable, Iterable
from pathlib import Path

from .protocol import (
    COMMAND_OPS,
    Command,
    History,
    Manifest,
    Message,
    MessageDecoder,
    Notice,
    ProtocolError,
    Report,
    State,
    send_message,
)

log = logging.getLogger(__name__)

_POLL_INTERVAL = 0.5
_RECV_SIZE = 65536
_MAX_PENDING_COMMANDS = 1000


class _ClientConnection:
    def __init__(
        self,
        sock: socket.socket,
        initial: list[Message],
        queue_size: int,
        on_done: Callable[[_ClientConnection], None],
        on_command: Callable[[Command], None],
    ):
        self.sock = sock
        self._initial = initial
        self._queue: queue.Queue[Message] = queue.Queue(maxsize=queue_size)
        self._closing = threading.Event()
        self._on_done = on_done
        self._on_command = on_command
        self.thread = threading.Thread(
            target=self._run, name="trainctl-client-send", daemon=True
        )
        self.reader = threading.Thread(
            target=self._read, name="trainctl-client-recv", daemon=True
        )

    def start(self) -> None:
        self.thread.start()
        self.reader.start()

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
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

            try:
                self.sock.close()
            except OSError:
                pass

            self._on_done(self)

    def _read(self) -> None:
        decoder = MessageDecoder()

        try:
            while not self._closing.is_set():
                try:
                    data = self.sock.recv(_RECV_SIZE)
                except TimeoutError:
                    continue

                if not data:
                    break

                for message in decoder.feed(data):
                    if isinstance(message, Command):
                        self._on_command(message)
                    else:
                        log.warning(f"Ignoring {message.TYPE} message sent by a client")
        except ProtocolError as e:
            log.warning(f"Disconnecting a client that sent a bad message: {e}")
        except OSError:
            pass
        finally:
            self.abort()


class Server:
    def __init__(
        self,
        socket_path: Path,
        manifest: Manifest,
        history_size: int = 500,
        client_queue_size: int = 256,
        send_timeout: float = 5.0,
        initial_history: Iterable[Report] = (),
    ):
        self.socket_path = socket_path
        self.manifest = manifest
        self.client_queue_size = client_queue_size
        self.send_timeout = send_timeout

        self._lock = threading.Lock()
        self._history: deque[Report] = deque(initial_history, maxlen=history_size)
        self._clients: set[_ClientConnection] = set()
        self._state: State | None = None

        self._commands: queue.Queue[Command] = queue.Queue(
            maxsize=_MAX_PENDING_COMMANDS
        )

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
        with self._lock:
            self._history.append(report)
            too_slow = self._offer_all(report)

        self._drop(too_slow)

    def publish_state(self, state: State) -> None:
        with self._lock:
            self._state = state
            too_slow = self._offer_all(state)
        self._drop(too_slow)

    def take_comamnds(self) -> list[Command]:
        commands: list[Command] = []

        while True:
            try:
                commands.append(self._commands.get_nowait())
            except queue.Empty:
                return commands

    def publish_notice(self, notice: Notice) -> None:
        with self._lock:
            too_slow = self._offer_all(notice)
        self._drop(too_slow)

    def wait_for_commands(self, timeout: float) -> list[Command]:
        try:
            first = self._commands.get(timeout=timeout)
        except queue.Empty:
            return []
        return [first, *self.take_comamnds()]

    def _offer_all(self, message: Message) -> list[_ClientConnection]:
        too_slow = [client for client in self._clients if not client.offer(message)]
        self._clients.difference_update(too_slow)
        return too_slow

    def _drop(self, clients: list[_ClientConnection]) -> None:
        for client in clients:
            log.warning("Dropping a client that is not keeping up")
            client.abort()

    def _receive_command(self, command: Command) -> None:
        if command.op not in COMMAND_OPS:
            log.warning(f"Ignoring unknown command op {command.op}")
            return

        try:
            self._commands.put_nowait(command)
        except queue.Full:
            log.warning(f"Command queue full, dropping a {command.op} command")

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
            client.reader.join(timeout=2 * _POLL_INTERVAL)

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
                if self._state is not None:
                    initial.append(self._state)
                client = _ClientConnection(
                    sock,
                    initial,
                    self.client_queue_size,
                    self._remove,
                    self._receive_command,
                )
                self._clients.add(client)
            client.start()

    def _remove(self, client: _ClientConnection) -> None:
        with self._lock:
            self._clients.discard(client)
