# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import sys

import pytest

from cypilot.core.transport import CLASSIC_MTU, DEFAULT_BITRATE, FD_MTU, InterfaceError, parse_interface


def test_slcan_is_classic_can() -> None:
    spec = parse_interface("slcan:/dev/pts/3")
    assert (spec.kind, spec.channel, spec.mtu, spec.is_fd) == ("slcan", "/dev/pts/3", CLASSIC_MTU, False)
    assert parse_interface("SLCAN:COM5").text == "slcan:COM5"


def test_slcan_refuses_can_fd() -> None:
    with pytest.raises(InterfaceError, match="classic"):
        parse_interface("slcan:COM5", mtu=FD_MTU)


def test_python_can_adapters_default_to_can_fd() -> None:
    spec = parse_interface("pcan:PCAN_USBBUS1")
    assert spec.mtu == FD_MTU and spec.bitrate == DEFAULT_BITRATE
    spec = parse_interface("gs_usb:0", mtu=8, bitrate=(500_000, 500_000))
    assert spec.mtu == 8 and spec.bitrate == (500_000, 500_000)


@pytest.mark.parametrize("text", ["", "vcan0", "socketcan:", ":vcan0"])
def test_malformed_interfaces(text: str) -> None:
    with pytest.raises(InterfaceError):
        parse_interface(text)


def test_invalid_mtu_and_bitrate() -> None:
    with pytest.raises(InterfaceError, match="MTU"):
        parse_interface("pcan:PCAN_USBBUS1", mtu=13)
    with pytest.raises(InterfaceError, match="positive"):
        parse_interface("pcan:PCAN_USBBUS1", bitrate=(0, 1))


@pytest.mark.skipif(sys.platform == "linux", reason="SocketCAN exists on Linux")
def test_socketcan_is_linux_only() -> None:
    with pytest.raises(InterfaceError, match="Linux"):
        parse_interface("socketcan:vcan0")


@pytest.mark.skipif(sys.platform != "linux", reason="SocketCAN exists on Linux only")
def test_socketcan_defaults_to_can_fd() -> None:
    assert parse_interface("socketcan:vcan0").mtu == FD_MTU
