# cypilot Agent Notes

cypilot is the operator GUI (`cypilot`) and CLI (`cypilot-ctl`) for
[UNIMOC](https://github.com/Tecnologic/unimoc) drives and other Cyphal nodes,
in Python on pycyphal. The plan, the decisions and the phases are in
[docs/plan.md](docs/plan.md); read it before larger changes.

## Layout

- `src/cypilot/core/`: the bus side, shared by the CLI and the GUI: transport
  from interface text (`transport.py`), the session with discovery
  (`session.py`), registers (`registers.py`), the heartbeat status
  (`status.py`), capabilities and ports from discovery (`capabilities.py`),
  settings profiles (`profiles.py`). **No Qt import in `core/`.**
- `src/cypilot/dsdl.py`: downloads and compiles the DSDL of the pinned UNIMOC
  release (`DEFAULT_UNIMOC_REF`) or a local checkout (`CYPILOT_UNIMOC`).
- `src/cypilot/simulation.py`: fetches and runs the UNIMOC `native_sim` drive.
- `src/cypilot/cli.py`: `cypilot-ctl`.
- `src/cypilot/ui/`: the Qt GUI `cypilot`. `bus.py` runs the core on its own
  asyncio thread and hands results to the UI through Qt signals (no `await`
  on the UI thread); `models.py`, `panels.py`, `main_window.py`,
  `connect_dialog.py`, `app.py`. GUI tests use the `qapp` fixture
  (offscreen) and `tests/qt_support.py`.
- `tests/unit/`: no bus. `tests/sim/`: against the released simulation (Linux).

## Rules

- **No fixed register list.** Registers are scanned from the node; only the
  standard `uavcan.*` names and the few the control functions need are named
  in code. Node functions come from discovery (ports, `.type` registers,
  register prefixes), not from a node type.
- **No DSDL copy in the repository.** The UNIMOC DSDL is GPL-3.0-or-later; it
  is downloaded at run time (`cypilot.dsdl`). Do not commit generated code.
- **Safety:** cypilot is no safety device. It never engages a drive by itself,
  stops publishing readiness when its UI stalls, and keeps a "stop all" always
  reachable (`docs/plan.md`, section 7).
- **Platforms:** Linux and Windows. The simulation runs on Linux only; on
  Windows the simulation and cypilot run together in WSL2. Do not add a bridge
  from Windows to a simulation.
- **Threads:** all Cyphal I/O on one asyncio loop (the bus thread in the GUI);
  the UI only sends commands and receives snapshots.
- Changes to the bus contract (vendor status bits, readiness subjects,
  register description file) belong to UNIMOC first; cypilot follows a UNIMOC
  release. Bump `DEFAULT_UNIMOC_REF` with the matching tests.
- Python 3.11 baseline. Every source file starts with
  `# SPDX-License-Identifier: Apache-2.0`.

## Check before a commit

```sh
pip install -e '.[dev]'
ruff check . && ruff format --check . && mypy
pytest                       # unit tests, and the simulation tests on Linux
```

All three must pass before a PR. Without network access to GitHub, point
`CYPILOT_UNIMOC` at a local UNIMOC west workspace and `CYPILOT_SIM_EXE` at a
`native_sim` binary.

## Git

- Work on an own branch; never commit to or push `main` or another person's
  branch. Never force-push, amend or rebase pushed commits unless asked.
- Commit each finished step with a clear message.
