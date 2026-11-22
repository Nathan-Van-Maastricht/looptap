from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, fields
from typing import Any, BinaryIO, ClassVar

PROTOCOL_VERSION = 2

MetricValue = float | list[float]


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
    window: int = 10

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
class Manifest:
    TYPE: ClassVar[str] = "manifest"

    run_name: str
    metrics: list[str]
    views: list[View] = field(default_factory=list)
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


Message = Manifest | Report | History


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
            return _build(Manifest, {**data, "views": views})
        if kind == Report.TYPE:
            return _build(Report, data)
        if kind == History.TYPE:
            return History(reports=[_build(Report, r) for r in data.get("reports", [])])
    except (TypeError, AttributeError) as e:
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
