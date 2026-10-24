# SPDX-License-Identifier: Apache-2.0
"""The connection dialog: interface, CAN MTU, bit rates and the local node-ID."""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QSpinBox,
    QWidget,
)

from cypilot.core.session import DEFAULT_LOCAL_NODE_ID
from cypilot.core.transport import InterfaceError, InterfaceSpec, parse_interface

CAN_LINK_TYPE = "280"  # ARPHRD_CAN in /sys/class/net/<link>/type
MTU_CHOICES = (("Default of the interface", None), ("Classic CAN (8)", 8), ("CAN FD (64)", 64))


def discover_interfaces() -> list[str]:
    """Interface texts found on this machine: SocketCAN links (Linux) and serial ports as slcan."""
    found: list[str] = []
    if sys.platform == "linux":
        net = Path("/sys/class/net")
        if net.is_dir():
            for link in sorted(net.iterdir()):
                try:
                    if (link / "type").read_text().strip() == CAN_LINK_TYPE:
                        found.append(f"socketcan:{link.name}")
                except OSError:
                    continue
    try:
        from serial.tools import list_ports

        found += [f"slcan:{port.device}" for port in sorted(list_ports.comports(), key=lambda p: p.device)]
    except ImportError:
        pass
    return found


class ConnectDialog(QDialog):
    """Asks for the interface; remembers the last choices in the Qt settings."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Connect to a CAN bus")
        self._settings = QSettings("Tecnologic", "cypilot")

        self.interface = QComboBox(editable=True)
        last = str(self._settings.value("connect/interface", ""))
        choices = discover_interfaces()
        if last and last not in choices:
            choices.insert(0, last)
        self.interface.addItems(choices)
        self.interface.setCurrentText(last or (choices[0] if choices else ""))
        self.interface.setToolTip(
            "socketcan:vcan0, slcan:/dev/pts/3 (a simulated drive), slcan:COM5, pcan:PCAN_USBBUS1, gs_usb:0"
        )
        self.mtu = QComboBox()
        for label, value in MTU_CHOICES:
            self.mtu.addItem(label, value)
        saved_mtu = self._settings.value("connect/mtu")
        for index, (_, value) in enumerate(MTU_CHOICES):
            if str(value) == str(saved_mtu):
                self.mtu.setCurrentIndex(index)
        self.bitrate = QLineEdit(str(self._settings.value("connect/bitrate", "")))
        self.bitrate.setPlaceholderText("1000000/1000000 (for adapters that need it)")
        self.local_node_id = QSpinBox(minimum=0, maximum=127)
        self.local_node_id.setValue(
            int(str(self._settings.value("connect/local_node_id", DEFAULT_LOCAL_NODE_ID)))
        )
        self.error = QLabel()
        self.error.setStyleSheet("color: #c62828")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form = QFormLayout(self)
        form.addRow("Interface", self.interface)
        form.addRow("CAN MTU", self.mtu)
        form.addRow("Bit rates", self.bitrate)
        form.addRow("Local node-ID", self.local_node_id)
        form.addRow(self.error)
        form.addRow(buttons)
        self.spec: InterfaceSpec | None = None

    def _accept(self) -> None:
        try:
            bitrate = _bitrate(self.bitrate.text())
            self.spec = parse_interface(
                self.interface.currentText(), mtu=self.mtu.currentData(), bitrate=bitrate
            )
        except (InterfaceError, ValueError) as error:
            self.error.setText(str(error))
            return
        self._settings.setValue("connect/interface", self.interface.currentText())
        self._settings.setValue("connect/mtu", self.mtu.currentData())
        self._settings.setValue("connect/bitrate", self.bitrate.text())
        self._settings.setValue("connect/local_node_id", self.local_node_id.value())
        self.accept()


def _bitrate(text: str) -> tuple[int, int] | None:
    text = text.strip()
    if not text:
        return None
    arbitration, _, data = text.partition("/")
    return int(arbitration), int(data or arbitration)
