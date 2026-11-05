# Flow Control System Server — Implementation Roadmap

Currently, [`contents/code/main.py`](../contents/code/main.py) is an 8-line stub (`print("hello world")`). This roadmap lays out a phased engineering plan to build the production Valve Cart server by porting, fixing, and unifying the reference implementations from [`PDP-Monitoring-System`](../../PDP-Monitoring-System/) and [`PDP-Ground-Support-Rewrite`](../../PDP-Ground-Support-Rewrite/).

A core architectural principle of this roadmap is **consolidating the server entirely in modern asynchronous Python (`asyncio`)**, deliberately avoiding any C++/Python boundary, compiled C++ daemons, or mixed IPC on the Valve Cart host. The only C++ in the system is isolated to the embedded Arduino firmware on the microcontroller (`Controls.ino`).

---

## Target Directory Structure (`contents/`)

```text
Flow-Control-System-Server/
├── Docs/
│   ├── README.md
│   ├── protocol-and-architecture.md
│   └── roadmap.md
├── contents/
│   ├── code/
│   │   ├── main.py                  # Asyncio entrypoint, CLI flags (--mock, --host, --port)
│   │   ├── ws_server.py             # Unified WebSocket server (multi-client broadcast & RPC)
│   │   ├── serial_controller.py     # Controls Arduino USB serial manager (FIFO + priority abort)
│   │   ├── instrumentation.py       # LabJack U6 stream reader + thermocouple CJC conversion
│   │   ├── thermocouple.py          # NIST ITS-90 Type-K polynomial functions (from PDP-Monitoring-System)
│   │   ├── data_recorder.py         # Two-row CSV / JSONL session recorder & remote file manager
│   │   └── mocks.py                 # Realistic hardware simulator for desktop development
│   └── config/
│       ├── flow-control-server.service
│       └── server_config.json       # Serial port, baudrate, LabJack gains, broadcast rate
└── tools/
    ├── push-code.sh
    └── services.sh
```

---

## Phase 1: Unified Asyncio WebSocket Server & Mock Mode (`ws_server.py`, `mocks.py`)

**Goal:** Replace the `print("hello world")` stub in `main.py` with a working `asyncio` + `websockets` server listening on `0.0.0.0:8080` (`/ws`) that supports `--mock` mode out-of-the-box so the client can connect, view live telemetry, and test valve actuation without needing physical LabJack or Arduino hardware attached.

### Tasks:
1. **Multi-Client Connection Registry (`ws_server.py`):**
   - Maintain `self.clients: set[WebSocketServerProtocol] = set()`.
   - On client connect:
     - Register client in `self.clients`.
     - Immediately send `{"type": "init", "system": "Flow-Control-System-Server", "valves": self.valve_states, "recording": self.recorder.is_recording}`.
   - Implement `broadcast(payload: dict)` using `websockets.broadcast(self.clients, json.dumps(payload))` so slow/disconnected clients never block the event loop.
2. **Inbound Message Router (`ws_server.py`):**
   - Parse incoming JSON frames from clients and dispatch by `msg.get("type")` (or legacy `msg.get("identifier")` / `msg.get("command")`):
     - `"command"` / `"CTRL"` $\rightarrow$ `serial_controller.send_valve_command(valve, action)`
     - `"abort"` / `"ABORT"` $\rightarrow$ `serial_controller.send_abort()`
     - `"unabort"` / `"UNABORT"` $\rightarrow$ `serial_controller.send_unabort()`
     - `"summary"` / `"SUMMARY"` $\rightarrow$ `serial_controller.request_summary()`
     - `"record"` $\rightarrow$ `data_recorder.set_recording(active, session_name)`
     - `"remote_data_req"` $\rightarrow$ `data_recorder.handle_remote_request(websocket, msg)`
3. **Hardware Simulation / Mock Mode (`mocks.py`):**
   - Combine the physics-inspired envelope generator from [`Flow-Control-System-Client/contents/code/telemetry_files.py:generate_demo_csv`](../../Flow-Control-System-Client/contents/code/telemetry_files.py) with the transit delay simulator from [`PDP-Monitoring-System/src/server/wss.py:__test_serial_handler`](../../PDP-Monitoring-System/src/server/wss.py):
     - Emit `{"type": "telemetry", "data": {...}}` at 20 Hz with realistic pressure/temperature/load waveforms that respond dynamically to virtual valve states (e.g., opening `N2OF` ramps `P_RUN_TANK`; opening `MEV` ramps `P_INJECTOR`, `P_COMB_CHMBR`, and `L_THRUST`).
     - When a valve command arrives in mock mode, immediately broadcast `{"type": "valve_state", "valve": valve, "state": "TRANSIT"}`, wait `1.5s` asynchronously (`await asyncio.sleep(1.5)` — **not** blocking `time.sleep(3)` as in legacy `wss.py:139`!), and then broadcast the target state (`"OPEN"` or `"CLOSED"`).
   - Automatically fall back to mock mode if `--mock` is passed or if `/dev/ttyACM0` / LabJack U6 hardware is not detected (emitting a `"WARN"` alert toast to connected clients so operators clearly know whether they are on live hardware or simulation).

---

## Phase 2: Controls Arduino Serial Interface (`serial_controller.py`)

**Goal:** Port and harden [`PDP-Monitoring-System/src/serialInterface/serialInterface.py`](../../PDP-Monitoring-System/src/serialInterface/serialInterface.py) to communicate reliably with [`Controls.ino`](../../PDP-Monitoring-System/src/controlsFirmaware/Controls.ino) over `/dev/ttyACM0` at `115200` baud.

### Tasks:
1. **FIFO Command Queue + Priority Abort Bypass:**
   - Replace `asyncio.LifoQueue` with `asyncio.Queue` (FIFO).
   - Provide an immediate `send_abort()` method that clears pending normal commands from the queue and writes `MCC,ABORT\n` directly to the serial stream.
2. **Fix Full `VC,SUMMARY` Parsing:**
   - When `VC,SUMMARY,N2OF,<s0>,N2OV,<s1>,N2F,<s2>,RTV,<s3>,ERV,<s4>,MEV,<s5>,NCV,<s6>` is received:
     - Iterate over **all** key-value pairs (fixing the legacy bug in `serialInterface.py:282` where `return feedback` exited after the first changed valve).
     - Normalize `"CLOSE"` to `"CLOSED"` for UI consistency (while preserving `"OPEN"`, `"TRANSIT"`, `"FILLING"`, `"VENTING"`).
     - Broadcast all changed valve states to connected WebSocket clients.
3. **Status & Fault Alert Forwarding:**
   - Parse `VC,STATUS,ABORTED` $\rightarrow$ broadcast `{"type": "alert", "level": "ERR", "message": "Hardware Emergency Abort triggered on Valve Cart!"}`.
   - Parse `VC,STATUS,CANCELLED ABORT` $\rightarrow$ broadcast `{"type": "alert", "level": "INFO", "message": "Valve Cart abort state cleared."}`.
   - Parse `VC,ERROR,...` $\rightarrow$ broadcast `{"type": "alert", "level": "WARN", "message": f"Arduino Serial Error: {raw_line}"}`.
4. **USB Hotplug & Auto-Reconnect Resilience:**
   - If `/dev/ttyACM0` disconnects due to EMI or vibration during testing, catch `serial.SerialException`, broadcast a `"WARN"` alert toast to Mission Control, and automatically retry opening the serial port every `2.0s` without crashing `flow-control-server.service`.
   - On reconnect, immediately send `MCC,CONNECT\n` to resynchronize valve limit-switch states.

---

## Phase 3: LabJack U6 Telemetry Acquisition (`instrumentation.py`, `thermocouple.py`)

**Goal:** Port [`PDP-Monitoring-System/src/instrumentation/read_labjack.py`](../../PDP-Monitoring-System/src/instrumentation/read_labjack.py) and [`thermocouple.py`](../../PDP-Monitoring-System/src/instrumentation/thermocouple.py) into a clean, thread-safe producer that feeds both the disk logger and the WebSocket broadcaster without `tmp.txt` file polling.

### Tasks:
1. **Port `thermocouple.py`:**
   - Copy `get_ref_voltage(T_cold_junction_K)` and `V_to_K(tc_voltage, ref_voltage)` from `PDP-Monitoring-System/src/instrumentation/thermocouple.py`.
   - Add `V_to_C(tc_voltage, ref_voltage)` returning `V_to_K(tc_voltage, ref_voltage) - 273.15` so temperatures are reported in Celsius (`°C`).
2. **Dedicated LabJack Reader Thread (`instrumentation.py`):**
   - Run `d.streamData(convert=False)` in a dedicated daemon `threading.Thread` (since `LabJackPython`'s USB bulk read generator is synchronous).
   - Convert voltages to the client's expected engineering units:
     - Pressures (`P_RUN_TANK`, `P_INJECTOR`, `P_N2O_FLOW`, `P_N2_FLOW`, `P_COMB_CHMBR`) in **`PSI`** (omitting the `6895` $\text{PSI}\rightarrow\text{Pa}$ factor from `read_labjack.py`).
     - Temperatures (`T_RUN_TANK`, `T_INJECTOR`, `T_COMB_CHMBR`, `T_POST_COMB`) in **`°C`**.
     - Loads (`L_RUN_TANK`, `L_THRUST`) in **`lbf`** (or `kg` / `N` matching `graphs.json`).
3. **Decoupled Full-Rate Disk Logging vs. Decimated WebSocket Broadcast:**
   - **Why decouple disk rate from WebSocket rate?** The LabJack scans at `1000 Hz` ($\approx 83$ packets/sec at 12 samples/packet). Writing every packet to local disk on the Mini PC preserves full-resolution data for post-test analysis (`Propulsion-Test-Data`), while broadcasting over the TP-Link Wi-Fi bridge at a steady `20–30 Hz` prevents Wi-Fi packet congestion and keeps client rendering smooth.

---

## Phase 4: On-Cart Data Recording & Remote File Management (`data_recorder.py`)

**Goal:** Support the Client's **Remote data** browser tab (`Dashboard.qml` / `DashboardFunctions.qml`) and topbar **Record data** button (`main.qml`).

### Tasks:
1. **Two-Row Header CSV & JSONL Logger:**
   - Store recorded telemetry files in `/home/uvr/Flow-Control-System-Server/data/`.
   - Write CSV files using the exact two-row header format expected by [`Flow-Control-System-Client/contents/code/telemetry_files.py`](../../Flow-Control-System-Client/contents/code/telemetry_files.py) and [`Propulsion-Test-Data/scripts/plot_instrumentation.py`](../../Propulsion-Test-Data/scripts/plot_instrumentation.py):
     - Row 0 (Units): `psi,psi,psi,psi,psi,kg,N,C,C,C,C,s`
     - Row 1 (Channels): `P_INJECTOR,P_COMB_CHMBR,P_N2O_FLOW,P_N2_FLOW,P_RUN_TANK,L_RUN_TANK,L_THRUST,T_RUN_TANK,T_INJECTOR,T_COMB_CHMBR,T_POST_COMB,seconds`
   - Flush periodically (every `2.5s`) and cleanly close on `SIGTERM` / `SIGINT` (matching `PDP-Ground-Support-Rewrite/contents/code/backend.py`).
2. **Remote Data WebSocket RPC Handlers:**
   - `operation == "list"`: Scan `data/` directory and reply with `{"type": "remote_data_list", "files": [...]}`.
   - `operation == "get"`: Read requested file from `data/` (validating path traversal stays inside `data/`) and send `{"type": "remote_data_content", "filename": ..., "mode": ..., "content": ...}`.
   - `operation == "rename"`: Rename file inside `data/` and broadcast updated `remote_data_list`.
   - `operation == "delete"`: Delete file inside `data/` (committed when client disconnects or closes) and broadcast updated `remote_data_list`.

---

## Phase 5: Deployment, Systemd Integration & Verification

**Goal:** Deploy to the Valve Cart Mini PC (`192.168.0.1`) and verify end-to-end operation with `Flow-Control-System-Client`.

### Tasks:
1. **Dependencies & Environment Setup on Mini PC:**
   - Add `requirements.txt` to `Flow-Control-System-Server` (`websockets>=12.0`, `pyserial>=3.5`, `LabJackPython>=2.1.0`).
   - Ensure user `uvr` on `192.168.0.1` is in the `dialout` group (`sudo usermod -aG dialout uvr`) for `/dev/ttyACM0` access without root, and that the LabJack `exodriver` udev rules are installed.
2. **Deploy via `tools/push-code.sh` & Monitor via `tools/services.sh`:**
   - Run `./tools/push-code.sh` to sync `Flow-Control-System-Server` and restart `flow-control-server.service`.
   - Verify service health and startup logs with `./tools/services.sh status` and `./tools/services.sh logs -f`.
3. **End-to-End Integration Checklist:**
   - [ ] Client connects to `ws://192.168.0.1:8080/ws` and receives `init` valve states.
   - [ ] Live telemetry graphs update smoothly at 20–30 Hz with accurate `PSI`, `°C`, and `lbf` units.
   - [ ] Disarmed valve actuation attempts are blocked with a warning toast.
   - [ ] Armed valve actuation transitions card from `CLOSED` $\rightarrow$ `TRANSIT` $\rightarrow$ `OPEN` as physical limit switches engage.
   - [ ] Pressing `[Space]` or clicking Safety Override triggers `MCC,ABORT\n` immediately regardless of arm state.
   - [ ] Remote data tab lists, loads, downloads, renames, and deletes recorded `.csv`/`.jsonl` logs on the Valve Cart.
