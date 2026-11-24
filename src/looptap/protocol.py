from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, fields
from typing import Any, BinaryIO, ClassVar

PROTOCOL_VERSION = 5

MetricValue = float | list[float]

COMMAND_OPS = ("set_param", "pause", "resume", "action", "stop")

PARAM_TYPES = ("int", "float", "bool", "str")
_TRUE_STRINGS = ("true", "yes", "on", "1")
_FALSE_STRINGS = ("false", "no", "off", "0")


class ProtocolError(Exception):
    """A line could not be decoded into a valid message"""


class UnknownMessageType(ProtocolError):
    """A well-formed message of a type this version doesn't know"""


@dataclass
class Value:
    KIND: ClassVar[str] = "value"

    metric: str
    label: str | None = None

    def sources(self) -> list[str]:
        return [self.metric]


@dataclass
class Rate:
    KIND: ClassVar[str] = "rate"

    metric: str
    label: str | None = None
    window: int = 1

    def sources(self) -> list[str]:
        return [self.metric]


@dataclass
class Plot:
    KIND: ClassVar[str] = "plot"

    x: str
    y: str
    title: str | None = None

    def sources(self) -> list[str]:
        return [self.x, self.y]


@dataclass
class UnknownView:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)

    def sources(self) -> list[str]:
        return []


View = Value | Rate | Plot | UnknownView
_VIEW_TYPES: dict[str, type] = {cls.KIND: cls for cls in (Value, Rate, Plot)}


@dataclass
class Param:
    name: str
    type: str
    default: Any
    min: float | None = None
    max: float | None = None

    def __post_init__(self) -> None:
        if isinstance(self.type, type):
            self.type = self.type.__name__
        if self.type not in PARAM_TYPES:
            raise ValueError(
                f"Param {self.name!r}: type must be one of {PARAM_TYPES}, got {self.type!r}"
            )
        if self.type not in ("int", "float") and (
            self.min is not None or self.max is not None
        ):
            raise ValueError(
                f"Param {self.name!r}: min and max only apply to int and float params"
            )

        try:
            self.default = self.coerce(self.default)
        except ValueError as e:
            raise ValueError(
                f"Param {self.name!r} has an invalid default: {e}"
            ) from None

    def coerce(self, value: Any) -> Any:
        result: Any
        if self.type == "bool":
            if isinstance(value, bool):
                result = value
            elif isinstance(value, str) and value.strip().lower() in _TRUE_STRINGS:
                result = True
            elif isinstance(value, str) and value.strip().lower() in _FALSE_STRINGS:
                result = False
            else:
                raise ValueError(f"expected a bool, got {value!r}")
        elif self.type == "int":
            if isinstance(value, int) and not isinstance(value, bool):
                result = value
            elif isinstance(value, float) and value.is_integer():
                result = int(value)
            elif isinstance(value, str):
                try:
                    result = int(value.strip())
                except ValueError:
                    raise ValueError(f"expected an int, got {value!r}") from None
            else:
                raise ValueError(f"expected an int, got {value!r}")
        elif self.type == "float":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                result = float(value)
            elif isinstance(value, str):
                try:
                    result = float(value.strip())
                except ValueError:
                    raise ValueError(f"expected a float, got {value!r}") from None
            else:
                raise ValueError(f"expected a float, got {value!r}")
        else:
            if not isinstance(value, str):
                raise ValueError(f"expected a str, got {value!r}")
            result = value

        if self.min is not None and result < self.min:
            raise ValueError(f"{result} is below the minimum {self.min}")
        if self.max is not None and result > self.max:
            raise ValueError(f"{result} is above the maximum {self.max}")
        return result


@dataclass
class Manifest:
    TYPE: ClassVar[str] = "manifest"

    run_name: str
    metrics: list[str]
    views: list[View] = field(default_factory=list)
    params: list[Param] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    pausable: bool = False

    protocol_version: int = PROTOCOL_VERSION


@dataclass
class Report:
    TYPE: ClassVar[str] = "report"

    time: float
    metrics: dict[str, MetricValue]


@dataclass
class History:
    TYPE: ClassVar[str] = "history"

    reports: list[Report] = field(default_factory=list)


@dataclass
class State:
    TYPE: ClassVar[str] = "state"

    params: dict[str, Any] = field(default_factory=dict)
    paused: bool = False
    resumable: bool = False


@dataclass
class Command:
    TYPE: ClassVar[str] = "command"

    op: str
    name: str | None = None
    value: Any = None


@dataclass
class Notice:
    TYPE: ClassVar[str] = "notice"

    level: str
    text: str


Message = Manifest | Report | History | State | Command | Notice


def _view_to_dict(view: View) -> dict[str, Any]:
    if isinstance(view, UnknownView):
        return {"kind": view.kind, **view.data}
    return {"kind": view.KIND, **asdict(view)}


def _view_from_dict(data: dict[str, Any]) -> View:
    data = dict(data)
    kind = data.pop("kind", None)
    cls = _VIEW_TYPES.get(kind)
    if cls is None:
        return UnknownView(kind=str(kind), data=data)
    return _build(cls, data)


def encode(message: Message) -> bytes:
    payload = {"type": message.TYPE, **asdict(message)}
    if isinstance(message, Manifest):
        payload["views"] = [_view_to_dict(v) for v in message.views]
    return (json.dumps(payload, separators=(",", ":")) + "\n").encode()


def _build(cls: type, data: dict[str, Any]) -> Any:
    known = {f.name for f in fields(cls)}  # ty: ignore[invalid-argument-type]
    return cls(**{k: v for k, v in data.items() if k in known})


def decode(line: bytes | str) -> Message:
    try:
        data = json.loads(line)
    except json.JSONDecodeError as e:
        raise ProtocolError(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ProtocolError(f"Expected a JSON object, got {type(data).__name__}")

    kind = data.pop("type", None)
    try:
        if kind == Manifest.TYPE:
            views = [_view_from_dict(v) for v in data.get("views", [])]
            params = [_build(Param, p) for p in data.get("params", [])]
            return _build(Manifest, {**data, "views": views, "params": params})
        if kind == Report.TYPE:
            return _build(Report, data)
        if kind == History.TYPE:
            return History(reports=[_build(Report, r) for r in data.get("reports", [])])
        if kind == State.TYPE:
            return _build(State, data)
        if kind == Command.TYPE:
            return _build(Command, data)
    except (TypeError, AttributeError, ValueError) as e:
        raise ProtocolError(f"Malformed {kind!r} message: {e}") from e
    raise UnknownMessageType(f"Unknown message type: {kind!r}")


def send_message(sock: socket.socket, message: Message) -> None:
    sock.sendall(encode(message))


def read_message(stream: BinaryIO) -> Iterator[Message]:
    for line in stream:
        line = line.strip()
        if not line:
            continue
        try:
            yield decode(line)
        except UnknownMessageType:
            continue


class MessageDecoder:
    def __init__(self, max_line: int = 1 << 20):
        self._buffer = bytearray()
        self._max_line = max_line

    def feed(self, data: bytes) -> list[Message]:
        self._buffer += data
        messages: list[Message] = []

        while True:
            end = self._buffer.find(b"\n")
            if end < 0:
                break

            line = bytes(self._buffer[:end]).strip()
            del self._buffer[: end + 1]

            if not line:
                continue

            try:
                messages.append(decode(line))
            except UnknownMessageType:
                continue

        if len(self._buffer) > self._max_line:
            raise ProtocolError(f"Line longer than {self._max_line} bytes")

        return messages
