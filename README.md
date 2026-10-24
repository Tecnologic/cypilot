# cypilot

Operator GUI and CLI for [UNIMOC](https://github.com/Tecnologic/unimoc) drives
and other Cyphal nodes. Where [cymon](https://github.com/Tecnologic/cymon) is
the debug tool for any register, subject and service by hand, cypilot does the
everyday jobs on one to many nodes: find the nodes and show their state, scan,
edit, compare and store their settings, enable and stop drives, recover from an
emergency stop or a trip, plot across nodes and take triggered scope shots.

> **Status: pre-alpha.** The core and the CLI `cypilot-ctl` (nodes, registers,
> commands) work against the UNIMOC simulation. The GUI `cypilot` is not
> written yet. The plan is [docs/plan.md](docs/plan.md).

## Install

Python 3.11 or newer, Linux or Windows.

```sh
pip install -e .            # from a clone; adds cypilot-ctl to the path
pip install -e '.[dev]'     # plus pytest, ruff and mypy
```

The DSDL is not part of this repository. On the first use cypilot downloads
the namespaces of the UNIMOC release it is pinned to (`uavcan`, `reg`,
`unimoc`, `cymon`) into the user cache and compiles them (about a minute). To
use another UNIMOC version, set `CYPILOT_UNIMOC` to a git ref of the UNIMOC
repository, or to the path of a local `unimoc` checkout in its west workspace:

```sh
export CYPILOT_UNIMOC=v0.2.0                      # a UNIMOC release (default)
export CYPILOT_UNIMOC=~/unimoc-ws/unimoc          # a local west workspace
cypilot-ctl dsdl                                  # fetch and compile now
```

## Interfaces

The interface text follows yakut (`UAVCAN__CAN__IFACE`):

| Interface | Linux | Windows | Use |
|---|---|---|---|
| `socketcan:vcan0` | yes | — | Simulation cluster (CAN FD) |
| `socketcan:can0` | yes | — | CAN FD adapter with a kernel driver |
| `slcan:/dev/pts/N` | yes | — | One simulated drive over its pseudo terminal (classic CAN) |
| `slcan:/dev/ttyACM0`, `slcan:COM5` | yes | yes | CANable with slcan firmware (classic CAN) |
| `pcan:PCAN_USBBUS1`, `gs_usb:0`, ... | yes | yes | python-can adapters (CAN FD by default) |

`--mtu 8` forces classic CAN, `--bitrate 1000000/4000000` sets the
arbitration and data bit rate for adapters that need them. cypilot is node 127
on the bus unless `--local-node-id` says otherwise.

The simulation runs on Linux only. **On a Windows machine run the simulation
and cypilot together in WSL2**; Windows itself is for real adapters.

## CLI

```sh
export CYPILOT_IFACE=slcan:/dev/pts/1
cypilot-ctl nodes                                 # nodes with name, version, health, state
cypilot-ctl regs 10                               # all registers of node 10
cypilot-ctl regs 10 'unimoc.motor.*'              # filtered
cypilot-ctl get 10 unimoc.motor.limits.i_max
cypilot-ctl set 10 unimoc.motor.limits.i_max 12.5 # exit code 1 when the node rejects it
cypilot-ctl command 10 store                      # restart, store, factory-reset, emergency-stop
cypilot-ctl --json regs 10 > drive10.json
```

The state column shows `disabled`, `enabled`, `engaged`, `blocked`
(external enable missing), `tripped`, `fault`, `E-stop latched`, `settings
pending` and `local control`, decoded from the heartbeat of UNIMOC nodes.

## Against the simulation

```sh
python -c "from cypilot.simulation import fetch_release; print(fetch_release())"
<that path> -flash=/tmp/drive.bin                 # prints uart_1 connected to pseudotty: /dev/pts/N
cypilot-ctl -i slcan:/dev/pts/N nodes
```

## Tests

```sh
pytest                     # unit tests, and the simulation tests on Linux
pytest tests/unit          # no simulation
```

The simulation tests use `CYPILOT_SIM_EXE` or download the `native_sim`
binary of the pinned UNIMOC release (checked against its `SHA256SUMS`).

## License

Apache-2.0 (`LICENSE`). The UNIMOC firmware is GPL-3.0-or-later; cypilot
talks to it over the bus only and carries none of its code. The DSDL is
downloaded at run time from its own repositories under their own licenses.
