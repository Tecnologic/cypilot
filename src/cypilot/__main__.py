# SPDX-License-Identifier: Apache-2.0
"""``python -m cypilot`` and the executable: the web app without arguments, else ``cypilot-ctl``."""

from __future__ import annotations

import sys

from cypilot.cli import app_main, main


def run() -> int:
    if len(sys.argv) == 1:
        return app_main([])  # as ``cypilot``: the web app on this computer, in the browser
    return main(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(run())
