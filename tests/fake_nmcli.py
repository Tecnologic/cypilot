# SPDX-License-Identifier: Apache-2.0
"""A stand-in for ``nmcli`` with its state in the JSON file ``$FAKE_NMCLI_STATE`` (the Wi-Fi tests).

It answers the commands :mod:`cypilot.wifi` runs, in nmcli's terse format,
and logs every call in the state.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

AP = "cypilot-ap"


def terse(*fields: Any) -> str:
    return ":".join(str(f).replace("\\", "\\\\").replace(":", "\\:") for f in fields)


def main(args: list[str]) -> int:
    import fcntl

    path = Path(os.environ["FAKE_NMCLI_STATE"])
    # One call at a time, as NetworkManager does: the web app runs several nmcli calls at once,
    # and an unlocked read and write back would undo the change of another call.
    with path.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        state: dict[str, Any] = json.loads(path.read_text())
        state.setdefault("calls", []).append(args)
        try:
            return run(state, args)
        finally:
            temporary = path.with_suffix(".tmp")
            temporary.write_text(json.dumps(state))
            temporary.replace(path)  # readers (the tests) never see a half-written file


def run(state: dict[str, Any], args: list[str]) -> int:
    if args[:1] == ["--wait"]:
        args = args[2:]
    words, skip = [], False  # the command words, without the options and their values
    for arg in args:
        if skip:
            skip = False
        elif arg in ("-f", "-g", "--rescan"):
            skip = True
        elif not arg.startswith("-"):
            words.append(arg)
    if args[:4] == ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"]:
        print(terse("wlan0", "wifi", state["state"], state["connection"]))
        print(terse("eth0", "ethernet", "unavailable", ""))
    elif "IP4.ADDRESS" in args:
        print(terse("IP4.ADDRESS[1]", state["address"] + "/24"))
    elif args[:4] == ["-t", "-f", "NAME,TYPE,AUTOCONNECT", "connection"]:
        for name in state["known"]:
            print(terse(name, "802-11-wireless", "yes"))
        print(terse(AP, "802-11-wireless", "no"))
        print(terse("Wired connection 1", "802-3-ethernet", "yes"))
    elif "802-11-wireless.ssid" in args and "-g" in args:
        print(state["ap_ssid"])
    elif words[:3] == ["device", "wifi", "list"]:
        for ssid, signal, security in state["networks"]:
            print(terse("*" if ssid == state["connection"] else "", ssid, signal, security))
    elif words[:3] == ["device", "wifi", "connect"]:
        ssid = words[3]
        password = args[args.index("password") + 1] if "password" in args else None
        if ssid not in state["passwords"]:
            print(f"Error: No network with SSID '{ssid}' found.", file=sys.stderr)
            return 10
        if state["passwords"][ssid] != password:
            print(
                "Error: Connection activation failed: Secrets were required, but not provided.",
                file=sys.stderr,
            )
            state["connection"], state["state"] = "", "disconnected"  # nmcli dropped the access point
            return 4
        state["connection"], state["state"], state["address"] = ssid, "connected", "192.168.1.50"
        if ssid not in state["known"]:
            state["known"].append(ssid)
    elif words[:2] == ["connection", "up"] and words[2] == AP:
        state["connection"], state["state"], state["address"] = AP, "connected", "10.42.0.1"
    elif words[:2] == ["connection", "delete"]:
        state["known"].remove(words[2])
    elif words[:3] == ["connection", "modify", AP]:
        settings = dict(zip(args[3::2], args[4::2], strict=True))
        state["ap_ssid"] = settings.get("802-11-wireless.ssid", state["ap_ssid"])
        state["ap_psk"] = settings.get("wifi-sec.psk", state["ap_psk"])
    else:
        print(f"Error: fake nmcli does not know {args}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
