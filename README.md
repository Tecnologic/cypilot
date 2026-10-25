# cypilot

Operator web app and CLI for [UNIMOC](https://github.com/Tecnologic/unimoc) drives
and other Cyphal nodes. Where [cymon](https://github.com/Tecnologic/cymon) is
the debug tool for any register, subject and service by hand, cypilot does the
everyday jobs on one to many nodes: find the nodes and show their state, scan,
edit, compare and store their settings, enable and stop drives, recover from an
emergency stop or a trip, plot across nodes and take triggered scope shots.

> **Status: pre-alpha.** The core, the CLI `cypilot-ctl` and the web app
> `cypilot` work against the UNIMOC simulation: nodes with their state,
> registers with units and help, settings profiles (CLI), enabling, setpoints,
> stops and recovery, plots of several nodes on one time axis, cymon scope
> captures, the simulation controls, the log and recorded runs. The plan is
> [docs/plan.md](docs/plan.md); the web app replaced its Qt GUI.

## Install

Python 3.11 or newer, Linux or Windows.

```sh
pip install -e .            # from a clone; adds cypilot and cypilot-ctl to the path
pip install -e '.[dev]'     # plus pytest, Playwright, ruff and mypy
```

The DSDL is not part of this repository. On the first use cypilot downloads
the namespaces of the UNIMOC release it is pinned to (`uavcan`, `reg`,
`unimoc`, `cymon`) into the user cache and compiles them (about a minute). To
use another UNIMOC version, set `CYPILOT_UNIMOC` to a git ref of the UNIMOC
repository, or to the path of a local `unimoc` checkout in its west workspace:

```sh
export CYPILOT_UNIMOC=v0.3.0                      # a UNIMOC release (default)
export CYPILOT_DSDL=~/vdc/dsdl/vdc                # extra root namespaces of other projects (os.pathsep list)
export CYPILOT_UNIMOC=~/unimoc-ws/unimoc          # a local west workspace
cypilot-ctl dsdl                                  # fetch and compile now
```

## Interfaces

The interface text follows yakut (`UAVCAN__CAN__IFACE`):

| Interface | Linux | Windows | Use |
|---|---|---|---|
| `socketcan:vcan0` | yes | — | Simulation cluster (CAN FD) |
| `socketcan:can0` | yes | — | CAN FD adapter with a kernel driver (candleLight, a Raspberry Pi CAN FD HAT) |
| `canable:/dev/ttyACM0`, `canable:COM5` | yes | yes | CANable 2.0 (Pro) with the slcan firmware in **CAN FD** mode |
| `slcan:/dev/pts/N` | yes | — | One simulated drive over its pseudo terminal (classic CAN) |
| `slcan:/dev/ttyACM0`, `slcan:COM5` | yes | yes | slcan adapters in classic CAN |
| `pcan:PCAN_USBBUS1`, `gs_usb:0`, ... | yes | yes | python-can adapters (CAN FD by default) |
| `udp:127.0.0.1` | yes | yes | Cyphal/UDP on the local interface with this address |
| `virtual:demo` | yes | yes | python-can's in-process bus (demo devices, tests) |

Repeat `-i` (or separate interfaces by spaces, as in yakut's
`UAVCAN__CAN__IFACE`) for a **redundant bus**: every transfer goes out on all
of them, duplicates are dropped on reception. `cypilot-ctl adapters` lists the
CANable serial ports and the SocketCAN interfaces of this computer.
`--pnp cypilot-pnp.db` runs a plug-and-play node-ID allocator. The CANable's
CAN FD data rate is `--bitrate 1000000/2000000` (or `/5000000`); without a data
rate it sends FD frames without bit rate switch, as the UNIMOC BCC needs.

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
cypilot-ctl monitor 212 unimoc.drive.Telemetry    # any subject with any DSDL type, for --for seconds
cypilot-ctl scope 10                              # cymon trace variables of node 10
cypilot-ctl scope 10 0 1 --samples 500 --trigger rising --level 2 --pretrigger 100 --out shot.csv
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

## Web app

```sh
cypilot                                            # http://localhost:8765/ in the browser
cypilot --demo 2                                   # two demo devices, no hardware
cypilot-ctl -i socketcan:can0 serve                # the server alone; API docs at /docs
cypilot-ctl -i canable:/dev/ttyACM0 serve --host 0.0.0.0  # for phones and tablets; prints a PIN
```

One page for the laptop, the tablet and the phone (there the views move to a
bottom bar):

- **Bus:** interfaces (several form a redundant bus), adapter search, bit
  rates, MTU, plug-and-play allocator, demo devices.
- **Nodes:** a card per node with name, health, mode, uptime, version and the
  state chips (`disabled`, `enabled`, `engaged`, `tripped`, `E-stop latched`,
  `settings pending`, `local control`, ...).
- **Registers:** scanned from the node, grouped by prefix, with unit, limits,
  when a write takes effect and the help text from the UNIMOC register
  description file (`CYPILOT_REGISTERS`, else the file of the pinned release).
  Write in the register's type, read back, store, restart, export as JSON.
- **Control:** enable, disable and recover the drive, setpoints in the modes
  the drive subscribes (d/q current, torque, speed, position) with a ramp.
  Enabling publishes `ENGAGED` on the drive's readiness subject (UNIMOC
  v0.2.0 and later: 1024 + node-ID) 20 times a second; a drive is never
  enabled without a click.
- **Plots:** the published subjects of any nodes on one time axis, a window,
  pause, CSV export; commands, stops and register writes are markers.
- **Scope:** cymon captures with trigger and pre-trigger on one node or on
  several at once: one node triggers on its condition, and the others trigger
  on its `cymon.TriggerEvent` (about 1 to 2 ms later on the bus). For that the
  nodes need the shared trigger subject (`uavcan.pub.cymon_trigger.id` and
  `uavcan.sub.cymon_trigger.id`, disabled by default on UNIMOC); the view sets
  it (subject 220), stores it and restarts the nodes. All channels in one plot,
  each node aligned on its own trigger sample; CSV export; captures taken
  during a run are stored in the run.
- **Simulation:** the virtual clock of a simulated drive (pause, run, advance,
  reset) and its plant inputs (`unimoc.sim.*`).
- **Runs:** see below.
- **Monitor:** any subject with any type, and publications with a lease.
- **Log:** the `uavcan.diagnostic.Record` messages of all nodes.

The REST and WebSocket API (`/api/...`, `/ws`) serves the UI and scripts
alike.

### Runs

**● Record** in the header starts a run, **Stop** ends it; nothing else does.
cypilot never records by itself, also not on enable or after a restart. Any
client may record; the header of every client shows the run with its duration
and size. A run holds everything on the bus while it goes:

- `frames.log`: every CAN frame in the `candump -L` format (`canplayer`
  replays it into `vcan0`);
- `node<N>_<port>.csv`: the decoded subjects of every node (all published
  ports with a `.type` register), with `recording.json` for the replay;
- `capture<K>_node<N>.csv`: scope captures taken during the run;
- `run.json`: start, end, interface, nodes with their info and a register
  snapshot, and the markers (control, enable and disable, trips, E-stops,
  STOP ALL, register writes, commands, captures and **Note**s).

The **Runs** view lists the runs with name, start, duration, size and nodes;
download a run as a zip or one of its files, replay it in the plots, rename
it, edit its notes and delete it (with a confirmation, not while it records).
Runs are kept in `--runs DIR` (default `$CYPILOT_RUNS`, else
`~/.local/share/cypilot/runs` or `%LOCALAPPDATA%\cypilot\runs`); closed runs
are packed into `data.zip`. Nothing is deleted automatically: the view warns
below `--runs-warn` MB of free space, and a run stops below `--runs-stop` MB
instead of filling the disk. A run that was recording when cypilot stopped
is closed and kept at the next start.

### Safety

cypilot is no safety device; the hard-wired E-stop loop and the brakes are.
Within that:

- **One operator:** enabling, setpoints, register writes, commands and
  publications need the **control lease** (Take control). The operator's page
  renews it every 100 ms over its WebSocket. When the renewals stop for
  `--lease` seconds (0.5 s by default), or the page closes, cypilot puts the
  engaged drives to sleep. Disconnecting or losing the adapter ends the
  readiness too; the drive disengages by its own timeout.
- **STOP ALL (Esc)** works for every client, also without control: `SLEEP` to
  every drive, zero setpoints and the end of all publications, at once.
- **E-STOP (Shift+Esc)** sends `EMERGENCY_STOP` to every UNIMOC node; it stays
  latched until a restart.
- **Login:** beyond this computer (`--host 0.0.0.0`) the app needs a PIN.
- Enabling is refused, with the reason, while an E-stop or trip is latched,
  the external enable is missing, the drive is under local control (e-bike), or
  another node (a VDC) publishes on the drive's readiness subject.
- **Recover** acknowledges a latched trip with `STANDBY`, or restarts a drive
  whose emergency stop is latched (allow the restart first).

## Against the simulation

```sh
python -c "from cypilot.simulation import fetch_release; print(fetch_release())"
<that path> -flash=/tmp/drive.bin                 # prints uart_1 connected to pseudotty: /dev/pts/N
cypilot-ctl -i slcan:/dev/pts/N nodes
```

## Tests

```sh
pytest                     # unit and browser tests, and the simulation tests on Linux
pytest tests/unit          # no simulation, no browser
```

The simulation tests use `CYPILOT_SIM_EXE` or download the `native_sim`
binary of the pinned UNIMOC release (checked against its `SHA256SUMS`). The
browser tests need Chromium for Playwright (`python -m playwright install
chromium`, or `CYPILOT_CHROMIUM` pointing at a Chromium binary); without it they
skip.

## License

Apache-2.0 (`LICENSE`). The UNIMOC firmware is GPL-3.0-or-later; cypilot
talks to it over the bus only and carries none of its code. The DSDL is
downloaded at run time from its own repositories under their own licenses.
