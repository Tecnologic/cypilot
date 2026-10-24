# SPDX-License-Identifier: Apache-2.0
"""CAN interfaces as text, the same syntax as yakut's ``UAVCAN__CAN__IFACE``.

Examples::

    socketcan:vcan0          Linux, the simulation cluster (CAN FD, MTU 64)
    socketcan:can0           Linux, a CAN FD adapter with a kernel driver
    slcan:/dev/pts/3         a pseudo terminal of the UNIMOC simulation (classic CAN)
    slcan:/dev/ttyACM0       CANable with slcan firmware (classic CAN)
    slcan:COM5               the same on Windows
    pcan:PCAN_USBBUS1        PEAK adapters through python-can (CAN FD)
    gs_usb:0                 candleLight adapters through python-can

Any other ``<interface>:<channel>`` goes to python-can unchanged.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import pycyphal.transport

CLASSIC_MTU = 8
FD_MTU = 64
DEFAULT_BITRATE = (1_000_000, 1_000_000)
"""Arbitration and data bit rate. The BCC sends CAN FD frames at 1 Mbit/s without bit rate switch."""

_CLASSIC_ONLY = {"slcan"}


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


def make_transport(spec: InterfaceSpec, local_node_id: int) -> pycyphal.transport.Transport:
    """Opens the interface as a Cyphal/CAN transport with the given local node-ID."""
    from pycyphal.transport.can import CANTransport

    if not 0 <= local_node_id <= 127:
        raise InterfaceError(f"invalid CAN node-ID {local_node_id}")
    media: object
    if spec.kind == "socketcan":
        from pycyphal.transport.can.media.socketcan import SocketCANMedia

        media = SocketCANMedia(spec.channel, spec.mtu)
    else:
        from pycyphal.transport.can.media.pythoncan import PythonCANMedia

        bitrate: int | tuple[int, int] = spec.bitrate if spec.is_fd else spec.bitrate[0]
        media = PythonCANMedia(spec.text, bitrate, spec.mtu)
    return CANTransport(media, local_node_id=local_node_id)
