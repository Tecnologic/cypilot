# SPDX-License-Identifier: Apache-2.0
"""CAN interfaces as text, the same syntax as yakut's ``UAVCAN__CAN__IFACE``.

Examples::

    socketcan:vcan0          Linux, the simulation cluster (CAN FD, MTU 64)
    socketcan:can0           Linux, a CAN FD adapter with a kernel driver (candleLight, a Pi HAT)
    canable:/dev/ttyACM0     CANable 2.0 with the slcan firmware in CAN FD mode (any OS; COM5 on Windows)
    slcan:/dev/pts/3         a pseudo terminal of the UNIMOC simulation (classic CAN)
    slcan:COM5               an slcan adapter in classic CAN
    pcan:PCAN_USBBUS1        PEAK adapters through python-can (CAN FD)
    gs_usb:0                 candleLight adapters through python-can
    virtual:demo             python-can's in-process bus (demo devices, tests)
    udp:127.0.0.1            Cyphal/UDP on the local interface with this address

Any other ``<interface>:<channel>`` goes to python-can unchanged. Several
interfaces form one redundant transport: every transfer goes out on all of
them, duplicates are dropped on reception (UNIMOC supports redundant buses).
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pycyphal.transport

CLASSIC_MTU = 8
FD_MTU = 64
DEFAULT_BITRATE = (1_000_000, 1_000_000)
"""Arbitration and data bit rate. The BCC sends CAN FD frames at 1 Mbit/s without bit rate switch."""

_CLASSIC_ONLY = {"slcan"}
_NOT_CAN = {"udp"}

SERIAL_ADAPTERS = {
    (0x16D0, 0x117E): ("CANable 2.0 (slcan firmware)", "canable"),
}
"""USB IDs of the serial CAN adapters cypilot drives. A candleLight adapter is no serial port; on Linux it
is a SocketCAN interface."""


class InterfaceError(ValueError):
    """The interface text is invalid or the interface is not available on this platform."""


@dataclass(frozen=True)
class InterfaceSpec:
    kind: str
    channel: str
    mtu: int
    bitrate: tuple[int, int]

    @property
    def text(self) -> str:
        return f"{self.kind}:{self.channel}"

    @property
    def is_fd(self) -> bool:
        return self.mtu > CLASSIC_MTU

    @property
    def is_can(self) -> bool:
        return self.kind not in _NOT_CAN

    def describe(self) -> str:
        """``canable:COM5 (1000 kbit/s, CAN FD MTU 64, no bit rate switch)``."""
        if not self.is_can:
            return self.text
        arbitration, data = self.bitrate
        rate = f"{arbitration // 1000} kbit/s"
        if self.is_fd:
            rate += f", CAN FD MTU {self.mtu}, " + (
                f"data {data // 1000} kbit/s" if data != arbitration else "no bit rate switch"
            )
        return f"{self.text} ({rate})"


@dataclass(frozen=True)
class Adapter:
    """A CAN adapter found on this computer."""

    device: str
    description: str
    interface: str
    """The interface text that opens it."""


def parse_interface(
    text: str, *, mtu: int | None = None, bitrate: tuple[int, int] | None = None
) -> InterfaceSpec:
    """Parses ``<kind>:<channel>``; ``mtu`` and ``bitrate`` override the defaults of the kind."""
    kind, separator, channel = text.strip().partition(":")
    kind = kind.lower()
    if not separator or not kind or not channel:
        raise InterfaceError(f"expected <interface>:<channel>, got {text!r}")
    if kind in _CLASSIC_ONLY:
        if mtu not in (None, CLASSIC_MTU):
            raise InterfaceError(f"{kind} carries classic CAN only (MTU {CLASSIC_MTU})")
        resolved_mtu = CLASSIC_MTU
    else:
        resolved_mtu = FD_MTU if mtu is None else mtu
    if resolved_mtu not in (CLASSIC_MTU, 12, 16, 20, 24, 32, 48, FD_MTU):
        raise InterfaceError(f"invalid CAN MTU {resolved_mtu}")
    if kind == "socketcan" and sys.platform != "linux":
        raise InterfaceError("SocketCAN exists on Linux only (on Windows: WSL2 or a python-can adapter)")
    resolved_bitrate = bitrate or DEFAULT_BITRATE
    if min(resolved_bitrate) <= 0:
        raise InterfaceError("bit rates must be positive")
    return InterfaceSpec(kind=kind, channel=channel, mtu=resolved_mtu, bitrate=resolved_bitrate)


def parse_interfaces(
    texts: Sequence[str], *, mtu: int | None = None, bitrate: tuple[int, int] | None = None
) -> list[InterfaceSpec]:
    """Several interfaces of one redundant bus; one text may also hold several, separated by spaces (as in
    yakut's ``UAVCAN__CAN__IFACE``), ``+`` or ``,``."""
    parts = [part for text in texts for part in text.replace("+", " ").replace(",", " ").split()]
    if not parts:
        raise InterfaceError("no interface given (e.g. socketcan:vcan0, canable:COM5, slcan:/dev/pts/3)")
    return [parse_interface(part, mtu=mtu, bitrate=bitrate) for part in parts]


def make_transport(
    specs: InterfaceSpec | Sequence[InterfaceSpec], local_node_id: int | None
) -> pycyphal.transport.Transport:
    """Opens the interfaces as one transport with the given local node-ID (None: anonymous, for PnP).

    One interface gives its transport; several give a redundant transport over all of them.
    """
    group = [specs] if isinstance(specs, InterfaceSpec) else list(specs)
    if not group:
        raise InterfaceError("no interface given")
    transports = []
    try:
        for spec in group:
            transports.append(_open(spec, local_node_id))
    except Exception:
        for transport in transports:
            transport.close()
        raise
    if len(transports) == 1:
        return transports[0]
    from pycyphal.transport.redundant import RedundantTransport

    redundant = RedundantTransport()
    for transport in transports:
        redundant.attach_inferior(transport)
    return redundant


def describe(specs: Sequence[InterfaceSpec]) -> str:
    return " + ".join(spec.describe() for spec in specs)


def _open(spec: InterfaceSpec, local_node_id: int | None) -> pycyphal.transport.Transport:
    if spec.kind == "udp":
        from pycyphal.transport.udp import UDPTransport

        return UDPTransport(spec.channel, local_node_id=local_node_id)
    from pycyphal.transport.can import CANTransport

    if local_node_id is not None and not 0 <= local_node_id <= 127:
        raise InterfaceError(f"invalid CAN node-ID {local_node_id}")
    media: object
    if spec.kind == "socketcan":
        from pycyphal.transport.can.media.socketcan import SocketCANMedia

        media = SocketCANMedia(spec.channel, spec.mtu)
    else:
        from pycyphal.transport.can.media.pythoncan import PythonCANMedia

        if spec.kind == "canable":
            from cypilot.core import canable

            canable.register()
        bitrate: int | tuple[int, int] = spec.bitrate if spec.is_fd else spec.bitrate[0]
        media = PythonCANMedia(spec.text, bitrate, spec.mtu)
    return CANTransport(media, local_node_id=local_node_id)


def find_adapters() -> list[Adapter]:
    """CAN adapters connected to this computer: USB serial adapters and SocketCAN interfaces (Linux)."""
    from serial.tools import list_ports

    found: list[Adapter] = []
    for port in list_ports.comports():
        known = SERIAL_ADAPTERS.get((port.vid or 0, port.pid or 0))
        if known:
            found.append(Adapter(port.device, known[0], f"{known[1]}:{port.device}"))
    net = Path("/sys/class/net")
    if net.is_dir():
        for path in sorted(net.iterdir()):
            try:
                if (path / "type").read_text().strip() != "280":  # ARPHRD_CAN
                    continue
                state = (path / "operstate").read_text().strip()
                fd = "CAN FD" if (path / "mtu").read_text().strip() == "72" else "classic CAN"
            except OSError:
                continue
            found.append(Adapter(path.name, f"SocketCAN, {state}, {fd}", f"socketcan:{path.name}"))
    return found
