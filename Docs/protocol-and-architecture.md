# Server Architecture & Communication Protocols

This document details the architecture and protocols linking the **Flow Control System Client**, the **FastAPI + Uvicorn Server**, the **Controls Arduino Mega**, and the **LabJack U6 Pro**, along with the rationale behind key design choices relative to the legacy [`PDP-Monitoring-System`](../../PDP-Monitoring-System/).

---

## 1. Architectural Reasoning: Improvements Over Legacy `PDP-Monitoring-System`

### 1.1 Unified FastAPI + Uvicorn Server on Port `8080` (`WS /ws` + `HTTP /api/data`)
- **Legacy (`wss.py`):** Ran two separate WebSocket servers (`8080` for serial controls, `8888` for instrumentation), held only a single client reference (`self.__wss_instance`), and sent up to 16 MB log files JSON-escaped over the control socket.
- **Current Design ([`contents/code/ws_server.py`](../contents/code/ws_server.py)):**
  - Runs one **FastAPI + Uvicorn** server on `0.0.0.0:8080` serving both **`WS /ws`** (multiplexed low-latency JSON control/telemetry) and streaming **`HTTP /api/data/...`** endpoints (backed by [`data_recorder.py`](../contents/code/data_recorder.py)) so large CSV uploads/downloads never stall `ABORT` or live telemetry.
  - Maintains `self.clients: set[Any]` so multiple Mission Control stations can connect simultaneously.
  - Falls back automatically to WebSocket-only mode (`_ws_serve`) if `fastapi`/`uvicorn` are absent in minimal environments.

### 1.2 Eliminating `tmp.txt` Polling for LabJack IPC
- **Legacy:** `read_labjack.py` overwrote `tmp.txt` on every USB packet while `wss.py` polled it every `1 ms`, causing SD/SSD wear and partial-read `JSONDecodeError` failures.
- **Current Design ([`contents/code/instrumentation.py`](../contents/code/instrumentation.py)):** Runs `u6.U6().streamData()` in a dedicated daemon `threading.Thread` that pushes converted channel dicts over `loop.call_soon_threadsafe()` into a bounded `asyncio.Queue`, decoupled from `WS /ws` broadcast decimation (`--rate`, default `20.0 Hz`).

### 1.3 FIFO Command Queue + Priority `ABORT` & Full `VC,SUMMARY` Parsing
- **FIFO Ordering (`asyncio.Queue`):** Avoids the legacy `asyncio.LifoQueue` bug that reversed rapid consecutive commands/feedback.
- **Priority `ABORT` Bypass:** `abort` / `ABORT` bypasses armed interlocks and drains queued actuator commands immediately ([`Docs/actuation-spec.md`](./actuation-spec.md)).
- **Complete `VC,SUMMARY` Parsing:** Evaluates every `(valve, state)` pair per summary frame instead of returning on the first changed valve.

### 1.4 Externalized Pydantic-Validated `sensors.json` & Metric Engineering Units
- **Single Hardware Config ([`contents/config/sensors.json`](../contents/config/sensors.json) & [`contents/code/sensors_config.py`](../contents/code/sensors_config.py)):** Replaces hardcoded pin/gain/unit tables duplicated across Python modules and legacy `Docs`. Only hardware parameters (`id`, `pin`, `mode`, `gain_index`, `scale`, `offset`, `unit`, optional `thermocouple` flag) live in `sensors.json`, keeping UI display preferences (colors, titles) cleanly on the client.
- **Validation:** Uses **Pydantic v2** models (with automatic stdlib validation fallback) to verify stream parameters, LabJack mode bitmasks (`SING`/`DIFF`), valid gains (`1`/`10`/`100`/`1000`), unique non-reserved channel IDs, and finite calibration constants on startup.
- **Metric Defaults & Client Sync:** Outputs pressures in **`kPa`**, run tank mass (`M_RUN_TANK`) in **`kg`**, thrust load (`L_THRUST`) in **`N`**, and Type-K thermocouples in **`°C` (`C`)**, broadcasting `"channels"` inside WS `init` so the client registers exact metadata and source units automatically.
- **Open-Thermocouple Fault Handling ([`contents/code/thermocouple.py`](../contents/code/thermocouple.py)):** Out-of-range voltages (`< -5.891 mV` or `> 54.886 mV`) return `math.nan` instead of `0.0 °C`. `data_recorder.py` records non-finite values as empty CSV cells (`""`), `sanitize_telemetry_for_json()` encodes them as JSON `null`, and `instrumentation.py` emits a rate-limited `WARN` alert toast.

### 1.5 Pure Python Runtime & Unified Uvicorn + FCS Console Logging
- Keeps the entire server in pure Python under `systemd` ([`contents/config/flow-control-server.service`](../contents/config/flow-control-server.service)), reserving C++ strictly for Arduino timing firmware (`Controls.ino`).
- [`contents/code/logging_config.py`](../contents/code/logging_config.py) unifies `fcs.*`, `uvicorn` (`uvicorn.error`), and `http` (`uvicorn.access`) formatting as `INFO/WARN/ERR: <DATE> <type> - <message>`.

---

## 2. Endpoint Summary (`0.0.0.0:8080`)

- **`WS /ws`:** Bidirectional real-time JSON/JSONL socket (`init`, `telemetry`, `valve_state`, `summary`, `armed`, `alert`, `command`, `abort`, `unabort`, `record`, and legacy `remote_data_req` fallback). See [`websocket-protocol.md`](./websocket-protocol.md).
- **HTTP `/api/data/...`:** Streaming log list (`GET /api/data`), download (`GET /api/data/{filename}`), upload (`PUT /api/data/{filename}`), rename (`POST /api/data/{filename}/rename`), and delete (`DELETE /api/data/{filename}`), plus interactive OpenAPI docs at `/docs`.

---

## 3. Controls Arduino Serial Protocol (Server ↔ Arduino)

Referenced from [`PDP-Monitoring-System/src/controlsFirmaware/Controls.ino`](../../PDP-Monitoring-System/src/controlsFirmaware/Controls.ino) and specified in full for [`serial_controller.py`](../Docs/actuation-spec.md):

- **Port:** `/dev/ttyACM0` (Linux) or `COM6` (Windows), configurable via `--serial-port` / `FCS_SERIAL_PORT`.
- **Settings:** `115200` baud, `8N1`, newline (`\n`) terminated, comma-separated (no spaces), uppercase.

### 3.1 Server → Arduino Commands & Arduino → Server Responses
- `MCC,CONNECT\n` → `VC,STATUS,ESTABLISH` + `VC,SUMMARY,...`
- `MCC,SUMMARY\n` → `VC,SUMMARY,N2OF,<st>,N2OV,<st>,N2F,<st>,RTV,<st>,ERV,<st>,MEV,<st>,NCV,<st>`
- `MCC,CTRL,<VALVE>,<ACTION>\n` *(send one valve per command line to avoid the `Controls.ino:239` `N2OF` index bug)*
- `MCC,ABORT\n` → `VC,STATUS,ABORTED` (cuts `Igniter` + `Solenoid`, closes fill/main valves, opens `N2OV`/`ERV`, overrides keyswitch)
- `MCC,UNABORT\n` → `VC,STATUS,CANCELLED ABORT`

---

## 4. LabJack U6 Configuration (`contents/config/sensors.json`)

### 4.1 `stream` Object
| Field | Default | Description |
| :--- | :---: | :--- |
| `scan_frequency` | `1000` | LabJack scan rate in Hz |
| `resolution_index` | `2` | U6 ADC resolution index (`0` = default, higher = lower noise/rate) |
| `settling_factor` | `2` | ADC settling factor (`0` = auto) |
| `samples_per_packet` | `12` | Samples per USB bulk packet (`1000 / 12 ≈ 83.3` packets/sec written to disk when recording) |

### 4.2 Default 11-Channel Calibration Table (`v_eng = scale * V_raw + offset`)

| Channel ID | Pin | Mode | Gain Index | Unit | `scale` | `offset` | Notes |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :--- |
| `P_INJECTOR` | `AIN82` | `DIFF` | `100` | `kPa` | `140257.4005` | `0.0` | Injector pressure |
| `P_COMB_CHMBR` | `AIN85` | `DIFF` | `100` | `kPa` | `0.0` | `0.0` | Combustion chamber pressure *(set scale upon calibration)* |
| `P_N2O_FLOW` | `AIN84` | `DIFF` | `100` | `kPa` | `137652.1937` | `0.0` | $\text{N}_2\text{O}$ flow pressure |
| `P_N2_FLOW` | `AIN83` | `DIFF` | `100` | `kPa` | `137023.0939` | `0.0` | $\text{N}_2$ flow pressure |
| `P_RUN_TANK` | `AIN81` | `DIFF` | `100` | `kPa` | `140750.9446` | `0.0` | Run tank pressure |
| `M_RUN_TANK` | `AIN86` | `DIFF` | `1000` | `kg` | `2014.22` | `-5.8` | Run tank mass load cell (`kg`, not `N`) |
| `L_THRUST` | `AIN87` | `DIFF` | `1000` | `N` | `466000.0` | `-25.8` | Thrust stand load cell |
| `T_RUN_TANK` | `AIN49` | `SING` | `100` | `C` | — | — | Type-K TC (`thermocouple.py` `V_to_C`) |
| `T_INJECTOR` | `AIN57` | `SING` | `100` | `C` | — | — | Type-K TC (`thermocouple.py` `V_to_C`) |
| `T_COMB_CHMBR` | `AIN55` | `SING` | `100` | `C` | — | — | Type-K TC (`thermocouple.py` `V_to_C`) |
| `T_POST_COMB` | `AIN53` | `SING` | `100` | `C` | — | — | Type-K TC (`thermocouple.py` `V_to_C`) |

*Note:* Channel IDs starting with `T_` (or explicitly tagged `"thermocouple": true`) automatically invoke `V_to_C(v, v_ref)` (`thermocouple.py`), returning `math.nan` on open-circuit/out-of-range voltages.
