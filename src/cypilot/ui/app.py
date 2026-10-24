# SPDX-License-Identifier: Apache-2.0
"""``cypilot``: the GUI entry point."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from cypilot import __version__
from cypilot.core.session import DEFAULT_LOCAL_NODE_ID
from cypilot.core.transport import InterfaceError, parse_interface


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cypilot", description="Operator GUI for UNIMOC and other Cyphal nodes"
    )
    parser.add_argument("--version", action="version", version=f"cypilot {__version__}")
    parser.add_argument(
        "-i",
        "--interface",
        default=os.environ.get("CYPILOT_IFACE"),
        help="connect at once to this interface (else use the Connect dialog)",
    )
    parser.add_argument("--mtu", type=int)
    parser.add_argument("--local-node-id", type=int, default=DEFAULT_LOCAL_NODE_ID)
    args = parser.parse_args(argv)

    from PySide6.QtWidgets import QApplication

    from cypilot.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = MainWindow()
    window.show()
    if args.interface:
        try:
            window.connect_to(parse_interface(args.interface, mtu=args.mtu), args.local_node_id)
        except InterfaceError as error:
            print(f"cypilot: {error}", file=sys.stderr)
            return 2
    return int(app.exec())


if __name__ == "__main__":
    sys.exit(main())
