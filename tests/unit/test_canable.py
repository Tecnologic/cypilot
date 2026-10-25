# SPDX-License-Identifier: Apache-2.0
"""The CANable slcan bus: FD frames with and without bit rate switch, rate checks, pycyphal media."""

from __future__ import annotations

import can
import pytest

from cypilot.core import canable
from cypilot.core.transport import make_transport, parse_interface, parse_interfaces


def _bus(**kwargs: object) -> canable.CanableBus:
    return can.Bus(interface="canable", channel="loop://", sleep_after_open=0, **kwargs)  # type: ignore[return-value]


def _sent(bus: canable.CanableBus, message: can.Message) -> list[str]:
    written: list[str] = []
    bus._write = written.append  # type: ignore[method-assign]
    bus.send(message)
    return written


def test_fd_without_bit_rate_switch_at_the_nominal_rate() -> None:
    bus = _bus(bitrate=1_000_000, data_bitrate=1_000_000)
    message = can.Message(arbitration_id=0x10ABCDEF, is_fd=True, bitrate_switch=True, data=bytes(12))
    assert _sent(bus, message) == ["D10ABCDEF9" + "00" * 12]
    bus.shutdown()


def test_fd_with_bit_rate_switch_at_the_data_rate() -> None:
    bus = _bus(bitrate=500_000, data_bitrate=2_000_000)
    assert _sent(bus, can.Message(arbitration_id=0x1, is_fd=True, data=b"\x01")) == ["B00000001101"]
    bus.shutdown()


def test_classic_frame_passes_through() -> None:
    bus = _bus(bitrate=1_000_000)
    assert _sent(bus, can.Message(arbitration_id=0x123, data=b"\xaa")) == ["T000001231AA"]
    bus.shutdown()


def test_unsupported_rates_are_rejected() -> None:
    with pytest.raises(ValueError, match="nominal"):
        _bus(bitrate=1_234_000)
    with pytest.raises(ValueError, match="data bit rate"):
        _bus(bitrate=1_000_000, data_bitrate=4_000_000)


def test_canable_media_and_a_redundant_transport() -> None:
    transport = make_transport(parse_interface("canable:loop://"), 5)
    assert transport.protocol_parameters.mtu == 63  # CAN FD: 64 bytes less the tail byte
    transport.close()
    redundant = make_transport(parse_interfaces(["virtual:a", "virtual:b"]), 5)
    assert type(redundant).__name__ == "RedundantTransport"
    assert len(redundant.inferiors) == 2
    redundant.close()
