# SPDX-License-Identifier: Apache-2.0
"""The register description file of UNIMOC (from v0.3.0 on): unit, limits, effect and help per register.

UNIMOC writes it with ``native_sim --describe-registers=FILE`` and attaches it
to each release (``unimoc-registers-<version>.json``,
:func:`cypilot.simulation.fetch_register_descriptions`). It only adds text and
limits to registers scanned from a node; which registers exist still comes
from the node.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = 1
"""The schema version of the file cypilot reads."""

APPLIES = ("immediate", "disengaged", "restart")
"""When a written value takes effect: at once, the next time the drive is disengaged, after a restart."""


class DescriptionError(ValueError):
    """The description file cannot be read."""


@dataclass(frozen=True)
class RegisterDescription:
    name: str
    type: str
    size: int
    mutable: bool
    persistent: bool
    default: tuple[Any, ...] = ()
    unit: str | None = None
    min: float | None = None
    max: float | None = None
    applies: str | None = None
    help: str = ""


@dataclass(frozen=True)
class Descriptions:
    firmware: dict[str, Any]
    """``name``, ``version``, ``vcs_revision``, ``target`` of the firmware that wrote the file."""
    registers: dict[str, RegisterDescription]

    def get(self, name: str) -> RegisterDescription | None:
        return self.registers.get(name)


def load_descriptions(source: Path | str | dict[str, Any]) -> Descriptions:
    """Reads a description file (a path, its JSON text or the parsed JSON)."""
    try:
        if isinstance(source, Path):
            data = json.loads(source.read_text(encoding="utf-8"))
        elif isinstance(source, str):
            data = json.loads(source)
        else:
            data = source
    except (OSError, json.JSONDecodeError) as error:
        raise DescriptionError(f"cannot read the register description file: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("registers"), list):
        raise DescriptionError("not a register description file (no registers list)")
    schema = data.get("schema")
    if schema != SCHEMA:
        raise DescriptionError(f"register description schema {schema!r}; cypilot reads {SCHEMA}")
    registers: dict[str, RegisterDescription] = {}
    for item in data["registers"]:
        try:
            description = RegisterDescription(
                name=str(item["name"]),
                type=str(item["type"]),
                size=int(item["size"]),
                mutable=bool(item["mutable"]),
                persistent=bool(item["persistent"]),
                default=tuple(item.get("default", ())),
                unit=item.get("unit"),
                min=item.get("min"),
                max=item.get("max"),
                applies=item.get("applies"),
                help=str(item.get("help", "")),
            )
        except (KeyError, TypeError, ValueError) as error:
            raise DescriptionError(f"bad register entry {item!r}: {error}") from error
        registers[description.name] = description
    return Descriptions(firmware=dict(data.get("firmware") or {}), registers=registers)
