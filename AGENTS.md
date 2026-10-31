# cypilot Agent Notes

cypilot is the operator web app (`cypilot`, `cypilot-ctl serve`) and CLI
(`cypilot-ctl`) for [UNIMOC](https://github.com/Tecnologic/unimoc) drives and
other Cyphal nodes, in Python on pycyphal. The plan, the decisions and the
phases are in [docs/plan.md](docs/plan.md) and, for the web app that replaced
its Qt GUI, [docs/web_plan.md](docs/web_plan.md); read them before larger
changes.

## Layout

- `src/cypilot/core/`: the bus side, shared by the CLI and the web app: transport
  from interface text (`transport.py`), the session with discovery
  (`session.py`), registers (`registers.py`), the heartbeat status
  (`status.py`), capabilities and ports from discovery (`capabilities.py`),
  settings profiles (`profiles.py`), enabling, setpoints, stop all, the
  watchdog and recovery (`control.py`), the simulation controls
  (`sim_control.py`), UNIMOC's hardware startup aid (`startup.py`) and homing (`homing.py`), subjects as channels with the recorder, export and
  replay (`signals.py`), and from the cymon tool: the CANable FD bus
  (`canable.py`), the event hub (`events.py`), monitoring any subject and
  publications with a lease (`subjects.py`), cymon captures (`scope.py`),
  the bus hub of the web server with the control lease (`hub.py`), the
  UNIMOC register description file (`descriptions.py`), runs on disk
  (`runs.py`) and their recording from the bus (`recording.py`). A run starts
  and stops only on a request of the user.
  **No web or UI import in `core/`.**
- `src/cypilot/demo.py`: simulated device (registers, telemetry, readiness,
  cymon services) for tests on a python-can virtual bus without the UNIMOC
  simulation (`tests/unit/demo_bus.py`).
- `src/cypilot/dsdl.py`: downloads and compiles the DSDL of the pinned UNIMOC
  release (`DEFAULT_UNIMOC_REF`) or a local checkout (`CYPILOT_UNIMOC`).
- `src/cypilot/simulation.py`: fetches and runs the UNIMOC `native_sim` drive.
- `src/cypilot/cli.py`: `cypilot-ctl`; `cypilot-ctl serve` runs the web server.
- `src/cypilot/wifi.py`: the Wi-Fi of the Pi over `nmcli` for the web app
  (`serve --wifi`); tests with `tests/fake_nmcli.py` (`fake_nmcli` fixture).
- `src/cypilot/web/`: the FastAPI app (`app.py`: REST, WebSocket, PIN login)
  on the hub, and the static UI in `static/` (`index.html`, `app.js`,
  `app.css`; Alpine.js and uPlot vendored in `static/vendor/`, no build step).
  Tests: `tests/unit/test_web.py` (API), `tests/browser/` (Playwright with the
  `browser` fixture and `tests/web_support.py`).
- `scripts/pi-setup.sh`: the Raspberry Pi on the cart ([docs/pi.md](docs/pi.md));
  `tests/unit/test_pi_setup.py` runs its dry run. `scripts/load_test.py`: the
  load test of the web app.
- `packaging/`: the PyInstaller spec of the executable (no Qt, no DSDL; it
  downloads the DSDL on the first start) and the smoke test of the
  executable and the wheel; `.github/workflows/build.yml` builds both
  (called by CI and by `release.yml` on a tag `vX.Y.Z`).
- `src/cypilot/__main__.py`: `python -m cypilot` and the executable (the web
  app without arguments, else `cypilot-ctl`).
- `tests/unit/`: no bus. `tests/browser/`: the web UI against demo devices.
  `tests/sim/`: against the released simulation (Linux).

## Rules

- **No fixed register list.** Registers are scanned from the node; only the
  standard `uavcan.*` names and the few the control functions need are named
  in code. Node functions come from discovery (ports, `.type` registers,
  register prefixes), not from a node type.
- **No DSDL copy in the repository.** The UNIMOC DSDL is GPL-3.0-or-later; it
  is downloaded at run time (`cypilot.dsdl`). Do not commit generated code.
- **Safety:** cypilot is no safety device. It never engages a drive by itself,
  puts the drives to sleep when the operator's control lease lapses, and keeps
  "stop all" and the E-stop open to every client (`docs/web_plan.md`,
  section 5).
- **Platforms:** Linux and Windows. The simulation runs on Linux only; on
  Windows the simulation and cypilot run together in WSL2. Do not add a bridge
  from Windows to a simulation.
- **Threads:** all Cyphal I/O on one asyncio loop (uvicorn's in the web app);
  the browser only uses the API and receives events.
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
