# SPDX-License-Identifier: Apache-2.0
"""Register profiles: the persistent settings of a node as a JSON file.

A profile holds the mutable, persistent registers of one node, taken from a
scan. It can be compared with a node and loaded into the same or another node
(``docs/plan.md``, section 5): diff first, then write, verify and store.

Loading skips the registers that identify a node or place it on the bus unless
asked: the port-IDs (``uavcan.pub|sub|srv|cln.*.id``), ``uavcan.node.id`` and
the role label ``uavcan.node.description``. Copying one drive's settings to the
other three drives must not move them onto the same subjects or give them the
same node-ID or name.

Some settings are validated against each other (a timeout below another, a
derating start below the trip voltage), so one write can be refused until
another has been made. Loading therefore repeats the refused writes while
each pass makes progress.
"""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from cypilot.core.registers import Register, RegisterValue, WriteResult

PROFILE_FORMAT = 1
"""Version of the JSON layout; :func:`Profile.from_json` refuses others."""

_PORT_ID = re.compile(r"^uavcan\.(pub|sub|srv|cln)\.[^.]+\.id$")
_IDENTITY = frozenset({"uavcan.node.id", "uavcan.node.description"})
MAX_PASSES = 4


class ProfileError(ValueError):
    """A profile file is not readable or has another format version."""


@dataclass(frozen=True)
class Profile:
    """The persistent settings of a node and where they came from."""

    registers: dict[str, RegisterValue]
    node_name: str | None = None
    software_version: tuple[int, int] | None = None
    vcs_revision: int | None = None
    created: str = ""

    @staticmethod
    def from_registers(
        registers: Iterable[Register],
        *,
        node_name: str | None = None,
        software_version: tuple[int, int] | None = None,
        vcs_revision: int | None = None,
    ) -> Profile:
        """The mutable, persistent registers of a scan."""
        return Profile(
            registers={r.name: r.value for r in registers if r.mutable and r.persistent},
            node_name=node_name,
            software_version=software_version,
            vcs_revision=vcs_revision,
            created=datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
        )

    def to_json(self) -> str:
        data = {
            "format": PROFILE_FORMAT,
            "node_name": self.node_name,
            "software_version": list(self.software_version) if self.software_version else None,
            "vcs_revision": f"{self.vcs_revision:x}" if self.vcs_revision is not None else None,
            "created": self.created,
            "registers": {name: value_to_json(value) for name, value in sorted(self.registers.items())},
        }
        return json.dumps(data, indent=2) + "\n"

    @staticmethod
    def from_json(text: str) -> Profile:
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or data.get("format") != PROFILE_FORMAT:
                raise ProfileError(f"not a cypilot profile of format {PROFILE_FORMAT}")
            version = data.get("software_version")
            revision = data.get("vcs_revision")
            return Profile(
                registers={name: value_from_json(value) for name, value in data["registers"].items()},
                node_name=data.get("node_name"),
                software_version=(int(version[0]), int(version[1])) if version else None,
                vcs_revision=int(revision, 16) if revision else None,
                created=str(data.get("created", "")),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, ProfileError):
                raise
            raise ProfileError(f"invalid profile: {error}") from error


def value_to_json(value: RegisterValue) -> dict[str, Any]:
    content = value.content
    return {"kind": value.kind, "value": content.hex() if isinstance(content, bytes) else content}


def value_from_json(data: Mapping[str, Any]) -> RegisterValue:
    kind = data["kind"]
    content = data.get("value")
    if kind == "unstructured":
        return RegisterValue(kind, bytes.fromhex(str(content)))
    if kind == "string":
        return RegisterValue(kind, str(content))
    if kind == "empty":
        return RegisterValue(kind, None)
    if not isinstance(content, list):
        raise ValueError(f"{kind} needs a list of values")
    if kind == "bit":
        return RegisterValue(kind, [bool(item) for item in content])
    if kind.startswith("real"):
        return RegisterValue(kind, [float(item) for item in content])
    return RegisterValue(kind, [int(item) for item in content])


def skipped(name: str, *, include_ports: bool = False, include_identity: bool = False) -> bool:
    """True for a register that loading leaves alone unless asked."""
    if _PORT_ID.match(name):
        return not include_ports
    if name in _IDENTITY:
        return not include_identity
    return False


@dataclass(frozen=True)
class Difference:
    """One register where a profile and a node disagree."""

    name: str
    profile: RegisterValue | None  # None: only on the node
    node: RegisterValue | None  # None: the node does not have it

    @property
    def kind(self) -> str:
        """``changed``, ``type`` (another value type), ``missing`` (not on the node) or ``extra``."""
        if self.node is None:
            return "missing"
        if self.profile is None:
            return "extra"
        return "type" if self.profile.kind != self.node.kind else "changed"


def diff(profile: Profile, registers: Iterable[Register]) -> list[Difference]:
    """The differences between a profile and a node's scan, by name.

    Only mutable, persistent registers of the node count; read-only values
    (types, counters) are never part of a profile.
    """
    node = {r.name: r.value for r in registers if r.mutable and r.persistent}
    result = []
    for name in sorted(profile.registers.keys() | node.keys()):
        wanted, actual = profile.registers.get(name), node.get(name)
        if wanted is not None and actual is not None and wanted.same_as(actual):
            continue
        result.append(Difference(name, wanted, actual))
    return result


class RegisterWriter(Protocol):
    async def write(self, name: str, value: RegisterValue) -> WriteResult: ...


@dataclass
class LoadResult:
    written: list[str] = field(default_factory=list)
    rejected: dict[str, RegisterValue] = field(default_factory=dict)
    """Refused writes, with the value the node kept."""
    skipped: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    """In the profile, not on the node."""
    incompatible: list[str] = field(default_factory=list)
    """Another value type on the node."""

    @property
    def complete(self) -> bool:
        """Every register that was to be written was accepted."""
        return not self.rejected and not self.incompatible


async def load(
    profile: Profile,
    registers: Iterable[Register],
    writer: RegisterWriter,
    *,
    include_ports: bool = False,
    include_identity: bool = False,
) -> LoadResult:
    """Writes the differences of a profile into a node (in RAM; storing is a separate command)."""
    result = LoadResult()
    pending: dict[str, RegisterValue] = {}
    for difference in diff(profile, registers):
        if difference.kind == "extra":
            continue
        if skipped(difference.name, include_ports=include_ports, include_identity=include_identity):
            result.skipped.append(difference.name)
        elif difference.kind == "missing":
            result.missing.append(difference.name)
        elif difference.kind == "type":
            result.incompatible.append(difference.name)
        else:
            assert difference.profile is not None
            pending[difference.name] = difference.profile

    for attempt in range(MAX_PASSES):
        refused: dict[str, RegisterValue] = {}
        kept: dict[str, RegisterValue] = {}
        for name, value in pending.items():
            outcome = await writer.write(name, value)
            if outcome.accepted:
                result.written.append(name)
            else:
                refused[name] = value
                kept[name] = outcome.register.value
        if not refused:
            break
        if len(refused) == len(pending) or attempt == MAX_PASSES - 1:
            result.rejected = kept  # no progress any more
            break
        pending = refused
    return result
