# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from cypilot.core.status import Chip, Health, PowerStage, VendorStatus, chips


def test_vendor_bits() -> None:
    status = VendorStatus.decode(0b1111_1001)
    assert status.power_stage == PowerStage.ENABLED
    assert status.engaged and status.trip_latched and status.emergency_stop
    assert status.settings_pending and status.local_control
    assert VendorStatus.decode(0x03) == VendorStatus(PowerStage.BLOCKED, False, False, False, False, False)
    assert VendorStatus.decode(0x05).power_stage == PowerStage.UNKNOWN
    with pytest.raises(ValueError):
        VendorStatus.decode(256)


def test_chips_of_a_unimoc_drive() -> None:
    def of(code: int, health: Health = Health.NOMINAL) -> list[Chip]:
        return chips(online=True, health=health, vendor_code=code, unimoc=True)

    assert of(0x00) == [Chip.DISABLED]
    assert of(0x09) == [Chip.ENGAGED]
    assert of(0x01) == [Chip.ENABLED]
    assert of(0x03) == [Chip.BLOCKED]
    assert of(0x12, Health.ADVISORY) == [Chip.TRIPPED, Chip.FAULT]
    assert of(0x20) == [Chip.E_STOP]
    assert of(0x40) == [Chip.DISABLED, Chip.SETTINGS_PENDING]
    assert of(0x80) == [Chip.DISABLED, Chip.LOCAL_CONTROL]


def test_emergency_stop_of_firmware_without_bit_5() -> None:
    # UNIMOC v0.1.0 reports the latched E-stop only as health CAUTION.
    assert chips(online=True, health=Health.CAUTION, vendor_code=0, unimoc=True) == [Chip.E_STOP]


def test_other_nodes_get_no_unimoc_chips() -> None:
    assert chips(online=True, health=Health.CAUTION, vendor_code=0x09, unimoc=False) == []
    assert chips(online=False, health=Health.NOMINAL, vendor_code=0, unimoc=True) == [Chip.OFFLINE]
