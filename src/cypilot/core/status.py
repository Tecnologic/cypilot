# SPDX-License-Identifier: Apache-2.0
"""Node status from the Cyphal heartbeat, without any bus or DSDL import.

UNIMOC nodes put their drive state into the vendor-specific status code of the
heartbeat (UNIMOC ``docs/gui_plan.md``, section 6):

====  ===========================================================
Bits  Meaning
====  ===========================================================
0..2  power stage: 0 disabled, 1 enabled, 2 fault, 3 blocked
3     engaged
4     trip latched (overcurrent or overvoltage), cleared by STANDBY
5     emergency stop latched, cleared by a restart
6     settings pending (wait for the disengage)
7     local control: the node engages by itself (e-bike)
====  ===========================================================

Bits 4 to 7 come with UNIMOC v0.2.0 (F1). With older firmware they read 0;
the emergency stop is then recognized from the health CAUTION, the only case in
which a UNIMOC drive reports it.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Health(enum.IntEnum):
    NOMINAL = 0
    ADVISORY = 1
    CAUTION = 2
    WARNING = 3


class Mode(enum.IntEnum):
    OPERATIONAL = 0
    INITIALIZATION = 1
    MAINTENANCE = 2
    SOFTWARE_UPDATE = 3


class PowerStage(enum.IntEnum):
    DISABLED = 0
    ENABLED = 1
    FAULT = 2
    BLOCKED = 3
    UNKNOWN = 7


class Chip(enum.Enum):
    """What the GUI shows as the state of a node; several may apply at once."""

    OFFLINE = "offline"
    E_STOP = "E-stop latched"
    TRIPPED = "tripped"
    FAULT = "fault"
    BLOCKED = "blocked"
    ENGAGED = "engaged"
    ENABLED = "enabled"
    DISABLED = "disabled"
    SETTINGS_PENDING = "settings pending"
    LOCAL_CONTROL = "local control"


@dataclass(frozen=True)
class VendorStatus:
    """The decoded vendor-specific status code of a UNIMOC node."""

    power_stage: PowerStage
    engaged: bool
    trip_latched: bool
    emergency_stop: bool
    settings_pending: bool
    local_control: bool

    @staticmethod
    def decode(code: int) -> VendorStatus:
        if not 0 <= code <= 0xFF:
            raise ValueError(f"vendor status code out of range: {code}")
        stage = code & 0x07
        return VendorStatus(
            power_stage=PowerStage(stage) if stage <= PowerStage.BLOCKED else PowerStage.UNKNOWN,
            engaged=bool(code & 0x08),
            trip_latched=bool(code & 0x10),
            emergency_stop=bool(code & 0x20),
            settings_pending=bool(code & 0x40),
            local_control=bool(code & 0x80),
        )


def chips(*, online: bool, health: Health, vendor_code: int, unimoc: bool) -> list[Chip]:
    """The state chips of a node, most important first.

    ``unimoc`` says whether the vendor code follows the UNIMOC layout; for other
    nodes only the online state counts here (health and mode are shown apart).
    """
    if not online:
        return [Chip.OFFLINE]
    if not unimoc:
        return []
    status = VendorStatus.decode(vendor_code)
    result: list[Chip] = []
    if status.emergency_stop or health == Health.CAUTION:
        result.append(Chip.E_STOP)
    if status.trip_latched:
        result.append(Chip.TRIPPED)
    if status.power_stage == PowerStage.FAULT:
        result.append(Chip.FAULT)
    if status.power_stage == PowerStage.BLOCKED:
        result.append(Chip.BLOCKED)
    if status.engaged:
        result.append(Chip.ENGAGED)
    elif status.power_stage == PowerStage.ENABLED:
        result.append(Chip.ENABLED)
    elif status.power_stage == PowerStage.DISABLED and not result:
        result.append(Chip.DISABLED)
    if status.settings_pending:
        result.append(Chip.SETTINGS_PENDING)
    if status.local_control:
        result.append(Chip.LOCAL_CONTROL)
    return result
