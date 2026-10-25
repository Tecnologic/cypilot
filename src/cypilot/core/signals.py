# SPDX-License-Identifier: Apache-2.0
"""Live signals of any node: subjects as named numeric channels, a recorder, export and replay (G4).

Nothing here names a message type. The sources of a node are its published
ports with a ``.type`` register (``cypilot.core.capabilities.ports``); the type
name (``unimoc.drive.Telemetry.0.1``) selects the compiled DSDL class, and every
numeric field of a message becomes a channel ``<node>/<port>/<field>``, arrays
as ``field[i]``, booleans as 0 and 1.

The time stamp of a sample is the reception time of its transfer on this
machine, one clock for all nodes (``docs/plan.md``, section 8.1).
"""

from __future__ import annotations

import csv
import importlib
import json
import math
import threading
import time
from collections import deque
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cypilot.core.capabilities import ports
from cypilot.core.registers import Register

DEFAULT_HISTORY = 600.0
"""Seconds of samples the recorder keeps per source."""
EXPORT_FORMAT = 1


class SignalError(RuntimeError):
    """A type name does not resolve to a compiled DSDL type, or an export is unreadable."""


@dataclass(frozen=True)
class Source:
    """One published subject of a node with its DSDL type."""

    node_id: int
    port: str
    subject: int
    type_name: str

    @property
    def key(self) -> str:
        return f"{self.node_id}/{self.port}"


def sources(registers: Iterable[Register], node_id: int) -> list[Source]:
    """The published subjects of a node that can be decoded (they have a ``.type`` register)."""
    return [
        Source(node_id, p.name, p.port_id, p.type_name)
        for p in ports(registers, node_id)
        if p.kind == "pub" and p.port_id is not None and p.type_name
    ]


def load_type(type_name: str) -> Any:
    """The compiled class of ``ns.sub.Type.major.minor`` (the DSDL must be on ``sys.path``).

    Without a version (``uavcan.node.Heartbeat``) the newest compiled version.
    """
    parts = type_name.strip().split(".")
    versioned = len(parts) >= 4 and parts[-1].isdigit() and parts[-2].isdigit()
    if not versioned and len(parts) < 2:
        raise SignalError(f"not a DSDL type name: {type_name!r}")
    module_name = ".".join(parts[:-3] if versioned else parts[:-1])
    short = parts[-3] if versioned else parts[-1]
    try:
        module = importlib.import_module(module_name)
    except ImportError as error:
        raise SignalError(f"no compiled DSDL namespace {module_name}") from error
    if versioned:
        found = getattr(module, f"{short}_{parts[-2]}_{parts[-1]}", None)
    else:
        versions = [
            name
            for name in dir(module)
            if name.startswith(short + "_") and name[len(short) + 1 :].replace("_", "").isdigit()
        ]
        newest = max(
            versions, key=lambda name: tuple(int(v) for v in name[len(short) + 1 :].split("_")), default=None
        )
        found = getattr(module, newest) if newest else None
    if found is None:
        raise SignalError(f"no compiled DSDL type {type_name}")
    return found


def type_name(data_type: Any) -> str:
    """``unimoc.drive.Telemetry.0.1`` of a compiled class."""
    import pycyphal.dsdl

    model = pycyphal.dsdl.get_model(data_type)
    return f"{model.full_name}.{model.version.major}.{model.version.minor}"


def flatten(message: Any) -> dict[str, float]:
    """The numeric fields of a message as ``{name: value}``; arrays as ``name[i]``, nested as ``a.b``."""
    import pycyphal.dsdl

    return flatten_builtin(pycyphal.dsdl.to_builtin(message))


def flatten_builtin(value: Any) -> dict[str, float]:
    """:func:`flatten` of a message already converted with ``pycyphal.dsdl.to_builtin``."""
    result: dict[str, float] = {}
    _flatten(value, "", result)
    return result


def _flatten(value: Any, name: str, out: dict[str, float]) -> None:
    if isinstance(value, bool):
        out[name] = 1.0 if value else 0.0
    elif isinstance(value, int | float):
        out[name] = float(value)
    elif isinstance(value, dict):
        for key, item in value.items():
            _flatten(item, f"{name}.{key}" if name else key, out)
    elif isinstance(value, list) and len(value) <= 64 and all(isinstance(v, int | float) for v in value):
        for index, item in enumerate(value):
            _flatten(item, f"{name}[{index}]", out)
    # strings, byte arrays and long arrays are no signals


@dataclass
class Trace:
    """The samples of one source: reception times and the flattened rows."""

    source: Source
    times: deque[float] = field(default_factory=deque)
    rows: deque[dict[str, float]] = field(default_factory=deque)

    @property
    def fields(self) -> list[str]:
        names: dict[str, None] = {}
        for row in list(self.rows)[-50:]:
            names.update(dict.fromkeys(row))
        return list(names)


class Recorder:
    """Samples of many sources, thread-safe: written on the bus thread, read by the UI.

    It also keeps markers (register writes, commands) on the same clock.
    """

    def __init__(self, history: float = DEFAULT_HISTORY, clock: Any = time.time) -> None:
        self.history = history
        self._clock = clock
        self._lock = threading.Lock()
        self._traces: dict[str, Trace] = {}
        self.markers: list[tuple[float, str]] = []

    def add(self, source: Source, row: dict[str, float], stamp: float | None = None) -> None:
        now = self._clock() if stamp is None else stamp
        with self._lock:
            trace = self._traces.setdefault(source.key, Trace(source))
            trace.times.append(now)
            trace.rows.append(row)
            while trace.times and trace.times[0] < now - self.history:
                trace.times.popleft()
                trace.rows.popleft()

    def mark(self, text: str, stamp: float | None = None) -> None:
        now = self._clock() if stamp is None else stamp
        with self._lock:
            self.markers.append((now, text))
            while self.markers and self.markers[0][0] < now - self.history:
                del self.markers[0]

    def source_keys(self) -> list[str]:
        with self._lock:
            return sorted(self._traces)

    def source(self, key: str) -> Source | None:
        with self._lock:
            trace = self._traces.get(key)
            return trace.source if trace else None

    def count(self, key: str) -> int:
        with self._lock:
            trace = self._traces.get(key)
            return len(trace.times) if trace else 0

    def first_time(self, key: str) -> float | None:
        with self._lock:
            trace = self._traces.get(key)
            return trace.times[0] if trace and trace.times else None

    def last_time(self, key: str) -> float | None:
        with self._lock:
            trace = self._traces.get(key)
            return trace.times[-1] if trace and trace.times else None

    def fields(self, key: str) -> list[str]:
        with self._lock:
            trace = self._traces.get(key)
            return trace.fields if trace else []

    def series(self, key: str, name: str, since: float | None = None) -> tuple[list[float], list[float]]:
        """Times and values of one channel (NaN where a row lacks it), from ``since`` on."""
        with self._lock:
            trace = self._traces.get(key)
            if trace is None:
                return [], []
            pairs = [
                (t, row.get(name, math.nan))
                for t, row in zip(trace.times, trace.rows, strict=True)
                if since is None or t >= since
            ]
        return [t for t, _ in pairs], [v for _, v in pairs]

    def clear(self) -> None:
        with self._lock:
            self._traces.clear()
            self.markers.clear()

    # --- Export and replay ---------------------------------------------------

    def export(self, directory: Path) -> list[Path]:
        """One CSV per source (``time`` and its fields, wide) and ``recording.json`` with the metadata."""
        directory.mkdir(parents=True, exist_ok=True)
        written: list[Path] = []
        meta: dict[str, Any] = {"format": EXPORT_FORMAT, "sources": [], "markers": []}
        with self._lock:
            traces = list(self._traces.values())
            markers = list(self.markers)
        for trace in traces:
            source = trace.source
            name = f"node{source.node_id}_{source.port}.csv"
            columns = trace.fields
            with (directory / name).open("w", newline="") as stream:
                writer = csv.writer(stream)
                writer.writerow(["time", *columns])
                for stamp, row in zip(trace.times, trace.rows, strict=True):
                    writer.writerow([f"{stamp:.6f}", *(_number(row.get(c, math.nan)) for c in columns)])
            written.append(directory / name)
            meta["sources"].append(
                {
                    "file": name,
                    "node_id": source.node_id,
                    "port": source.port,
                    "subject": source.subject,
                    "type": source.type_name,
                }
            )
        meta["markers"] = [{"time": stamp, "text": text} for stamp, text in markers]
        (directory / "recording.json").write_text(json.dumps(meta, indent=2) + "\n")
        written.append(directory / "recording.json")
        return written

    @staticmethod
    def load(directory: Path) -> Recorder:
        """A recorder with the samples of an export, for replay in the plots."""
        try:
            meta = json.loads((directory / "recording.json").read_text())
        except (OSError, ValueError) as error:
            raise SignalError(f"no cypilot recording in {directory}: {error}") from error
        if meta.get("format") != EXPORT_FORMAT:
            raise SignalError(f"unknown recording format {meta.get('format')!r}")
        recorder = Recorder(history=math.inf)
        for entry in meta["sources"]:
            source = Source(
                int(entry["node_id"]), str(entry["port"]), int(entry["subject"]), str(entry["type"])
            )
            with (directory / entry["file"]).open(newline="") as stream:
                for row in _rows(csv.reader(stream)):
                    stamp = row.pop("time")
                    recorder.add(source, row, stamp)
        for marker in meta.get("markers", []):
            recorder.mark(str(marker["text"]), float(marker["time"]))
        return recorder


def _number(value: float) -> str:
    return "" if math.isnan(value) else repr(value)


def _rows(reader: Iterator[list[str]]) -> Iterator[dict[str, float]]:
    header = next(reader, None)
    if not header or header[0] != "time":
        raise SignalError("a recording CSV starts with a time column")
    for line in reader:
        yield {name: float(cell) if cell else math.nan for name, cell in zip(header, line, strict=True)}


def subscribe(node: Any, source: Source, recorder: Recorder) -> Any:
    """Records every message of ``source`` (only from its node: one subject may carry several nodes).

    Returns the pycyphal subscriber; ``close()`` it to stop.
    """
    subscriber = node.make_subscriber(load_type(source.type_name), source.subject)

    async def on_message(message: Any, transfer: Any) -> None:
        if transfer.source_node_id == source.node_id:
            recorder.add(source, flatten(message))

    subscriber.receive_in_background(on_message)
    return subscriber
