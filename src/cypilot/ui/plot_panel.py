# SPDX-License-Identifier: Apache-2.0
"""The Plot tab: live channels of any node on one time axis, recording, export and replay (G4).

The sources are the published, typed ports of every node selected so far
(``cypilot.core.signals.sources``), so channels of several nodes share one
plot. Checking a source subscribes to it on the bus thread; its fields appear
once its first messages arrived, and checking a field draws it. The recorder
keeps every subscribed source, also the fields not drawn, for the export.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from cypilot.core.registers import Register, RegisterClient
from cypilot.core.session import Session
from cypilot.core.signals import Recorder, SignalError, Source, sources, subscribe
from cypilot.ui.bus import Bus

PLOT_PERIOD_MS = 100
MAX_MARKERS = 50
"""Markers drawn at most, the latest ones."""
KEY_ROLE = Qt.ItemDataRole.UserRole
FIELD_ROLE = Qt.ItemDataRole.UserRole + 1


class PlotPanel(QWidget):
    message = Signal(str)

    def __init__(self, bus: Bus, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bus = bus
        self.live = Recorder()
        self.recorder = self.live
        """What the plot shows: the live recorder or a loaded recording."""
        self._origin = time.time()
        self._subscribers: dict[str, Any] = {}
        self._items: dict[str, QTreeWidgetItem] = {}
        self._curves: dict[tuple[str, str], Any] = {}
        self._marker_lines: list[Any] = []
        self._sources: dict[str, Source] = {}
        self._scanned: set[int] = set()
        self._updating = False

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Source / field"])
        self.tree.itemChanged.connect(self._on_item_changed)
        self.plot = pg.PlotWidget()
        self.plot.setBackground("w")
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        self.plot.setLabel("bottom", "time", units="s")
        self.plot.addLegend()

        self.span = QDoubleSpinBox(suffix=" s")
        self.span.setRange(0.5, 600.0)
        self.span.setValue(10.0)
        self.span.setToolTip("Width of the time window that follows the latest sample")
        self.pause = QCheckBox("Pause")
        self.pause.setToolTip("Stop following; the recorder keeps recording")
        self.clear_button = QPushButton("Clear")
        self.clear_button.clicked.connect(self.clear)
        self.export_button = QPushButton("Export...")
        self.export_button.setToolTip("One CSV per source and recording.json into a directory")
        self.export_button.clicked.connect(self._ask_export)
        self.load_button = QPushButton("Load...")
        self.load_button.clicked.connect(self._ask_load)
        self.live_button = QPushButton("Live")
        self.live_button.setToolTip("Back to the live channels after a loaded recording")
        self.live_button.clicked.connect(self.show_live)
        self.state = QLabel()
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Window"))
        for widget in (
            self.span,
            self.pause,
            self.clear_button,
            self.export_button,
            self.load_button,
            self.live_button,
        ):
            controls.addWidget(widget)
        controls.addStretch(1)
        controls.addWidget(self.state)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.tree)
        splitter.addWidget(self.plot)
        splitter.setSizes([260, 700])
        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(splitter)

        self.timer = QTimer(self)
        self.timer.setInterval(PLOT_PERIOD_MS)
        self.timer.timeout.connect(self.update_plot)
        self.timer.start()
        self._update_state()

    # --- Sources -------------------------------------------------------------

    def show_node(self, node_id: int | None) -> None:
        """Adds the sources of a node to the tree (once per connection); the others stay."""
        if node_id is None or node_id in self._scanned:
            return
        self._scanned.add(node_id)

        async def scan(session: Session) -> list[Source]:
            client = RegisterClient(session.node, node_id)
            try:
                names = [n for n in await client.names() if n.startswith("uavcan.pub.")]
                registers: list[Register] = await client.scan(names)
                return sources(registers, node_id)
            finally:
                client.close()

        def done(result: list[Source] | BaseException) -> None:
            if isinstance(result, BaseException):
                self._scanned.discard(node_id)
                self.message.emit(f"node {node_id}: no plot sources: {result}")
                return
            if self.recorder is self.live:
                for source in result:
                    self._add_source(source)

        self._bus.call(scan, done)

    def _add_source(self, source: Source) -> QTreeWidgetItem:
        item = self._items.get(source.key)
        if item is not None:
            return item
        self._updating = True
        item = QTreeWidgetItem([f"{source.key}  (subject {source.subject})"])
        item.setToolTip(0, source.type_name)
        item.setData(0, KEY_ROLE, source.key)
        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
        item.setCheckState(0, Qt.CheckState.Unchecked)
        self.tree.addTopLevelItem(item)
        self._items[source.key] = item
        self._sources[source.key] = source
        self._updating = False
        return item

    def source_item(self, key: str) -> QTreeWidgetItem | None:
        return self._items.get(key)

    def field_item(self, key: str, name: str) -> QTreeWidgetItem | None:
        item = self._items.get(key)
        return None if item is None else _children(item).get(name)

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._updating:
            return
        checked = item.checkState(0) == Qt.CheckState.Checked
        key = str(item.data(0, KEY_ROLE))
        name = item.data(0, FIELD_ROLE)
        if name is None:
            if self.recorder is not self.live:
                return
            if checked:
                self._subscribe(key)
            else:
                self._unsubscribe(key)
        elif checked:
            if self.recorder is self.live and key not in self._subscribers:
                parent = self._items[key]
                parent.setCheckState(0, Qt.CheckState.Checked)  # subscribes
            self._add_curve(key, str(name))
        else:
            self._remove_curve(key, str(name))

    def _subscribe(self, key: str) -> None:
        source = self._sources.get(key)
        if source is None or key in self._subscribers:
            return
        self._subscribers[key] = None  # pending
        recorder = self.live

        async def start(session: Session) -> Any:
            return subscribe(session.node, source, recorder)

        def done(result: Any) -> None:
            if isinstance(result, BaseException):
                self._subscribers.pop(key, None)
                self.message.emit(f"{key}: {result}")
                item = self._items.get(key)
                if item is not None:
                    self._updating = True
                    item.setCheckState(0, Qt.CheckState.Unchecked)
                    self._updating = False
            elif key in self._subscribers:
                self._subscribers[key] = result
                self.message.emit(f"{key}: recording {source.type_name}")
            else:  # unchecked while pending
                self._close_subscriber(result)

        self._bus.call(start, done)
        self._update_state()

    def _unsubscribe(self, key: str) -> None:
        if key not in self._subscribers:
            return
        subscriber = self._subscribers.pop(key)
        if subscriber is not None:
            self._close_subscriber(subscriber)
        self._update_state()

    def _close_subscriber(self, subscriber: Any) -> None:
        async def close(_session: Session) -> None:
            subscriber.close()

        self._bus.call(close)

    @property
    def subscribed(self) -> list[str]:
        return sorted(k for k, s in self._subscribers.items() if s is not None)

    def disconnected(self) -> None:
        """The session is gone and with it the subscribers; the tree and the samples stay."""
        self._subscribers.clear()
        self._scanned.clear()
        self._updating = True
        for item in self._items.values():
            item.setCheckState(0, Qt.CheckState.Unchecked)
        self._updating = False
        self._update_state()

    # --- Curves --------------------------------------------------------------

    def _add_curve(self, key: str, name: str) -> None:
        if (key, name) in self._curves:
            return
        color = pg.intColor(len(self._curves), hues=9)
        self._curves[(key, name)] = self.plot.plot([], [], pen=pg.mkPen(color, width=2), name=f"{key}/{name}")
        self.update_plot(force=True)

    def _remove_curve(self, key: str, name: str) -> None:
        curve = self._curves.pop((key, name), None)
        if curve is not None:
            self.plot.removeItem(curve)

    def curve_data(self, key: str, name: str) -> tuple[list[float], list[float]]:
        curve = self._curves.get((key, name))
        if curve is None:
            return [], []
        x, y = curve.getData()
        return ([] if x is None else list(x)), ([] if y is None else list(y))

    def update_plot(self, force: bool = False) -> None:
        self._add_fields()
        if not force and (self.pause.isChecked() or not self.isVisible()):
            return
        replay = self.recorder is not self.live
        latest = self._latest()
        since = None if replay or latest is None else latest - self.span.value()
        for (key, name), curve in self._curves.items():
            times, values = self.recorder.series(key, name, since)
            # NaN (a row without the field) is drawn as a gap
            curve.setData([t - self._origin for t in times], values, connect="finite")
        self._draw_markers(since)
        if latest is not None and not replay:
            self.plot.setXRange(latest - self._origin - self.span.value(), latest - self._origin, padding=0)

    def _latest(self) -> float | None:
        stamps = [self.recorder.last_time(key) for key in self.recorder.source_keys()]
        return max((s for s in stamps if s is not None), default=None)

    def _draw_markers(self, since: float | None) -> None:
        for line in self._marker_lines:
            self.plot.removeItem(line)
        self._marker_lines = []
        markers = [m for m in self.recorder.markers if since is None or m[0] >= since][-MAX_MARKERS:]
        for stamp, text in markers:
            line = pg.InfiniteLine(
                pos=stamp - self._origin,
                angle=90,
                pen=pg.mkPen("#888", style=Qt.PenStyle.DashLine),
                label=text,
                labelOpts={"position": 0.95, "color": "#555", "rotateAxis": (1, 0)},
            )
            self.plot.addItem(line)
            self._marker_lines.append(line)

    def _add_fields(self) -> None:
        """Adds the fields of the sources as their messages arrive."""
        self._updating = True
        for key in self.recorder.source_keys():
            item = self._items.get(key)
            if item is None:
                continue
            known = _children(item)
            for name in self.recorder.fields(key):
                if name not in known:
                    child = QTreeWidgetItem([name])
                    child.setData(0, KEY_ROLE, key)
                    child.setData(0, FIELD_ROLE, name)
                    child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                    child.setCheckState(0, Qt.CheckState.Unchecked)
                    item.addChild(child)
        self._updating = False

    # --- Recording -----------------------------------------------------------

    def mark(self, text: str) -> None:
        """A marker on the live time axis (a command, a register write)."""
        self.live.mark(text)

    def clear(self) -> None:
        self.recorder.clear()
        if self.recorder is self.live:
            self._origin = time.time()
        self.update_plot(force=True)

    def export(self, directory: Path) -> list[Path]:
        written = self.recorder.export(directory)
        self.message.emit(f"exported {len(written) - 1} sources to {directory}")
        return written

    def load(self, directory: Path) -> None:
        """Shows a recording instead of the live channels; :meth:`show_live` goes back."""
        try:
            recording = Recorder.load(directory)
        except (SignalError, OSError, ValueError, KeyError) as error:
            self.message.emit(f"load: {error}")
            return
        self._reset_view(recording)
        for key in recording.source_keys():
            source = recording.source(key)
            if source is not None:
                self._add_source(source)
        self._origin = self._origin_of(recording)
        self._add_fields()
        self.plot.enableAutoRange()
        self.message.emit(f"replaying {directory}: {', '.join(recording.source_keys()) or 'empty'}")
        self._update_state()

    def show_live(self) -> None:
        if self.recorder is self.live:
            return
        self._reset_view(self.live)
        self._origin = self._origin_of(self.live)
        self._scanned.clear()
        for key in self.live.source_keys():
            source = self.live.source(key)
            if source is not None:
                self._add_source(source)
        self._updating = True
        for key in self._subscribers:
            item = self._items.get(key)
            if item is not None:
                item.setCheckState(0, Qt.CheckState.Checked)
        self._updating = False
        self._update_state()

    def _reset_view(self, recorder: Recorder) -> None:
        for key, name in list(self._curves):
            self._remove_curve(key, name)
        self._updating = True
        self.tree.clear()
        self._updating = False
        self._items.clear()
        self.recorder = recorder

    @staticmethod
    def _origin_of(recorder: Recorder) -> float:
        starts = [recorder.first_time(key) for key in recorder.source_keys()]
        return min((s for s in starts if s is not None), default=time.time())

    def _ask_export(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Export the recording into")
        if directory:
            self.export(Path(directory))

    def _ask_load(self) -> None:
        directory = QFileDialog.getExistingDirectory(self, "Load a cypilot recording")
        if directory:
            self.load(Path(directory))

    def _update_state(self) -> None:
        replay = self.recorder is not self.live
        self.live_button.setEnabled(replay)
        self.pause.setEnabled(not replay)
        if replay:
            self.state.setText("replay")
        else:
            count = len(self._subscribers)
            self.state.setText(f"live, {count} source{'s' if count != 1 else ''}" if count else "live")


def _children(item: QTreeWidgetItem) -> dict[str, QTreeWidgetItem]:
    """The field items of a source item by field name."""
    found = {}
    for index in range(item.childCount()):
        child = item.child(index)
        if child is not None:
            found[str(child.data(0, FIELD_ROLE))] = child
    return found
