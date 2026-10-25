# SPDX-License-Identifier: Apache-2.0
"""DSDL sources and their compilation for pycyphal.

cypilot carries no copy of the DSDL. It takes the namespaces of a UNIMOC
release (``dsdl/unimoc``), the cymon namespace of the cymon-lib commit that
release pins (``lib/cymon``) and the public regulated data types at the
revision of the release's ``west.yml``. They are downloaded once into the user
cache, or taken from a local UNIMOC west workspace, and compiled with
pycyphal (about a minute on the first run).

Select the source with ``CYPILOT_UNIMOC``: a git ref of
https://github.com/Tecnologic/unimoc (default :data:`DEFAULT_UNIMOC_REF`) or
the path of a local ``unimoc`` checkout inside its west workspace.

Other projects' root namespaces (a VDC, a BMS, any Cyphal node) are added with
``CYPILOT_DSDL``: directories of root namespaces separated by ``os.pathsep``
(``:`` on Linux, ``;`` on Windows), compiled against the standard ones.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

DEFAULT_UNIMOC_REF = "v0.3.0"
"""The UNIMOC release cypilot is tested against."""

NAMESPACES = ("uavcan", "reg", "unimoc", "cymon")

_UNIMOC_REPO = "Tecnologic/unimoc"
_CYMON_LIB_REPO = "Tecnologic/cymon-lib"
_PUBLIC_DSDL_REPO = "OpenCyphal/public_regulated_data_types"
_SAFE_REF = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")


class DsdlError(RuntimeError):
    """The DSDL could not be found, downloaded or compiled."""


@dataclass(frozen=True)
class DsdlSource:
    """Where the DSDL comes from: a UNIMOC git ref or a local checkout."""

    unimoc_ref: str | None = None
    unimoc_path: Path | None = None

    @staticmethod
    def from_environment() -> DsdlSource:
        value = os.environ.get("CYPILOT_UNIMOC", "").strip()
        if not value:
            return DsdlSource(unimoc_ref=DEFAULT_UNIMOC_REF)
        path = Path(value).expanduser()
        if path.is_dir():
            return DsdlSource(unimoc_path=path.absolute())
        return DsdlSource(unimoc_ref=value)

    @property
    def key(self) -> str:
        """Name of the cache directory of this source."""
        if self.unimoc_path is not None:
            digest = hashlib.sha1(str(self.unimoc_path).encode()).hexdigest()[:12]
            return f"local-{digest}"
        assert self.unimoc_ref is not None
        return "ref-" + re.sub(r"[^A-Za-z0-9._-]", "_", self.unimoc_ref)


def cache_root() -> Path:
    """The cypilot cache: ``%LOCALAPPDATA%\\cypilot`` on Windows, else ``$XDG_CACHE_HOME/cypilot``."""
    override = os.environ.get("CYPILOT_CACHE")
    if override:
        return Path(override).expanduser()
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "cypilot"


def ensure_dsdl(source: DsdlSource | None = None, extra_roots: Sequence[str | Path] | None = None) -> Path:
    """Makes the compiled DSDL importable and returns its directory.

    Downloads and compiles on the first call for a source; later calls only put
    the compiled packages on ``sys.path``. A local checkout is recompiled when one
    of its ``.dsdl`` files is newer than the last compilation. ``extra_roots``
    (default ``CYPILOT_DSDL``) are compiled into a directory of their own, keyed
    by their content.
    """
    source = source or DsdlSource.from_environment()
    base = cache_root() / "dsdl" / source.key
    sources = base / "src"
    compiled = base / "py"
    stamp = compiled / ".complete"

    if source.unimoc_path is not None:
        roots = _local_roots(source.unimoc_path)
        newest = max(p.stat().st_mtime for root in roots.values() for p in root.rglob("*.dsdl"))
        if not stamp.exists() or stamp.stat().st_mtime < newest:
            _compile(roots, compiled)
    elif not stamp.exists():
        assert source.unimoc_ref is not None
        if sources.exists():
            shutil.rmtree(sources)
        manifest = _download(source.unimoc_ref, sources)
        _compile({name: sources / name for name in NAMESPACES}, compiled)
        (base / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    if str(compiled) not in sys.path:
        sys.path.insert(0, str(compiled))
    if extra_roots is None:
        extra_roots = [part for part in os.environ.get("CYPILOT_DSDL", "").split(os.pathsep) if part.strip()]
    if extra_roots:
        standard = _local_roots(source.unimoc_path) if source.unimoc_path else _ref_roots(sources)
        extra = _extra_dsdl(standard, [Path(root).expanduser().resolve() for root in extra_roots])
        if str(extra) not in sys.path:
            sys.path.insert(0, str(extra))
    return compiled


def _ref_roots(sources: Path) -> dict[str, Path]:
    return {name: sources / name for name in NAMESPACES}


def _extra_dsdl(standard: dict[str, Path], roots: list[Path]) -> Path:
    for root in roots:
        if not root.is_dir() or not any(root.rglob("*.dsdl")):
            raise DsdlError(f"{root} is no DSDL root namespace (a directory with .dsdl files)")
        if root.name in standard:
            raise DsdlError(f"the root namespace {root.name} is part of the UNIMOC DSDL already")
    names = [root.name for root in roots]
    if len(set(names)) != len(names):
        raise DsdlError(f"a root namespace is given twice: {', '.join(names)}")
    digest = hashlib.sha256()
    for root in sorted(roots):
        for path in sorted(root.rglob("*.dsdl")):
            digest.update(str(path.relative_to(root.parent)).encode())
            digest.update(path.read_bytes())
    output = cache_root() / "dsdl" / f"extra-{digest.hexdigest()[:16]}"
    if not (output / ".complete").exists():
        _compile({**standard, **{root.name: root for root in roots}}, output, only=names)
    return output


def _local_roots(unimoc: Path) -> dict[str, Path]:
    public = unimoc.parent / "modules" / "lib" / "public_regulated_data_types"
    roots = {
        "uavcan": public / "uavcan",
        "reg": public / "reg",
        "unimoc": unimoc / "dsdl" / "unimoc",
        "cymon": unimoc / "lib" / "cymon" / "dsdl" / "cymon",
    }
    missing = [str(path) for path in roots.values() if not path.is_dir()]
    if missing:
        raise DsdlError(
            "not a UNIMOC checkout in a west workspace (run west update and init lib/cymon); missing: "
            + ", ".join(missing)
        )
    return roots


def _compile(roots: dict[str, Path], output: Path, only: Sequence[str] = NAMESPACES) -> None:
    """Compiles the namespaces ``only`` of ``roots`` (all of them the lookup) into ``output``.

    Each process compiles into a directory of its own and moves it into place
    at the end, so two cypilot processes that start at once do not delete each
    other's files.
    """
    import pycyphal.dsdl  # slow, only when compiling

    work = output.with_name(f"{output.name}.tmp-{os.getpid()}")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    lookup: list[str | Path] = list(roots.values())
    try:
        for name in only:
            pycyphal.dsdl.compile(roots[name], lookup, work)
    except Exception as error:  # pydsdl and nunavut raise many types
        shutil.rmtree(work, ignore_errors=True)
        raise DsdlError(f"DSDL compilation failed: {error}") from error
    (work / ".complete").touch()
    if output.exists():
        shutil.rmtree(output, ignore_errors=True)
    try:
        work.rename(output)
    except OSError:  # another process moved its result into place first
        shutil.rmtree(work, ignore_errors=True)


# --- Download ------------------------------------------------------------------


def _download(unimoc_ref: str, target: Path) -> dict[str, str]:
    if not _SAFE_REF.match(unimoc_ref):
        raise DsdlError(f"invalid UNIMOC ref: {unimoc_ref!r}")
    target.mkdir(parents=True)

    unimoc = _fetch(f"https://codeload.github.com/{_UNIMOC_REPO}/tar.gz/{unimoc_ref}")
    west = _extract(unimoc, {"dsdl/unimoc": target / "unimoc"}, extra_file="west.yml")
    public_rev = _public_dsdl_revision(west)
    cymon_rev = _submodule_commit(unimoc_ref, "lib/cymon")

    public = _fetch(f"https://codeload.github.com/{_PUBLIC_DSDL_REPO}/tar.gz/{public_rev}")
    _extract(public, {"uavcan": target / "uavcan", "reg": target / "reg"})
    cymon = _fetch(f"https://codeload.github.com/{_CYMON_LIB_REPO}/tar.gz/{cymon_rev}")
    _extract(cymon, {"dsdl/cymon": target / "cymon"})

    return {"unimoc": unimoc_ref, "public_regulated_data_types": public_rev, "cymon-lib": cymon_rev}


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers=_github_headers())
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data: bytes = response.read()
            return data
    except OSError as error:
        raise DsdlError(f"download failed: {url}: {error}") from error


def _github_headers() -> dict[str, str]:
    headers = {"User-Agent": "cypilot"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _extract(archive: bytes, directories: dict[str, Path], extra_file: str | None = None) -> str | None:
    """Writes the files below the given archive directories; returns the text of ``extra_file``."""
    extra_text = None
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = PurePosixPath(member.name).parts[1:]  # drop the <repo>-<ref>/ prefix
            if not member.isfile() or ".." in parts:
                continue
            relative = "/".join(parts)
            if extra_file is not None and relative == extra_file:
                extracted = tar.extractfile(member)
                assert extracted is not None
                extra_text = extracted.read().decode()
                continue
            for prefix, destination in directories.items():
                if relative.startswith(prefix + "/") and relative.endswith(".dsdl"):
                    path = destination / relative[len(prefix) + 1 :]
                    path.parent.mkdir(parents=True, exist_ok=True)
                    extracted = tar.extractfile(member)
                    assert extracted is not None
                    path.write_bytes(extracted.read())
    for destination in directories.values():
        if not destination.is_dir():
            raise DsdlError(f"the archive holds no {destination.name} namespace")
    return extra_text


def _public_dsdl_revision(west_yml: str | None) -> str:
    """The revision of public_regulated_data_types in the UNIMOC west manifest."""
    if west_yml is None:
        raise DsdlError("the UNIMOC archive has no west.yml")
    match = re.search(
        r"-\s*name:\s*public_regulated_data_types\s*\n(?:\s+\w+:.*\n)*?\s+revision:\s*(\S+)", west_yml
    )
    if not match or not _SAFE_REF.match(match.group(1)):
        raise DsdlError("no public_regulated_data_types revision in the UNIMOC west.yml")
    return match.group(1)


def _submodule_commit(unimoc_ref: str, path: str) -> str:
    url = f"https://api.github.com/repos/{_UNIMOC_REPO}/contents/{path}?ref={unimoc_ref}"
    try:
        content = json.loads(_fetch(url))
    except json.JSONDecodeError as error:
        raise DsdlError(f"unexpected answer from {url}") from error
    if not isinstance(content, dict):
        raise DsdlError(f"unexpected answer from {url}")
    sha = content.get("sha")
    if content.get("type") != "submodule" or not isinstance(sha, str) or not _SAFE_REF.match(sha):
        raise DsdlError(f"{path} is no submodule at {unimoc_ref}")
    return sha
