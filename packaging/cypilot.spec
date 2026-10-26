# SPDX-License-Identifier: Apache-2.0
# PyInstaller spec of the one-file executable: pyinstaller packaging/cypilot.spec
#
# The executable carries cypilot and the web UI, no Qt and no DSDL: the UNIMOC
# DSDL is GPL-3.0-or-later and is downloaded and compiled on the first start
# (into the user cache), as with the Python package. So nunavut, pydsdl and
# pycyphal go in whole, with their templates, for the compiled DSDL to import.
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

repo = Path(SPECPATH).parent

datas = [(str(repo / "src/cypilot/web/static"), "cypilot/web/static")]
binaries = []
hiddenimports = (
    collect_submodules("cypilot") + collect_submodules("uvicorn") + collect_submodules("can")
)
for package in ("nunavut", "pydsdl", "pycyphal", "numpy"):
    d, b, h = collect_all(package)
    datas += d
    binaries += b
    hiddenimports += h
# python-can finds the "canable" interface through the entry points of the cypilot distribution.
datas += copy_metadata("cypilot") + copy_metadata("python-can") + copy_metadata("pycyphal")

a = Analysis(
    [str(repo / "src/cypilot/__main__.py")],
    pathex=[str(repo / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "playwright", "PySide6"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="cypilot",
    console=True,  # the log and Ctrl+C to quit
    upx=False,
)
