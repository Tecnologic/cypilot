# cypilot

Operator GUI and CLI for [UNIMOC](https://github.com/Tecnologic/unimoc) drives
and other Cyphal nodes. Where [cymon](https://github.com/Tecnologic/cymon) is
the debug tool for any register, subject and service by hand, cypilot does the
everyday jobs on one to many nodes: find the nodes and show their state, scan,
edit, compare and store their settings, enable and stop drives, recover from an
emergency stop or a trip, plot across nodes and take triggered scope shots.

> **Status: pre-alpha.** The core and the CLI `cypilot-ctl` (nodes, registers,
> commands, capabilities, settings profiles) work against the UNIMOC
> simulation. The GUI `cypilot` shows the nodes with their state, edits their
> registers, enables and stops drives, recovers them, plots the subjects of
> several nodes on one time axis, records and replays them, and shows their
> log; the scope follows.
> The plan is [docs/plan.md](docs/plan.md).

## Install

Python 3.11 or newer, Linux or Windows.

```sh
pip install -e .            # from a clone; adds cypilot-ctl to the path
pip install -e '.[gui]'     # plus the GUI cypilot (PySide6, pyqtgraph)
pip install -e '.[dev]'     # plus the GUI, pytest, ruff and mypy
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
cypilot-ctl caps 10                               # what the node supports, its ports and their types
cypilot-ctl run 10 --current 0,2 --for 3          # enable, hold i_q = 2 A for 3 s, ramp down, disable
cypilot-ctl stop                                  # SLEEP to every drive and zero setpoints, at once
cypilot-ctl recover 10                            # STANDBY clears a trip; --restart for a latched E-stop
cypilot-ctl sim 10 pause                          # simulated drive: status, pause, run, advance N, reset
cypilot-ctl sim 10 advance 16000                  # one second of virtual time, as fast as it runs
cypilot-ctl record 10 telemetry --for 5 --out rec # subjects of node 10 into rec/*.csv and recording.json
```

### Settings profiles

A profile is the set of mutable, persistent registers of a node in a JSON
file. Loading writes only the differences, repeats writes that the node
refuses until another setting is in place, reads everything back and stores
on request. It leaves the port-IDs (`uavcan.*.*.id`), `uavcan.node.id` and the
role label `uavcan.node.description` alone unless asked
(`--include-ports`, `--include-identity`), so one drive's settings can be
copied to the others:

```sh
cypilot-ctl profile save 10 fl_drive.json
cypilot-ctl profile diff 11 fl_drive.json         # exit code 1 when they differ
cypilot-ctl profile load 11 fl_drive.json --dry-run
cypilot-ctl profile load 11 fl_drive.json --store # write, verify, store in flash
```

The state column shows `disabled`, `enabled`, `engaged`, `blocked`
(external enable missing), `tripped`, `fault`, `E-stop latched`, `settings
pending` and `local control`, decoded from the heartbeat of UNIMOC nodes.

## GUI

```sh
cypilot                                           # the Connect dialog lists SocketCAN links and serial ports
cypilot -i slcan:/dev/pts/1                       # connect at once
```

The window lists the nodes of the bus with name, version, health, mode,
uptime and their state (the same state chips as `cypilot-ctl nodes`). Select a
node to scan its registers into a tree; filter by name, double-click a value
to edit it in the register's type. A value the node refuses turns red and
the node's own value stays; **Store** saves the settings in flash. The **Log**
tab collects the `uavcan.diagnostic.Record` messages of all nodes. All bus
work runs in its own thread, so the window stays responsive on a busy bus.

The **Control** tab enables, disables and recovers the selected drive and sends
its setpoints (the modes the drive subscribes: d/q current, torque, speed,
position) with a ramp. Enabling publishes `ENGAGED` on the drive's own
readiness subject (UNIMOC v0.2.0: 1024 + node-ID) 20 times a second; a drive is
never enabled without a click. Setpoint subjects are shared by default, so a
setpoint reaches every engaged drive on the same subject.

The **Simulation** tab, for a simulated UNIMOC drive, shows the virtual clock
and pauses, runs, advances (a number of control cycles of a paused
simulation) and resets it (plant at standstill, virtual time 0). Its plant
inputs (shaft mode, imposed speed, load torque, DC-link voltage, external
enable, ...) come from the drive's `unimoc.sim.*` registers. The drive's
readiness and setpoint timeouts run in real time, also while paused.

### Safety

cypilot is no safety device; the hard-wired E-stop loop and the brakes are.
Within that:

- **STOP ALL (Esc)** works anywhere in the window: `SLEEP` to every drive and
  zero setpoints at once, no ramp.
- **Emergency stop (Shift+Esc)**, after a confirmation, sends
  `EMERGENCY_STOP` to every UNIMOC node; it stays latched until a restart.
- **Watchdog:** the UI thread confirms every 100 ms that it is alive. After
  0.3 s without it the bus thread sends `SLEEP` to the engaged drives and stops
  refreshing their readiness. Closing the window, disconnecting or losing the
  adapter ends the refresh too; the drive disengages by its own timeout.
- Enabling is refused, with the reason, while an E-stop or trip is latched,
  the external enable is missing, the drive is under local control (e-bike), or
  another node (a VDC) publishes on the drive's readiness subject.
- **Recover** acknowledges a latched trip with `STANDBY`, or restarts a drive
  whose emergency stop is latched (after a confirmation).

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
