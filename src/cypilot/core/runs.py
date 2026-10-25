# SPDX-License-Identifier: Apache-2.0
"""Runs: time spans with everything on the bus, recorded on the computer that runs cypilot.

A run is started and stopped by the user only (``docs/web_plan.md``, section 6).
While it records, its directory holds:

- ``frames.log``: every CAN frame in the ``candump -L`` format (replay with
  ``canplayer``);
- ``node<N>_<port>.csv``: the decoded subjects, in the format of
  :meth:`cypilot.core.signals.Recorder.export` (flushed every second);
- ``capture<K>_node<N>.csv``: scope captures taken during the run;
- ``run.json``: start, end, nodes, register snapshots, interface and markers.

Closing a run writes ``recording.json`` (so :meth:`Recorder.load` replays it)
and packs the data files into ``data.zip`` next to ``run.json``. A run that was
still recording when cypilot stopped is closed and kept when the store opens
again, never continued. Nothing is deleted automatically.
"""

from __future__ import annotations

import csv
import datetime
import io
import json
import math
import os
import shutil
import sys
import tempfile
import time
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from cypilot.core.signals import EXPORT_FORMAT, Recorder, SignalError, Source

RUN_FORMAT = 1
RUN_FILE = "run.json"
DATA_FILE = "data.zip"
FRAMES_FILE = "frames.log"
RECORDING_FILE = "recording.json"
DEFAULT_WARN_FREE = 1 << 30
"""Free bytes below which the Runs view warns."""
DEFAULT_STOP_FREE = 200 << 20
"""Free bytes below which a recording run stops instead of filling the disk."""


class RunError(RuntimeError):
    """A run cannot be started, changed or read."""


def default_runs_dir() -> Path:
    """``$CYPILOT_RUNS``, else ``%LOCALAPPDATA%\\cypilot\\runs`` or ``$XDG_DATA_HOME/cypilot/runs``."""
    override = os.environ.get("CYPILOT_RUNS")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "cypilot" / "runs"


@dataclass(frozen=True)
class RunLimits:
    warn_free: int = DEFAULT_WARN_FREE
    stop_free: int = DEFAULT_STOP_FREE


def _now() -> float:
    return time.time()


def _iso(stamp: float) -> str:
    return datetime.datetime.fromtimestamp(stamp, datetime.UTC).isoformat(timespec="milliseconds")


def _number(value: float) -> str:
    return "" if isinstance(value, float) and math.isnan(value) else repr(value)


@dataclass
class _Table:
    stream: IO[str]
    writer: Any
    columns: list[str]
    entry: dict[str, Any]


class ActiveRun:
    """The run that records now; all calls on the session's loop."""

    def __init__(self, directory: Path, meta: dict[str, Any], clock: Callable[[], float] = _now) -> None:
        self.directory = directory
        self.meta = meta
        self._clock = clock
        self._frames = (directory / FRAMES_FILE).open("w", encoding="ascii", newline="\n")
        self._tables: dict[str, _Table] = {}
        self._captures = 0
        self._dirty = True
        self.frames = 0
        self.samples = 0
        self.write_meta()

    @property
    def run_id(self) -> str:
        return str(self.meta["id"])

    @property
    def started(self) -> float:
        return float(self.meta["start"])

    # --- Data ------------------------------------------------------------------

    def frame(self, stamp: float, iface: int, identifier: int, data: bytes, fd: bool) -> None:
        """One CAN frame (extended ID) in the ``candump -L`` format."""
        payload = data.hex().upper()
        separator = "##0" if fd else "#"
        self._frames.write(f"({stamp:.6f}) can{iface} {identifier:08X}{separator}{payload}\n")
        self.frames += 1

    def sample(self, source: Source, stamp: float, row: dict[str, float]) -> None:
        """One decoded message; the columns of a source are those of its first message."""
        table = self._tables.get(source.key)
        if table is None:
            name = f"node{source.node_id}_{source.port}.csv"
            stream = (self.directory / name).open("w", newline="")
            writer = csv.writer(stream)
            columns = list(row)
            writer.writerow(["time", *columns])
            entry = {
                "file": name,
                "node_id": source.node_id,
                "port": source.port,
                "subject": source.subject,
                "type": source.type_name,
            }
            table = self._tables[source.key] = _Table(stream, writer, columns, entry)
            self.meta["sources"].append(entry)
            self._dirty = True
        table.writer.writerow([f"{stamp:.6f}", *(_number(row.get(c, math.nan)) for c in table.columns)])
        self.samples += 1

    def mark(self, text: str, stamp: float | None = None, kind: str = "event") -> None:
        """A marker on the time axis: control, stops, writes, trips, notes of the user."""
        self.meta["markers"].append(
            {"time": self._clock() if stamp is None else stamp, "text": text, "kind": kind}
        )
        self._dirty = True

    def add_capture(self, node_id: int, capture: Any, labels: list[str] | None = None) -> str:
        """A scope capture (``cypilot.core.scope.Capture``) as a CSV in the run."""
        self._captures += 1
        name = f"capture{self._captures}_node{node_id}.csv"
        with (self.directory / name).open("w", newline="") as stream:
            writer = csv.writer(stream)
            channels = list(capture.channels)
            header = (
                labels
                if labels and len(labels) == len(channels)
                else [f"ch{i}" for i in range(len(channels))]
            )
            writer.writerow(["t_s", *header])
            for index, t in enumerate(capture.t):
                writer.writerow([repr(t), *(repr(channel[index]) for channel in channels)])
        self.meta["captures"].append({"file": name, "node_id": node_id, "time": self._clock()})
        self.mark(f"capture of node {node_id}", kind="capture")
        return name

    def set_node(self, node_id: int, info: dict[str, Any]) -> None:
        self.meta["nodes"].setdefault(str(node_id), {}).update(info)
        self._dirty = True

    # --- Upkeep ----------------------------------------------------------------

    def flush(self) -> None:
        self._frames.flush()
        for table in self._tables.values():
            table.stream.flush()
        if self._dirty:
            self.write_meta()

    def write_meta(self) -> None:
        temporary = self.directory / (RUN_FILE + ".tmp")
        temporary.write_text(json.dumps(self.meta, indent=1) + "\n")
        temporary.replace(self.directory / RUN_FILE)
        self._dirty = False

    def size(self) -> int:
        return sum(path.stat().st_size for path in self.directory.iterdir() if path.is_file())

    def summary(self) -> dict[str, Any]:
        now = self._clock()
        return _summary(self.meta) | {
            "duration": now - self.started,
            "size": self.size(),
            "frames": self.frames,
            "samples": self.samples,
        }

    def close(self, reason: str) -> dict[str, Any]:
        self.mark(f"stopped: {reason}", kind="stop")
        self._frames.close()
        for table in self._tables.values():
            table.stream.close()
        self.meta["end"] = self._clock()
        self.meta["stop_reason"] = reason
        return _finish(self.directory, self.meta)


def _recording_json(meta: dict[str, Any]) -> dict[str, Any]:
    """The ``recording.json`` of :meth:`Recorder.export`, so the plots can replay the run."""
    return {
        "format": EXPORT_FORMAT,
        "sources": meta.get("sources", []),
        "markers": [{"time": m["time"], "text": m["text"]} for m in meta.get("markers", [])],
    }


def _finish(directory: Path, meta: dict[str, Any]) -> dict[str, Any]:
    """Packs the data files of a run into ``data.zip`` and marks it closed."""
    (directory / RECORDING_FILE).write_text(json.dumps(_recording_json(meta), indent=1) + "\n")
    files = sorted(p for p in directory.iterdir() if p.is_file() and p.name not in (RUN_FILE, DATA_FILE))
    files = [p for p in files if not p.name.endswith(".tmp")]
    temporary = directory / (DATA_FILE + ".tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in files:
            archive.write(path, path.name)
    temporary.replace(directory / DATA_FILE)
    meta["files"] = [{"name": p.name, "size": p.stat().st_size} for p in files]
    meta["state"] = "closed"
    for path in files:
        path.unlink()
    (directory / RUN_FILE).write_text(json.dumps(meta, indent=1) + "\n")
    return _summary(meta) | {"size": _size(directory)}


def _size(directory: Path) -> int:
    return sum(path.stat().st_size for path in directory.iterdir() if path.is_file())


def _summary(meta: dict[str, Any]) -> dict[str, Any]:
    start = float(meta["start"])
    end = meta.get("end")
    return {
        "id": meta["id"],
        "name": meta.get("name", ""),
        "notes": meta.get("notes", ""),
        "state": meta.get("state", "closed"),
        "start": start,
        "start_text": _iso(start),
        "end": end,
        "duration": (float(end) - start) if end is not None else None,
        "nodes": sorted(int(n) for n in meta.get("nodes", {})),
        "markers": len(meta.get("markers", [])),
        "stop_reason": meta.get("stop_reason"),
    }


class RunStore:
    """The runs in one directory; at most one records at a time."""

    def __init__(
        self, root: Path | None = None, limits: RunLimits | None = None, clock: Callable[[], float] = _now
    ) -> None:
        self.root = root or default_runs_dir()
        self.limits = limits or RunLimits()
        self._clock = clock
        self.active: ActiveRun | None = None
        self.root.mkdir(parents=True, exist_ok=True)
        self._recover()

    def _recover(self) -> None:
        """Closes the runs that were recording when cypilot stopped; they are kept, not continued."""
        for directory in sorted(self.root.iterdir()):
            meta = self._read_meta(directory)
            if meta is None or meta.get("state") != "recording":
                continue
            files = [p for p in directory.iterdir() if p.is_file()]
            end = max((p.stat().st_mtime for p in files), default=float(meta["start"]))
            meta["markers"].append({"time": end, "text": "closed when cypilot started again", "kind": "stop"})
            meta["end"] = end
            meta["stop_reason"] = "cypilot stopped while recording"
            _finish(directory, meta)

    @staticmethod
    def _read_meta(directory: Path) -> dict[str, Any] | None:
        try:
            meta = json.loads((directory / RUN_FILE).read_text())
        except (OSError, ValueError):
            return None
        return meta if isinstance(meta, dict) and meta.get("format") == RUN_FORMAT else None

    def _directory(self, run_id: str) -> Path:
        directory = self.root / run_id
        if "/" in run_id or "\\" in run_id or run_id.startswith(".") or self._read_meta(directory) is None:
            raise LookupError(f"no run {run_id}")
        return directory

    # --- Space -----------------------------------------------------------------

    def space(self) -> dict[str, Any]:
        usage = shutil.disk_usage(self.root)
        return {
            "free": usage.free,
            "total": usage.total,
            "warn": usage.free < self.limits.warn_free,
            "warn_free": self.limits.warn_free,
            "stop_free": self.limits.stop_free,
        }

    def out_of_space(self) -> bool:
        return shutil.disk_usage(self.root).free < self.limits.stop_free

    # --- Runs ------------------------------------------------------------------

    def summaries(self) -> list[dict[str, Any]]:
        runs = []
        for directory in sorted(self.root.iterdir(), reverse=True):
            if self.active is not None and directory == self.active.directory:
                runs.append(self.active.summary())
                continue
            meta = self._read_meta(directory)
            if meta is not None:
                runs.append(_summary(meta) | {"size": _size(directory)})
        return runs

    def get(self, run_id: str) -> dict[str, Any]:
        """The whole ``run.json`` of a run."""
        if self.active is not None and self.active.run_id == run_id:
            return dict(self.active.meta)
        meta = self._read_meta(self._directory(run_id))
        assert meta is not None
        return meta

    def start(self, name: str = "", notes: str = "", info: dict[str, Any] | None = None) -> ActiveRun:
        if self.active is not None:
            raise RunError(f"run {self.active.run_id} records already")
        if self.out_of_space():
            raise RunError("not enough free space for a run")
        now = self._clock()
        base = datetime.datetime.fromtimestamp(now).strftime("%Y%m%d-%H%M%S")
        run_id, suffix = base, 1
        while (self.root / run_id).exists():
            suffix += 1
            run_id = f"{base}-{suffix}"
        directory = self.root / run_id
        directory.mkdir()
        meta: dict[str, Any] = {
            "format": RUN_FORMAT,
            "id": run_id,
            "name": name.strip(),
            "notes": notes,
            "state": "recording",
            "start": now,
            "end": None,
            "nodes": {},
            "sources": [],
            "captures": [],
            "markers": [{"time": now, "text": "started", "kind": "start"}],
        } | (info or {})
        self.active = ActiveRun(directory, meta, self._clock)
        return self.active

    def stop(self, reason: str = "stopped by the user") -> dict[str, Any] | None:
        active, self.active = self.active, None
        return active.close(reason) if active is not None else None

    def update(self, run_id: str, name: str | None = None, notes: str | None = None) -> dict[str, Any]:
        """Renames a run or changes its notes (also while it records)."""
        if self.active is not None and self.active.run_id == run_id:
            if name is not None:
                self.active.meta["name"] = name.strip()
            if notes is not None:
                self.active.meta["notes"] = notes
            self.active.write_meta()
            return self.active.summary()
        directory = self._directory(run_id)
        meta = self._read_meta(directory)
        assert meta is not None
        if name is not None:
            meta["name"] = name.strip()
        if notes is not None:
            meta["notes"] = notes
        (directory / RUN_FILE).write_text(json.dumps(meta, indent=1) + "\n")
        return _summary(meta) | {"size": _size(directory)}

    def delete(self, run_id: str) -> None:
        if self.active is not None and self.active.run_id == run_id:
            raise RunError("stop the run before deleting it")
        shutil.rmtree(self._directory(run_id))

    # --- Reading ---------------------------------------------------------------

    def _closed(self, run_id: str) -> Path:
        if self.active is not None and self.active.run_id == run_id:
            raise RunError("the run records; stop it first")
        return self._directory(run_id)

    def files(self, run_id: str) -> list[dict[str, Any]]:
        directory = self._closed(run_id)
        with zipfile.ZipFile(directory / DATA_FILE) as archive:
            entries = [{"name": i.filename, "size": i.file_size} for i in archive.infolist()]
        return [{"name": RUN_FILE, "size": (directory / RUN_FILE).stat().st_size}, *entries]

    def read_file(self, run_id: str, name: str) -> bytes:
        directory = self._closed(run_id)
        if name == RUN_FILE:
            return (directory / RUN_FILE).read_bytes()
        with zipfile.ZipFile(directory / DATA_FILE) as archive:
            try:
                return archive.read(name)
            except KeyError:
                raise LookupError(f"run {run_id} has no file {name}") from None

    def archive(self, run_id: str) -> Iterator[bytes]:
        """The run as one zip (the data files and ``run.json`` in a folder named after the run)."""
        directory = self._closed(run_id)
        buffer = io.BytesIO()
        with zipfile.ZipFile(directory / DATA_FILE) as source, zipfile.ZipFile(buffer, "w") as target:
            target.write(directory / RUN_FILE, f"{run_id}/{RUN_FILE}")
            for info in source.infolist():
                with source.open(info) as member:
                    target.writestr(f"{run_id}/{info.filename}", member.read(), zipfile.ZIP_DEFLATED)
        yield buffer.getvalue()

    def recorder(self, run_id: str) -> Recorder:
        """The decoded subjects and markers of a closed run, for the plots."""
        directory = self._closed(run_id)
        with tempfile.TemporaryDirectory() as temporary:
            with zipfile.ZipFile(directory / DATA_FILE) as archive:
                names = [n for n in archive.namelist() if n == RECORDING_FILE or n.endswith(".csv")]
                for name in names:
                    if "/" in name or "\\" in name:
                        continue
                    (Path(temporary) / name).write_bytes(archive.read(name))
            try:
                return Recorder.load(Path(temporary))
            except (SignalError, OSError, KeyError) as error:
                raise RunError(f"run {run_id} cannot be replayed: {error}") from error


def replay_json(recorder: Recorder, max_points: int = 20000) -> dict[str, Any]:
    """The channels of a recorder as JSON for the plots, each source thinned to ``max_points`` rows."""
    sources = []
    for key in recorder.source_keys():
        source = recorder.source(key)
        assert source is not None
        fields = recorder.fields(key)
        times, _ = recorder.series(key, fields[0]) if fields else ([], [])
        step = max(1, math.ceil(len(times) / max_points))
        values = {}
        for name in fields:
            _, series = recorder.series(key, name)
            values[name] = [None if math.isnan(v) else v for v in series[::step]]
        sources.append(
            {
                "key": key,
                "node_id": source.node_id,
                "port": source.port,
                "subject": source.subject,
                "type": source.type_name,
                "t": times[::step],
                "fields": values,
            }
        )
    return {"sources": sources, "markers": [{"at": t, "text": text} for t, text in recorder.markers]}
