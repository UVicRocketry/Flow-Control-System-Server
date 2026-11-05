# Server Architecture & Communication Protocols

This document details the communication protocols between the **Flow Control System Client**, the **Flow Control System Server**, the **Controls Arduino**, and the **LabJack U6**, along with the engineering rationale for the server's design compared to the legacy [`PDP-Monitoring-System`](../../PDP-Monitoring-System/).

---

## 1. Architectural Reasoning: Lessons from `PDP-Monitoring-System`

A careful audit of [`PDP-Monitoring-System/src/`](../../PDP-Monitoring-System/src/) reveals several critical bottlenecks and bugs in the legacy backend that `Flow-Control-System-Server` is designed to solve:

### 1.1 Unifying WebSocket Ports (`8080` + `8888` $\rightarrow$ Single `/ws` Endpoint on `8080`)
- **Legacy Implementation (`wss.py`):** Ran two separate processes/ports: `PORT_SERIAL = 8080` for valve commands and `PORT_INSTRUMENTATION = 8888` for LabJack telemetry. Furthermore, `self.__wss_instance` stored only a single `websocket` reference (`self.__wss_instance = websocket`), meaning if a second client connected or a browser tab refreshed, the previous reference was overwritten or crashed on send.
- **New Server Design:** Runs a single `websockets` server on `0.0.0.0:8080` (path `/ws`), maintaining a `set[ServerConnection]` of active clients. Both telemetry streams and valve command/feedback frames are multiplexed over the same WebSocket connection using a JSON `"type"` field (`"telemetry"`, `"valve_state"`, `"command"`, `"abort"`, `"init"`, `"alert"`, `"remote_data_list"`).
- **Why:** Matches [`Flow-Control-System-Client/contents/qml/main.qml`](../../Flow-Control-System-Client/contents/qml/main.qml)'s single `sharedWebSocket` (`ws://127.0.0.1:8080/ws` / `ws://192.168.0.1:8080/ws`), prevents half-connected states where telemetry works but valve controls are dead, and allows multiple mission control laptops to monitor the test stand simultaneously.

### 1.2 Eliminating `tmp.txt` File Polling for LabJack IPC
- **Legacy Implementation (`read_labjack.py:292` & `wss.py:110`):** `read_labjack.py` overwrote a file named `tmp.txt` on every USB stream packet (`with open('tmp.txt', 'w') as tmp:`), while `wss.py` ran a `while True:` loop opening and reading `tmp.txt` every `1 ms`.
- **Why that was problematic:** Opening, truncating, and reading a file on disk hundreds of times per second causes SD/SSD wear, high CPU overhead, and frequent `json.JSONDecodeError` crashes when `wss.py` reads `tmp.txt` mid-write (partially mitigated in legacy code by checking `if len(lines) > 1` for a trailing `\n!`).
- **New Server Design:** Runs the LabJack U6 stream reader in a dedicated background thread (or `asyncio` executor) that pushes converted sample dictionaries directly into an in-memory `asyncio.Queue` / ring buffer and writes sequentially to an append-only session log file.

### 1.3 Replacing `asyncio.LifoQueue` with `asyncio.Queue` (FIFO)
- **Legacy Implementation (`PDP-Monitoring-System/src/main.py:15-16`):** Used `asyncio.LifoQueue()` (Last-In, First-Out stack) for both `serial_command_queue` and `serial_feedback_queue`.
- **Why that was a bug:** If two valve commands or two serial feedback frames arrived within the `100 ms` polling sleep (`await asyncio.sleep(0.1)`), a LIFO stack processes them in **reverse chronological order**, potentially executing an older command after a newer command or reporting an outdated limit-switch state after the latest state!
- **New Server Design:** Uses standard FIFO `asyncio.Queue()` for normal commands/feedback, with an immediate priority bypass for `ABORT` commands.

### 1.4 Fixing Early `return` in `SUMMARY` Serial Parsing
- **Legacy Implementation (`serialInterface.py:270-285`):**
  ```python
  if message_array[1] == "SUMMARY":
      for i in range(2, len(message_array), 2):
          current_valve = message_array[i]
          current_action = message_array[i + 1]
          if self.__valve_state[current_valve] != current_action:
              self.__valve_state[current_valve] = current_action
              return feedback  # <-- BUG: exits loop after the FIRST changed valve!
  ```
- **Why that was a bug:** When `Controls.ino` executes `ABORT` or `STARTUP`, multiple valves change state simultaneously in a single `VC,SUMMARY,N2OF,CLOSE,N2OV,OPEN,...` line. Returning on the first changed valve caused `serialInterface.py` to drop all remaining valve updates in that `SUMMARY` packet.
- **New Server Design:** Iterates through all `(valve, state)` pairs in `VC,SUMMARY,...`, updates the complete valve state dictionary, and emits updates for **every** changed valve (or a combined `valve_state` map).

### 1.5 Converting SI Sensor Readings to Engineering Units Expected by the Client
- **Legacy Implementation (`read_labjack.py:257-290`):** Multiplied pressure voltages by `6895` to output Pascals ($\text{Pa}$), load cells in Newtons ($\text{N}$), and thermocouples in Kelvin ($\text{K}$ via `V_to_K()`).
- **Client Expectations (`Flow-Control-System-Client/contents/config/graphs/graphs.json` & `telemetry_files.py`):**
  - Pressure channels (`P_*`): **`PSI`**
  - Temperature channels (`T_*`): **`°C`** (Celsius)
  - Load channels (`L_RUN_TANK`, `L_THRUST`): **`lbf`** (or `kg` / `N` as configured)
- **New Server Design:** Converts voltages directly to the engineering units configured in the client (`PSI`, `°C`, `lbf`) so live WebSocket telemetry and recorded CSV logs match the scales and units displayed on `Graphs.qml` without requiring manual post-processing.

### 1.6 Consolidating Server Entirely into Pure Python (Avoiding C++/Python IPC)
- **Context & Motivation:** In complex robotic or telemetry stacks, developers often split high-speed hardware drivers into C/C++ daemons or compile C++ extensions (such as using Cython, pybind11, or separate C++ binaries communicating over IPC/sockets with Python).
- **Why Pure Python is Superior for Flow Control Server:**
  - **Single Runtime & Deployment Simplicity:** The entire Valve Cart server runs under a single standard `python3` process managed by `systemd` (`flow-control-server.service`). There are no native shared libraries (`.so`) to recompile when Python or glibc versions change on the Ubuntu mini-PC, and no secondary C++ daemon to synchronize or monitor.
  - **Native Python Hardware Drivers:**
    - `pyserial` provides asynchronous, direct non-blocking serial communication with the Arduino Mega without requiring a C++ serial bridge.
    - `LabJackPython` already wraps the underlying low-level USB drivers (`libusb-1.0` and `exodriver`) in C while exposing high-speed stream methods directly in Python.
  - **Eliminating Inter-Process Serialization Latency:** By keeping the serial controller, LabJack acquisition loop, WebSocket server, and file logger within one unified Python `asyncio` event loop (with a background daemon thread for synchronous LabJack streaming), data frames pass in-memory as native Python dictionaries. There is zero overhead from inter-process pipes, shared memory semaphores, or intermediate serialization between C++ and Python.
  - **Microdot / Asyncio Lightweight Footprint:** The entire backend can operate efficiently on the Valve Cart Mini PC's modest hardware with low memory footprint and predictable garbage collection.
- **Microcontroller Role (Embedded C++ Limited Strictly to Arduino Firmware):**
  - The only C++ code in the entire Valve Cart ecosystem is strictly confined to the embedded Arduino microcontroller itself (`Controls.ino` / `Valve.cpp`), where hard real-time microsecond stepper motor pulse timing (`delayMicroseconds(50)`) and hardware limit switch interrupt sensing are required. The server side remains 100% Python, communicating over standard USB Serial ASCII strings.


---

## 2. WebSocket Protocol (Client $\leftrightarrow$ Server)

- **Endpoint:** `ws://192.168.0.1:8080/ws` (Production Valve Cart) or `ws://127.0.0.1:8080/ws` (Local Dev/Mock)
- **Framing:** UTF-8 JSON text frames (supports both single JSON objects and newline-delimited JSONL batches).

### 2.1 Server $\rightarrow$ Client Packets

| `type` | Purpose | Payload Schema |
| :--- | :--- | :--- |
| `"init"` | Sent on client connection | `{"type": "init", "system": "Flow-Control-System-Server", "armed": false, "recording": false, "valves": {"MEV": "CLOSED", "N2OV": "CLOSED", "RTV": "VENTING", "N2F": "CLOSED", "ERV": "CLOSED", "N2OF": "CLOSED", "NCV": "CLOSED"}}` |
| `"telemetry"` | Periodic sensor broadcast (10–50 Hz) | `{"type": "telemetry", "data": {"packet_id": 105, "seconds": 10.5, "uptime": 342.1, "P_RUN_TANK": 740.2, "P_N2O_FLOW": 610.4, "P_N2_FLOW": 798.1, "P_COMB_CHMBR": 420.0, "P_INJECTOR": 540.3, "T_RUN_TANK": 21.0, "T_INJECTOR": 22.4, "T_COMB_CHMBR": 680.5, "T_POST_COMB": 520.1, "L_RUN_TANK": 45.2, "L_THRUST": 380.0}}` |
| `"valve_state"` | Valve limit-switch / solenoid transition | Single: `{"type": "valve_state", "valve": "MEV", "state": "OPEN"}`<br/>Bulk: `{"type": "valve_state", "valves": {"MEV": "OPEN", ...}}` |
| `"alert"` | Server / Arduino status or fault toast | `{"type": "alert", "level": "INFO" \| "WARN" \| "ERR", "message": "..."}` |
| `"remote_data_list"` | List of recorded logs on the Valve Cart | `{"type": "remote_data_list", "files": ["run_2026_11_04_hotfire.csv", ...]}` |
| `"remote_data_content"` | Contents of a requested remote log file | `{"type": "remote_data_content", "filename": "run_2026_11_04_hotfire.csv", "mode": "load" \| "download", "content": "..."}` |

### 2.2 Client $\rightarrow$ Server Packets

| `type` | Purpose | Payload Schema | Server Action |
| :--- | :--- | :--- | :--- |
| `"command"` | Actuate a valve | `{"type": "command", "command": "CTRL", "valve": "MEV", "action": "OPEN"}` | Writes `MCC,CTRL,MEV,OPEN\n` to Arduino serial |
| `"abort"` | Emergency abort | `{"type": "abort", "command": "ABORT"}` | Clears command queue, writes `MCC,ABORT\n` to Arduino serial, broadcasts alert |
| `"unabort"` | Clear abort lockout | `{"type": "unabort", "command": "UNABORT"}` | Writes `MCC,UNABORT\n` to Arduino serial |
| `"summary"` | Poll all valve limit switches | `{"type": "summary", "command": "SUMMARY"}` | Writes `MCC,SUMMARY\n` to Arduino serial |
| `"record"` | Start/stop on-cart disk logging | `{"type": "record", "active": true, "name": "optional_tag"}` | Starts or closes the active CSV/JSONL recording file on the Mini PC |
| `"remote_data_req"` | Browse/load/rename/delete cart logs | `{"type": "remote_data_req", "operation": "list" \| "get" \| "rename" \| "delete", ...}` | Performs file operation in `/home/uvr/Flow-Control-System-Server/data/` and replies |

---

## 3. Controls Arduino Serial Protocol (Server $\leftrightarrow$ Arduino)

Referenced from [`PDP-Monitoring-System/Docs/serial-api.md`](../../PDP-Monitoring-System/Docs/serial-api.md) and [`PDP-Monitoring-System/src/controlsFirmaware/Controls.ino`](../../PDP-Monitoring-System/src/controlsFirmaware/Controls.ino).

- **Port:** `/dev/ttyACM0` (Linux) or `COM6` (Windows), configurable via environment variable `FCS_SERIAL_PORT`.
- **Settings:** `115200` baud, `8N1`, newline (`\n`) terminated, comma-separated (`NO SPACES`), all uppercase.

### 3.1 Server $\rightarrow$ Arduino Commands

| Command Format | Example | Description in `Controls.ino` |
| :--- | :--- | :--- |
| `MCC,CONNECT\n` | `MCC,CONNECT\n` | Replies with `VC,STATUS,ESTABLISH` followed by `sendState()` (`VC,SUMMARY,...`). |
| `MCC,SUMMARY\n` | `MCC,SUMMARY\n` | Replies with current limit-switch states for all 7 valves (`VC,SUMMARY,...`). |
| `MCC,CTRL,<VALVE>,<ACTION>\n` | `MCC,CTRL,MEV,OPEN\n` | Sets `TargetState[index]` for `<VALVE>` (`-1` for `OPEN`, `1` for `CLOSE`). Multiple valves can be chained in one line. |
| `MCC,ABORT\n` | `MCC,ABORT\n` | Sets `ABORT = true`, cuts power to `Igniter` and `Solenoid`, closes `N2OF`, `N2F`, `MEV`, `RTV`, `NCV`, and opens `N2OV` (vent) and `ERV` (relief). Overrides hardware keyswitch lockout. |
| `MCC,UNABORT\n` | `MCC,UNABORT\n` | Cancels `ABORT` mode and replies `VC,STATUS,CANCELLED ABORT`. |

> **Firmware Bug Note in `Controls.ino` (Line 239):**
> In `Controls.ino:239`, `setTarget()` has an empty `if (Label == "N2OF") { }` block that omits `index = 0;` (though `index` is initialized to `0` at the top of `setTarget()`, if multiple valves are sent in a single `CTRL` command where `N2OF` is not first, `index` would retain the previous valve's index). Sending one valve per `MCC,CTRL,<VALVE>,<ACTION>\n` command avoids this bug completely.

### 3.2 Arduino $\rightarrow$ Server Responses

| Response Format | Example | Meaning |
| :--- | :--- | :--- |
| `VC,STATUS,STARTUP,SUCCESS` | Sent on Arduino boot | Arduino initialized pins and entered `setup()`. |
| `VC,STATUS,ESTABLISH` | Sent after `MCC,CONNECT` | Serial handshake confirmed. |
| `VC,STATUS,ABORTED` | Sent on `ABORT` or hardware `EStop` (`Pin 6 == HIGH`) | Arduino entered emergency abort state. Server must immediately broadcast an `ERR` alert toast to the client. |
| `VC,STATUS,CANCELLED ABORT` | Sent after `MCC,UNABORT` | Abort cleared. |
| `VC,SUMMARY,N2OF,<st>,N2OV,<st>,N2F,<st>,RTV,<st>,ERV,<st>,MEV,<st>,NCV,<st>` | `VC,SUMMARY,N2OF,CLOSE,N2OV,OPEN,N2F,CLOSE,RTV,VENTING,ERV,TRANSIT,MEV,CLOSE,NCV,CLOSE` | Emitted whenever any limit switch changes state (`getChange()`) or in response to `CONNECT`/`SUMMARY`. |
| `VC,ERROR,UNKNOWNCOMMAND,...` | `VC,ERROR,UNKNOWNCOMMAND,MCC,FOO,` | Unrecognized command type. |
| `VC,ERROR,UNKOWNVALVE` | `VC,ERROR,UNKOWNVALVE` | Unrecognized valve label in `CTRL`. |

### 3.3 Valve Hardware & Limit Switch Reference (`Controls.ino`)

| Valve ID | Description | Actuator Type | Open Limit Pin | Close Limit Pin | Step / Enable Pin | Dir Pin | States Reported |
| :--- | :--- | :--- | :---: | :---: | :---: | :---: | :--- |
| **`MEV`** | Main Engine Valve | Stepper (`speed=4`) | 30 | 27 | Step: 19, En: 51 | 20 | `OPEN`, `CLOSE`, `TRANSIT` |
| **`N2OV`** | Nitrous Vent Valve | Stepper (`speed=4`) | 41 | 40 | Step: 4, En: 47 | 3 | `OPEN`, `CLOSE`, `TRANSIT` |
| **`RTV`** | Run Tank Valve | Solenoid | 39 | 38 | En: 37 | — | `FILLING` (open), `VENTING` (closed), `TRANSIT` |
| **`N2F`** | Nitrogen Fill Valve | Stepper (`speed=20`, reversed) | 42 | 43 | Step: 10, En: 49 | 9 | `OPEN`, `CLOSE`, `TRANSIT` |
| **`ERV`** | Electronic Relief Valve | Stepper (`speed=2`) | 34 | 35 | Step: 15, En: 50 | 14 | `OPEN`, `CLOSE`, `TRANSIT` |
| **`N2OF`** | Nitrous Fill Valve | Stepper (`speed=4`) | 33 | 32 | Step: 17, En: 46 | 16 | `OPEN`, `CLOSE`, `TRANSIT` |
| **`NCV`** | Nitrogen Cart Valve | Solenoid (`Pin 48`) | 28 | 29 | Solenoid: 48 | — | `OPEN`, `CLOSE`, `TRANSIT` |
| **`IGPRIME`** | Igniter Prime Relay | Digital Output | — | — | Pin 45 | — | Controlled via `CTRL,IGPRIME,OPEN/CLOSE` |

---

## 4. LabJack U6 Instrumentation Pipeline

Referenced from [`PDP-Monitoring-System/src/instrumentation/read_labjack.py`](../../PDP-Monitoring-System/src/instrumentation/read_labjack.py) and [`thermocouple.py`](../../PDP-Monitoring-System/src/instrumentation/thermocouple.py).

### 4.1 Stream Configuration
- **Device:** LabJack U6 (`u6.U6()`)
- **Scan Frequency:** `1000 Hz` (`scan_frequency = 1000`)
- **Resolution Index:** `2`, **Settling Factor:** `2`, **Samples Per Packet:** `12`

### 4.2 Sensor Channel Table & Unit Conversions

| Channel Key | Sensor Description | LabJack Pin | Mode & Gain | Legacy Output (`read_labjack.py`) | Target Client Output (`graphs.json`) |
| :--- | :--- | :---: | :---: | :--- | :--- |
| `P_RUN_TANK` | Run Tank Pressure | `AIN81` | `DIFF \| X100` | $\text{Pa}$ (`2 * 6895 * 10206.74 * V`) | **`PSI`** (`2 * 10206.74 * V`) |
| `P_INJECTOR` | Injector Pressure | `AIN82` | `DIFF \| X100` | $\text{Pa}$ (`2 * 6895 * 10170.95 * V`) | **`PSI`** (`2 * 10170.95 * V`) |
| `P_N2_FLOW` | $\text{N}_2$ Flow Pressure | `AIN83` | `DIFF \| X100` | $\text{Pa}$ (`2 * 6895 * 9936.41 * V`) | **`PSI`** (`2 * 9936.41 * V`) |
| `P_N2O_FLOW` | $\text{N}_2\text{O}$ Flow Pressure | `AIN84` | `DIFF \| X100` | $\text{Pa}$ (`2 * 6895 * 9982.03 * V`) | **`PSI`** (`2 * 9982.03 * V`) |
| `P_COMB_CHMBR` | Combustion Chamber Pressure | `AIN85` | `DIFF \| X100` | $\text{Pa}$ (`2 * 6895 * 0.0 * V`) | **`PSI`** (Calibrate gain) |
| `L_RUN_TANK` | Run Tank Load Cell | `AIN86` | `DIFF \| X1000` | $\text{N}$ (`2014.22 * V - 5.8`) | **`lbf`** ($\text{N} / 4.44822$) or **`kg`** ($\text{N} / 9.80665$) |
| `L_THRUST` | Engine Thrust Load Cell | `AIN87` | `DIFF \| X1000` | $\text{N}$ (`466000 * V - 25.8`) | **`lbf`** ($\text{N} / 4.44822$) or **`N`** |
| `T_RUN_TANK` | Run Tank Thermocouple (Type K) | `AIN49` | `SING \| X100` | $\text{K}$ (`V_to_K(V, V_ref)`) | **`°C`** (`V_to_K(V, V_ref) - 273.15`) |
| `T_POST_COMB` | Post-Combustion Thermocouple | `AIN53` | `SING \| X100` | $\text{K}$ (`V_to_K(V, V_ref)`) | **`°C`** (`V_to_K(V, V_ref) - 273.15`) |
| `T_COMB_CHMBR` | Combustion Chamber Thermocouple | `AIN55` | `SING \| X100` | $\text{K}$ (`V_to_K(V, V_ref)`) | **`°C`** (`V_to_K(V, V_ref) - 273.15`) |
| `T_INJECTOR` | Injector Thermocouple | `AIN57` | `SING \| X100` | $\text{K}$ (`V_to_K(V, V_ref)`) | **`°C`** (`V_to_K(V, V_ref) - 273.15`) |
