# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import sys

import pytest

from cypilot.core.transport import (
    CLASSIC_MTU,
    DEFAULT_BITRATE,
    FD_MTU,
    InterfaceError,
    find_adapters,
    parse_interface,
    parse_interfaces,
)


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


def test_several_interfaces_of_one_redundant_bus() -> None:
    specs = parse_interfaces(
        ["virtual:a virtual:b", "canable:COM5+pcan:PCAN_USBBUS1", "slcan:COM6,udp:10.0.0.1"]
    )
    assert [spec.text for spec in specs] == [
        "virtual:a",
        "virtual:b",
        "canable:COM5",
        "pcan:PCAN_USBBUS1",
        "slcan:COM6",
        "udp:10.0.0.1",
    ]
    with pytest.raises(InterfaceError, match="no interface"):
        parse_interfaces(["  "])


def test_describe_names_the_bit_rates() -> None:
    assert (
        parse_interface("canable:COM5").describe()
        == "canable:COM5 (1000 kbit/s, CAN FD MTU 64, no bit rate switch)"
    )
    fast = parse_interface("canable:COM5", bitrate=(500_000, 2_000_000))
    assert fast.describe().endswith("data 2000 kbit/s)")
    assert parse_interface("slcan:COM5").describe() == "slcan:COM5 (1000 kbit/s)"
    udp = parse_interface("udp:127.0.0.1")
    assert not udp.is_can and udp.describe() == "udp:127.0.0.1"


def test_find_adapters_lists_canable_ports(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from serial.tools import list_ports

    ports = [
        SimpleNamespace(device="COM5", vid=0x16D0, pid=0x117E),
        SimpleNamespace(device="COM6", vid=0x1234, pid=0x5678),
    ]
    monkeypatch.setattr(list_ports, "comports", lambda: ports)
    found = [adapter for adapter in find_adapters() if adapter.device.startswith("COM")]
    assert [(a.interface, a.description) for a in found] == [("canable:COM5", "CANable 2.0 (slcan firmware)")]
