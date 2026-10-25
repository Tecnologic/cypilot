# SPDX-License-Identifier: Apache-2.0
"""``python -m cypilot`` (and so the executable): arguments go to cypilot-ctl."""

from __future__ import annotations

import subprocess
import sys

from cypilot import __version__


def test_arguments_go_to_cypilot_ctl() -> None:
    result = subprocess.run([sys.executable, "-m", "cypilot", "--version"], capture_output=True, text=True)
    assert result.returncode == 0 and result.stdout.strip() == f"cypilot {__version__}"
    result = subprocess.run(
        [sys.executable, "-m", "cypilot", "serve", "--help"], capture_output=True, text=True
    )
    assert result.returncode == 0 and "--runs" in result.stdout and "--wifi" in result.stdout
