# SPDX-License-Identifier: Apache-2.0
"""Cyphal registers: scanned from the node, never from a fixed list.

``uavcan.register.List`` gives the names (index 0, 1, ... until the empty name),
``uavcan.register.Access`` with an empty value reads one, with a value writes
it. A UNIMOC node answers a rejected write with the unchanged value, so a write
is only accepted when the answer equals the request.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

Scalar = int | float | bool
Content = list[Scalar] | str | bytes | None

ARRAY_KINDS = (
    "bit",
    "integer64",
    "integer32",
    "integer16",
    "integer8",
    "natural64",
    "natural32",
    "natural16",
    "natural8",
    "real64",
    "real32",
    "real16",
)
KINDS = ("empty", "string", "unstructured", *ARRAY_KINDS)
MAX_REGISTERS = 1024
"""Upper bound of a scan, against a node that never answers with the empty name."""


class RegisterError(RuntimeError):
    """A register service did not answer or answered something unusable."""


@dataclass(frozen=True)
class RegisterValue:
    """A register value as plain Python data: the kind of the Cyphal union and its content."""

    kind: str
    content: Content

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown register value kind {self.kind!r}")

    def format(self) -> str:
        """Text for tables and the CLI; :func:`parse_like` reads it back."""
        if self.kind == "empty" or self.content is None:
            return ""
        if isinstance(self.content, str):
            return self.content
        if isinstance(self.content, bytes):
            return self.content.hex()
        return ", ".join(_format_scalar(self.kind, item) for item in self.content)

    def same_as(self, other: RegisterValue) -> bool:
        """Equal after the rounding of the register type (``real32`` and ``real16``)."""
        if self.kind != other.kind:
            return False
        if not isinstance(self.content, list) or not isinstance(other.content, list):
            return self.content == other.content
        if len(self.content) != len(other.content):
            return False
        return all(
            _rounded(self.kind, a) == _rounded(self.kind, b)
            or (isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b))
            for a, b in zip(self.content, other.content, strict=True)
        )


@dataclass(frozen=True)
class Register:
    name: str
    value: RegisterValue
    mutable: bool
    persistent: bool


@dataclass(frozen=True)
class WriteResult:
    accepted: bool
    register: Register


def parse_like(text: str, template: RegisterValue) -> RegisterValue:
    """Parses user text into the kind of an existing register value.

    Arrays are comma separated; their length must match the register, a single
    value fills a one-element register. ``bit`` takes 1/0, true/false, on/off.
    """
    kind = template.kind
    if kind == "string":
        return RegisterValue(kind, text)
    if kind == "unstructured":
        try:
            return RegisterValue(kind, bytes.fromhex(text))
        except ValueError as error:
            raise ValueError(f"expected hex bytes, got {text!r}") from error
    if kind == "empty":
        raise ValueError("the register is empty and takes no value")
    items = [item.strip() for item in text.split(",")]
    expected = len(template.content) if isinstance(template.content, list) else len(items)
    if len(items) != expected:
        raise ValueError(f"expected {expected} value(s), got {len(items)}")
    return RegisterValue(kind, [_parse_scalar(kind, item) for item in items])


def to_dsdl(value: RegisterValue) -> Any:
    """The ``uavcan.register.Value.1.0`` of a :class:`RegisterValue` (needs the compiled DSDL)."""
    import uavcan.primitive
    import uavcan.primitive.array
    import uavcan.register

    if value.kind == "empty":
        return uavcan.register.Value_1_0(empty=uavcan.primitive.Empty_1_0())
    if value.kind == "string":
        assert isinstance(value.content, str)
        return uavcan.register.Value_1_0(string=uavcan.primitive.String_1_0(value.content))
    if value.kind == "unstructured":
        assert isinstance(value.content, bytes)
        return uavcan.register.Value_1_0(unstructured=uavcan.primitive.Unstructured_1_0(value.content))
    assert isinstance(value.content, list)
    array_type = getattr(uavcan.primitive.array, value.kind.capitalize() + "_1_0")
    return uavcan.register.Value_1_0(**{value.kind: array_type(value.content)})


def from_dsdl(value: Any) -> RegisterValue:
    """The :class:`RegisterValue` of a ``uavcan.register.Value.1.0``."""
    if value.string is not None:
        return RegisterValue("string", value.string.value.tobytes().decode(errors="replace"))
    if value.unstructured is not None:
        return RegisterValue("unstructured", value.unstructured.value.tobytes())
    for kind in ARRAY_KINDS:
        field = getattr(value, kind)
        if field is not None:
            return RegisterValue(kind, [_python_scalar(kind, item) for item in field.value])
    return RegisterValue("empty", None)


class RegisterClient:
    """Register access of one remote node, on a running pycyphal node."""

    def __init__(self, node: Any, server_node_id: int, timeout: float = 1.0, retries: int = 2) -> None:
        import uavcan.register

        self._list = node.make_client(uavcan.register.List_1_0, server_node_id)
        self._access = node.make_client(uavcan.register.Access_1_0, server_node_id)
        for client in (self._list, self._access):
            client.response_timeout = timeout
        self._retries = retries
        self.server_node_id = server_node_id

    def close(self) -> None:
        self._list.close()
        self._access.close()

    async def names(self) -> list[str]:
        import uavcan.register

        names: list[str] = []
        for index in range(MAX_REGISTERS):
            response = await self._call(self._list, uavcan.register.List_1_0.Request(index))
            name = response.name.name.tobytes().decode(errors="replace")
            if not name:
                return names
            names.append(name)
        raise RegisterError(f"node {self.server_node_id} lists more than {MAX_REGISTERS} registers")

    async def read(self, name: str) -> Register:
        import uavcan.register

        request = uavcan.register.Access_1_0.Request(name=uavcan.register.Name_1_0(name))
        return _register(name, await self._call(self._access, request))

    async def write(self, name: str, value: RegisterValue) -> WriteResult:
        import uavcan.register

        request = uavcan.register.Access_1_0.Request(
            name=uavcan.register.Name_1_0(name), value=to_dsdl(value)
        )
        register = _register(name, await self._call(self._access, request))
        return WriteResult(accepted=register.value.same_as(value), register=register)

    async def scan(self, names: Sequence[str] | None = None) -> list[Register]:
        """Reads every register of the node (or the given ones), in list order."""
        result = []
        for name in names if names is not None else await self.names():
            result.append(await self.read(name))
        return result

    async def _call(self, client: Any, request: Any) -> Any:
        for _ in range(self._retries + 1):
            result = await client.call(request)
            if result is not None:
                return result[0]
        raise RegisterError(f"node {self.server_node_id} did not answer {type(request).__name__}")


def _register(name: str, response: Any) -> Register:
    return Register(
        name=name,
        value=from_dsdl(response.value),
        mutable=bool(response.mutable),
        persistent=bool(response.persistent),
    )


def _python_scalar(kind: str, item: Any) -> Scalar:
    if kind == "bit":
        return bool(item)
    if kind.startswith("real"):
        return float(item)
    return int(item)


def _parse_scalar(kind: str, text: str) -> Scalar:
    if kind == "bit":
        lowered = text.lower()
        if lowered in ("1", "true", "on", "yes"):
            return True
        if lowered in ("0", "false", "off", "no"):
            return False
        raise ValueError(f"expected a bit, got {text!r}")
    if kind.startswith("real"):
        return float(text)
    number = int(text, 0)
    bits = int(kind.removeprefix("integer").removeprefix("natural"))
    low, high = (
        (-(1 << (bits - 1)), (1 << (bits - 1)) - 1) if kind.startswith("integer") else (0, (1 << bits) - 1)
    )
    if not low <= number <= high:
        raise ValueError(f"{number} is out of the range of {kind} ({low} to {high})")
    return number


def _format_scalar(kind: str, item: Scalar) -> str:
    if kind == "bit":
        return "1" if item else "0"
    if kind == "real32" or kind == "real16":
        return f"{float(item):.7g}"
    return str(item)


def _rounded(kind: str, item: Scalar) -> Scalar:
    if kind == "real32":
        return float(struct.unpack("<f", struct.pack("<f", float(item)))[0])
    if kind == "real16":
        return float(struct.unpack("<e", struct.pack("<e", float(item)))[0])
    return item
