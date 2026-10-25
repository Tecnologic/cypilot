# One app: cypilot with the cymon features and a web front end

Status (2026-10-25): **decided; W1 and W2 done** (section 8). It changes decisions
of [plan.md](plan.md) (section 2: the Qt GUI, and cymon as a separate tool
the GUI depends on) and replaces its phases G5 to G7 by the phases W0 to W7
below. The firmware contract (plan sections 5.1, 6, 7.1, 9) stays as it is.

## 1. Why one app, and why a web front end

Today there are two host tools for the same bus:

| | cymon 0.1.0 ([Tecnologic/cymon](https://github.com/Tecnologic/cymon)) | cypilot (G0 to G4) |
|---|---|---|
| Purpose | Generic Cyphal admin and debug tool | Operator tool for UNIMOC nodes |
| Front end | **Web**: FastAPI, REST and one WebSocket, static Alpine.js and uPlot UI (vendored, no build step, works offline) | Qt (PySide6, pyqtgraph) and the CLI `cypilot-ctl` |
| Bus | Admin node, node table, port lists, PnP allocator, CANable FD plugin, adapter search, redundant interfaces, demo devices | Session, discovery, UNIMOC probe, capabilities, status chips from the vendor bits |
| Registers | Read, write, type and range check | Scan, edit, profiles (save, diff, load), the register description file (UNIMOC F2) |
| Subjects | Monitor any subject and type; publish any message with a lease | Live plots across nodes, recorder, CSV/JSON export, replay |
| Control | Publications with a lease, E-STOP | Readiness per node with keep-alive and watchdog, enable, disable, setpoints with ramps, stop all, recovery, simulation controls |
| Scope | cymon captures (trigger, pre-trigger, decimation, CSV) | planned (G5, on the cymon client) |
| Size | about 2400 lines of Python, 820 lines of UI | about 3000 lines of core and CLI, 1500 lines of Qt |
| License | AGPL-3.0-or-later | Apache-2.0 |

They overlap in the transport, the session, the node table, the registers and
the DSDL handling, and each lacks what the other has. Keeping both means two
answers to the same question for every bus feature.

A **web front end** fits the target better than Qt:

- **On the cart:** a Raspberry Pi with a CAN FD HAT runs the app headless;
  a phone, a tablet or a laptop opens it in the browser over Wi-Fi. Nothing
  is installed on the viewing device.
- **On the bench:** the same app runs on the laptop (Linux, Windows, WSL2
  with the simulation) and opens the browser on `localhost`.
- **Several viewers at once** (a phone on the cart, a laptop next to it), one
  of them in control (section 5).
- cymon already has this architecture and an API that scripts use; the Qt
  windows of G2 to G4 are the smaller part of cypilot. The core (no Qt in
  `core/`, by design) carries over unchanged.
- **Recording runs** (section 6) belongs on the box on the cart, not in a
  window on a laptop: the Pi records whether or not a browser is open.

What is lost: the Qt windows (G2 to G4 UI, about 1500 lines) and their
offscreen tests. Their logic (status model, control flows, recorder) is in
the core and stays; the views are rebuilt in the web UI.

## 2. Decisions to make (W0)

| # | Question | Proposal | Why |
|---|---|---|---|
| 1 | Which repository and name | **Decided 2026-10-25: cypilot** is the app (the tool does more than monitoring). The cymon features move in; `Tecnologic/cymon` gets a last release that points to cypilot, then is archived. The firmware library **cymon-lib** and its `cymon` DSDL keep their names (UNIMOC depends on them) | cypilot has the larger core, the UNIMOC contract, CI against the released simulation and Apache-2.0 |
| 2 | License of the merged code | **Decided 2026-10-25: Apache-2.0** for all of it; cymon's code is relicensed when it moves | The cymon code is the owner's (with AI-generated parts), so it can be relicensed. The AGPL would also apply to a Pi on the cart that serves the UI over the network: every user would have to be offered the source |
| 3 | Qt GUI | **Removed** once the web UI has the G2 to G4 functions (W3); until then it stays as it is, without new features | One UI to maintain; no PySide6 and Qt bundles on Windows and the Pi |
| 4 | Front-end technology | **cymon's**: static HTML, Alpine.js, uPlot, vendored, no Node.js build step | Works offline on the cart, small enough for a Pi, already tested; a build step can come later if the UI grows past what Alpine handles well |
| 5 | Access on the cart | The Pi opens its own Wi-Fi access point; the app binds to it, with a **PIN or token** (shown as a QR code on the bench UI); **viewer** and **operator** roles (section 5) | Without a login, anyone on the network could engage a drive |
| 6 | Run log format (recording only on the user's start, decided 2026-10-25) | **Two files per run:** the raw CAN frames (`candump` log, the truth, replayable into `vcan`) and the decoded channels (the existing cypilot export: CSV per source, `run.json` with nodes, firmware versions and register snapshots, markers) | Raw frames cost little at 1 Mbit/s and lose nothing; the decoded files open in any tool. MCAP (Foxglove) stays an option |
| 7 | Pi hardware | Raspberry Pi 4 or 5 (64-bit OS), a CAN FD HAT with the MCP2518FD (mainline `mcp251xfd` driver, SocketCAN), a high-endurance SD card or a USB SSD for the logs | SocketCAN is the best supported path; the MCP2518FD is the controller cymon's first hardware used too |

## 3. Architecture

```
 browser (phone, tablet, laptop)      cypilot-ctl (CLI)       scripts, pytest
        │ HTTP + one WebSocket                │                     │
        ▼                                     │                     │
 cypilot.web   FastAPI app, static UI, event hub, auth and roles, control lease
        │                                     │                     │
        └──────────────────────┬──────────────┴─────────────────────┘
 cypilot.core   (one asyncio loop; no web or UI imports)
   transport   interface text, CANable FD plugin, adapter search, redundant groups   ← cymon transport.py, canable.py
   session     node, discovery, UNIMOC probe, PnP allocator, demo devices            ← both
   registers   scan, read, write, description file, profiles                         ← both
   status      chips from heartbeat and vendor bits                                  ← cypilot
   control     readiness per node, keep-alive, enable, setpoints, stop all, recovery ← cypilot
   subjects    monitor any subject, publications with a lease                        ← cymon subjects.py
   signals     channels, recorder, export, replay                                    ← cypilot
   runlog      run sessions on disk: raw frames + decoded channels (section 6)       ← new
   scope       cymon captures, multi-node with the shared trigger                    ← cymon scope.py + G5
   sim_control simulation registers                                                  ← cypilot
```

- **One process, one asyncio loop:** the pycyphal node, the web server, the
  readiness keep-alive and the recorder. The Qt bus thread and its signals
  disappear; the web layer only calls the core and forwards its events.
- **The API is the UI's only access**, as in cymon (`/api/...`, `/ws`,
  OpenAPI at `/docs`). cymon's endpoints are kept where they fit (status,
  adapters, connect, nodes, registers, ports, command, estop, subjects,
  publishers, scope) and extended by cypilot's: `/api/control/...` (enable,
  disable, setpoint, stop all, recover), `/api/profiles`, `/api/sim/...`,
  `/api/plots`, `/api/runs`.
- **Events** over the WebSocket: node snapshots (4 Hz), diagnostics,
  channel batches (every 50 ms, only the channels a client shows), control
  state, capture progress, run log state. A slow client drops its own oldest
  events and never blocks the bus (cymon's event hub).
- **DSDL:** cypilot's download of the pinned UNIMOC release stays the default;
  cymon's extra roots (`--dsdl`, `/api/dsdl`) are added for other projects.
- **CLI:** `cypilot-ctl` keeps its commands and gains `serve` (the web app),
  `adapters` and `runs`. `cypilot` without arguments starts the web app and
  opens the browser, as `cymon` does.
- **Packaging:** the wheel (any OS, also the Pi); a PyInstaller executable for
  Windows and Linux x86_64 that serves the UI (cymon's spec); for the Pi a
  `pip install` plus a systemd unit (section 7). No Qt in any bundle.

## 4. The web UI

One page, tabs on a laptop, a bottom navigation on a phone (CSS only, no
second UI):

| View | Content | From |
|---|---|---|
| Bus | Interface, adapters, bit rates, DSDL, connect, demo | cymon |
| Nodes | Node cards with the status chips (engaged, tripped, E-stop latched, settings pending, local control, offline), health, mode, uptime; the cart view of four drives and the VDC as a layout of the same cards | cypilot + cymon roadmap |
| Registers | Tree by prefix, filter, units, limits and help from the description file, write with check, store, profiles | cypilot + F2 |
| Control | Enable, disable, setpoint with ramp, recover, per drive and for all; only for the operator | cypilot |
| Plots | Channels of any nodes on one time axis (uPlot), window, pause, markers, export; replay of a run | cypilot G4 + cymon monitor |
| Scope | cymon captures, several nodes on one trigger, overlay, CSV | cymon + G5 |
| Monitor, Publish | Any subject and type, generic publications with a lease (expert mode) | cymon |
| Runs | Start and stop a run (only by the user), its notes, the list of runs with size and duration, download (zip or single files), delete, free space | new |
| Simulation | Pause, advance, reset, plant inputs | cypilot |
| Log | Diagnostics of all nodes | both |

**STOP ALL** and **E-STOP** stay in the header of every view, also on the
phone, for every logged-in client (viewers too: stopping is never locked).
Esc and Shift+Esc work on keyboards.

## 5. Safety with several clients and Wi-Fi

The Qt UI had one window and a UI-thread watchdog. With browsers over Wi-Fi
the same rules hold, moved to the server:

- **One operator at a time.** A client takes the **control lease**
  (`/api/control/take`); the others are viewers. Enable, setpoints, register
  writes, store and the simulation controls need the lease; reading,
  plotting, recording and the stops do not. Taking the lease from another
  client asks that one first, or needs a confirmation after it has gone
  silent.
- **Readiness follows the lease.** The server publishes the readiness of the
  engaged drives (20 Hz, as today) only while the operator's WebSocket renews
  its lease. Renewal every 100 ms, lease 0.5 s by default (0.3 to 1 s
  configurable). A phone that loses Wi-Fi, sleeps, or closes the tab stops the
  drives within the lease time: the server publishes SLEEP, ramps the
  setpoints to zero and drops the lease. This replaces the Qt UI watchdog;
  `core.control`'s watchdog is fed by the lease instead of a Qt timer.
- **Wi-Fi jitter** can drop a lease that was fine. That is the safe side; the
  UI shows "lease lost" and needs a new Enable. The lease time is a trade-off
  the run log records.
- **Never engage by itself**, also not after a reconnect or a restart of the
  app; the cart's normal driver is the VDC, not this app.
- **Local control** (e-bike) and other masters on a readiness subject are
  shown and not overridden (plan section 7.1).
- cypilot stays **no safety device**: the hardware E-stop loop of the cart is
  the safety function. The UI says so where a drive is enabled.

## 6. Recording runs

A run is a time span with everything on the bus, stored on the box that runs
the app (the Pi on the cart):

- **Start and stop only by the user** (decided 2026-10-25): **Record** in
  the header of every view (also on the phone) starts a run with an optional
  name, **Stop** ends it. The app never starts a recording by itself, also
  not on engage, after a reconnect or a restart; a run that was going when
  the app or the Pi stopped is closed and kept, not continued. While a run
  records, the header shows it with its duration and size on every client.
  Recording needs no control lease: a viewer may record too.
- **Content:**
  - `frames.log`: every CAN frame with its time stamp, `candump -L` format
    (replay with `canplayer` into `vcan0`, and the app decodes it again);
  - `node<N>_<port>.csv`: the decoded subjects (the cypilot export format,
    written while the run goes, flushed every second);
  - `run.json`: start, end, the nodes with GetInfo (name, firmware version,
    VCS revision), a register snapshot of each node at the start, the
    interface and bit rates, the lease settings, and the markers (enable,
    disable, writes, trips, E-stops, the user's notes) with time stamps.
- **Which subjects:** all published ports with a `.type` register of all
  nodes, at their own rate (telemetry 20 Hz, drive status 50 Hz); a run can
  also raise `unimoc.telemetry.rate` for its duration and restores it.
- **Size:** the bus carries at most 1 Mbit/s; the raw log is under 100 KB/s
  at full load, a few MB per minute of driving. Closed runs are compressed.
  Nothing is deleted automatically: the Runs view shows the free space and
  warns below a limit; at a hard limit the running recording stops (with a
  marker and a message to every client) instead of filling the disk.
- **In the web app** (Runs view): the list of runs with name, start,
  duration, size and nodes; **download** a run as a zip (or a single file of
  it) to the phone, tablet or laptop; **delete** a run (with a confirmation,
  not while it records), or several at once; rename it and edit its notes.
  The API: `GET /api/runs`, `POST /api/runs/start`, `POST /api/runs/stop`,
  `GET /api/runs/{id}/download`, `DELETE /api/runs/{id}`.
- **Afterwards:** replay a run in the Plots view (the existing
  `Recorder.load`), or replay the raw frames into the simulation's bus for a
  test.
- **Captures** (scope) taken during a run are stored in the run as well.

The recorder runs in the server, so a run continues while no browser is
connected, and a browser that connects mid-run sees it.

## 7. Raspberry Pi on the cart

- Raspberry Pi OS 64-bit (Bookworm or later), Python 3.11.
- CAN FD HAT with the MCP2518FD: `dtoverlay=mcp251xfd,...` in
  `config.txt`; `ip link set can0 up type can bitrate 1000000 dbitrate
  ... fd on` by a systemd-networkd file (the UNIMOC BCC uses CAN FD at
  1 Mbit/s without bit rate switch, cymon's default).
- `pip install cypilot` in a venv; a systemd service starts
  `cypilot serve --interface socketcan:can0 --host 0.0.0.0` at boot and
  restarts it; the run log directory on the SSD or the SD card.
- Wi-Fi: the Pi as an access point (NetworkManager hotspot), the app on
  `http://cypilot.local` (mDNS) or the fixed address; optionally also a
  client on the workshop network.
- A script `scripts/pi-setup.sh` does all of the above; a Pi image is a later
  option.
- Time: the Pi has no real-time clock; runs are stamped from the boot clock
  and corrected when the time is known (NTP, or the browser's time on
  login).

## 8. Phases

| Phase | Content | Done when |
|---|---|---|
| W0 | The decisions of section 2; cymon's last release points to cypilot | Decided |
| W1 | **Done 2026-10-25 (the web parts move with W2):** **Core merge:** cymon's transport (CANable FD plugin, adapter search, redundant groups), PnP allocator, demo devices, subject monitor and publications with a lease, scope client, extra DSDL roots move into `cypilot.core`; duplicates (session, registers, DSDL, node table) are unified; cymon's tests come along | `cypilot-ctl` does everything both tools did on the command line; the core tests of both pass |
| W2 | **Done 2026-10-25 (the page is a placeholder until W3):** **Web server:** `cypilot.web` (FastAPI, event hub, static files), cymon's API plus control, profiles, simulation, plots; auth (PIN or token) and the control lease | API tests (httpx) against the simulation, lease loss stops a drive |
| W3 | **Web UI** with the G2 to G4 functions: bus, nodes with chips, registers with the description file, control, plots, simulation, log; responsive for the phone; then the **Qt GUI is removed** | Browser tests with Playwright (headless Chromium) against the simulation, also at phone size |
| W4 | **Run log:** runs started and stopped by the user, raw frames plus decoded channels plus `run.json`, list, download and delete in the web app, replay, free-space limit | A run of an engage, a setpoint step and a trip in the simulation, downloaded, replayed and deleted from the UI |
| W5 | **Scope** (old G5): cymon captures, several nodes on one shared trigger, overlay, stored in the run | Capture of two simulated drives on one trigger |
| W6 | **Raspberry Pi:** setup script, systemd service, access point, CAN FD HAT; load test (four drives, all subjects recorded, two browsers) | Runs a day on the cart bench with the drives |
| W7 | **Packaging and the end of cymon:** wheel, Windows and Linux executables without Qt, README workflows; `Tecnologic/cymon` archived | Install on a clean Windows machine and a fresh Pi |

Old phases: G5 becomes W5, G6 (cart, e-bike, VDC and BMS views) comes after
W3 as further views, G7 becomes W7.

## 9. Tests

- **Core:** the existing unit and simulation tests stay; cymon's tests
  (admin, scope, subjects, CANable, units) move with their code.
- **API:** FastAPI test client against the demo devices (no bus) and against
  the released `native_sim` (Linux), including lease loss, two clients, a
  viewer refused control, and E-stop from a viewer.
- **UI:** Playwright with headless Chromium (already in the CI images)
  against the simulation: connect, node chips, a register write, enable and
  stop, a plot, a run; once at laptop size and once at phone size.
- **Run log:** a recorded run of the simulation replayed through `vcan`
  gives the same decoded channels; no run starts without the user's start
  (engage, reconnect and restart start none); download and delete through the
  API and the UI.
- **Pi:** no CI; W6 is a manual check with a written checklist.

## 10. Risks and open points

1. **Lease over Wi-Fi:** 0.5 s may be too short on a noisy hall network and
   too long for comfort on a cart; to measure in W6.
2. **Browser load on a phone:** uPlot handles tens of thousands of points;
   the server sends only the shown channels, decimated to the plot width.
3. **SD card wear:** continuous logging wants a USB SSD or a high-endurance
   card; the recorder writes in blocks of about one second.
4. Decided 2026-10-25: the license is Apache-2.0 (decision 2).
5. **Scope of cymon's generic tools** (any subject, any publication): kept as
   an expert mode, or dropped where cypilot's typed views cover them.
6. **Firmware update over Cyphal** (cymon roadmap, UNIMOC F3) fits the same
   app later: the file server runs on the Pi.
