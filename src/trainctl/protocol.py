from __future__ import annotations

import json
import socket
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, fields
from typing import Any, BinaryIO, ClassVar

PROTOCOL_VERSION = 1

MetricValue = float | list[float]


class ProtocolError(Exception):
    """A line could not be decoded into a valid message"""


class UnknownMessageType(ProtocolError):
    """A well-formed message of a type this version doesn't know"""


@dataclass
class Manifest:
    Type: ClassVar[str] = "manifest"

    run_name: str
    metrics: list[str]
    protocol_version: int = PROTOCOL_VERSION


@dataclass
class Report:
    Type: ClassVar[str] = "report"

    step: int
    time: float
    metrics: dict[str, MetricValue]


@dataclass
class History:
    Type: ClassVar[str] = "history"

    reports: list[Report] = field(default_factory=list)


Message = Manifest | Report | History


def encode(message: Message) -> bytes:
    payload = {"type": message.Type, **asdict(message)}
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
        if kind == Manifest.Type:
            return _build(Manifest, data)
        if kind == Report.Type:
            return _build(Report, data)
        if kind == History.Type:
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
