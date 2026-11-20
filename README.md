# Flow Control System Server

Unified FastAPI, Uvicorn, WebSocket, and hardware acquisition backend for the UVic Rocketry Valve Cart (`Flow-Control-System`).

---

## Architecture Overview

The server runs as a single `asyncio` Python process on the Valve Cart Mini PC (`192.168.0.1:8080`, or `127.0.0.1:8080` locally in mock/dev mode) and exposes both real-time WebSocket control/telemetry and streaming HTTP log management:

- **HTTP + WebSocket Server ([`contents/code/ws_server.py`](contents/code/ws_server.py)):**
  - **`WS /ws`:** Multiplexed low-latency JSON/JSONL endpoint pushing `init` (system state, actuator definitions from `actuators.json`, and validated sensor channel metadata from `sensors.json`), decimated `telemetry`, `valve_state` / `summary`, `armed`, and `alert` frames, while receiving `command` (`CTRL`), priority `abort` (`ABORT`), `unabort`, `summary`, `armed`/`arm`/`safe`, and `record` commands. Falls back to WebSocket-only mode if `fastapi`/`uvicorn` are unavailable.
  - **`HTTP /api/data/...`:** Streaming telemetry log CRUD (`GET /api/data`, `GET /api/data/{filename}`, `PUT /api/data/{filename}`, `POST /api/data/{filename}/rename`, `DELETE /api/data/{filename}`) so large file transfers never block telemetry broadcasts or `ABORT`.
- **Sensor Configuration ([`contents/code/sensors_config.py`](contents/code/sensors_config.py) & [`contents/config/sensors.json`](contents/config/sensors.json)):** Pydantic-validated (with stdlib fallback) definition of stream scan rate, differential/single-ended LabJack channels, gains, linear scales/offsets (`kPa`, `M_RUN_TANK` in `kg`, `L_THRUST` in `N`, `C`), and open-thermocouple channel flags.
- **DAQ Instrumentation ([`contents/code/instrumentation.py`](contents/code/instrumentation.py) & [`contents/code/thermocouple.py`](contents/code/thermocouple.py)):** Dedicated daemon thread reading LabJack U6 USB bulk streams (`1000 Hz` scan / `~83.3 Hz` packets), converting NIST ITS-90 Type-K thermocouples (`V_to_C`), emitting `math.nan` + rate-limited `WARN` alerts on open-circuit probes, writing full-rate packets to disk, and decimating WS broadcasts (`--rate`).
- **On-Cart Recorder ([`contents/code/data_recorder.py`](contents/code/data_recorder.py)):** Thread-safe two-row header CSV recorder (`kPa,...,kg,N,C,...,s` + channel names), path-traversal sanitizer (`sanitize_filename`), and NaN-safe JSON sanitizer (`sanitize_telemetry_for_json`).
- **Unified Console Logging ([`contents/code/logging_config.py`](contents/code/logging_config.py)):** Aligns `fcs.*`, `uvicorn`, and `http` (`uvicorn.access`) console output into `INFO/WARN/ERR: <DATE> <type> - <message>` with right-aligned short subsystem tags and TTY ANSI color/dimming.
- **Hardware Simulation ([`contents/code/mocks.py`](contents/code/mocks.py)):** Realistic propellant/chamber/load telemetry waveforms and asynchronous valve transit simulation (`--mock`, or auto-enabled per subsystem when LabJack U6 or Arduino serial hardware is absent).
- **Arduino Actuation Contract ([`Docs/actuation-spec.md`](Docs/actuation-spec.md)):** Specifies `contents/code/serial_controller.py` for `/dev/ttyACM0` (`115200` baud) FIFO queue + priority `ABORT` bypass *(held off cleanly so hardware integration can drop directly onto `server.serial_controller`)*.

---

## Quickstart & CLI Options

Install dependencies (or deploy via `tools/push-code.sh` into `.venv`):
```sh
pip install -r requirements.txt
```

Run in full mock/development mode:
```sh
python contents/code/main.py --mock
```

Hardware auto-detection (uses LabJack U6 if attached, falling back to mock per missing subsystem):
```sh
python contents/code/main.py
```

### CLI Flags & Environment Variables
| CLI Flag | Env Variable | Default | Description |
| :--- | :--- | :--- | :--- |
| `--host` | `FCS_HOST` | `0.0.0.0` | HTTP + WebSocket bind address |
| `--port` | `FCS_PORT` | `8080` | HTTP + WebSocket bind port (`GET /docs` available in FastAPI mode) |
| `--mock` | `FCS_MOCK` (`1`) | `False` | Force mock simulation for both telemetry and actuation |
| `--mock-labjack` | `FCS_MOCK_LABJACK` (`1`) | `False` | Force mock telemetry simulation for LabJack U6 only |
| `--mock-serial` | `FCS_MOCK_SERIAL` (`1`) | `False` | Force mock valve actuation for Arduino serial only |
| `--serial-port` | `FCS_SERIAL_PORT` | `/dev/ttyACM0` | Controls Arduino serial port path |
| `--armed` / `--safe` | — | `--armed` only when full mock | Initial `ARMED`/`SAFE` interlock state |
| `--rate` | `FCS_BROADCAST_HZ` | `20.0` | WebSocket telemetry broadcast decimation rate (Hz) |
| `--transit-delay` | `FCS_TRANSIT_DELAY` | `1.5` | Mock valve limit-switch transit duration (seconds) |
| `--config` | — | `contents/config/actuators.json` | Path to canonical actuator definitions JSON |
| `--sensors-config` | — | `contents/config/sensors.json` | Path to validated LabJack `sensors.json` config |
| `--data-dir` | — | `data/` | Directory for recorded CSV/JSONL telemetry logs |
| `--log-level` | `FCS_LOG_LEVEL` | `INFO` | Logging verbosity (`DEBUG`, `INFO`, `WARNING`, `ERROR`) |

### Interactive Console Commands
When started from an interactive TTY, stdin accepts:
- `arm` / `safe` — Toggle server interlock state (`armed`)
- `abort` — Trigger emergency abort (`SAFE` + fill close + dump/vent open)
- `reset` — Reload `actuators.json` and broadcast updated `init`
- `state <ID> <STATE>` — Force an actuator state (e.g. `state N2OV OPEN`)
- `alert <LVL> <MSG>` — Broadcast an operator alert toast to connected clients

---

## Repository Layout

```text
Flow-Control-System-Server/
├── .github/workflows/ci.yml             # Shell syntax/LF checks, ruff, pytest
├── Docs/                                # Architecture, WS/HTTP protocol, serial spec, roadmaps
├── contents/
│   ├── code/
│   │   ├── main.py                      # Asyncio entrypoint & CLI
│   │   ├── ws_server.py                 # FastAPI + Uvicorn server (WS /ws + HTTP /api/data)
│   │   ├── instrumentation.py           # LabJack U6 USB streaming & WS decimation
│   │   ├── thermocouple.py              # Type-K NIST ITS-90 math & open-circuit NaN
│   │   ├── sensors_config.py            # Pydantic/stdlib sensors.json validation & metadata
│   │   ├── data_recorder.py             # Full-rate two-row CSV recorder & path sanitization
│   │   ├── logging_config.py            # Unified FCS + Uvicorn log formatter
│   │   ├── mocks.py                     # Mock telemetry & valve transit simulator
│   │   └── direct-eth-detect.sh         # Server-side direct-Ethernet auto-configuration
│   └── config/
│       ├── actuators.json               # Canonical valve/ematch definitions & safety flags
│       ├── sensors.json                 # LabJack stream config & calibration constants
│       └── flow-control-server.service  # systemd unit (.venv/bin/python -u main.py)
├── tools/
│   ├── _common.sh                       # Shared SSH/sudo/password.txt helpers
│   ├── push-code.sh                     # Incremental rsync (--exclude data/ & .venv/) + restart
│   ├── services.sh                      # Manage systemd unit, tail logs, fetch/push offline wheels
│   └── direct-ethernet.sh               # Configure PC-to-cart direct Ethernet bridge
└── tests/                               # unittest / pytest suite (interlocks, HTTP, CSV, logging)
```

---

## Deployment, Operations & Testing

- **Deploy code & restart `systemd` service** (preserves remote `data/` and `.venv/`):
  ```sh
  ./tools/push-code.sh
  ```
- **Manage service, tail logs, or cache offline wheels:**
  ```sh
  ./tools/services.sh status
  ./tools/services.sh logs -f
  ./tools/services.sh fetch-deps && ./tools/services.sh push-deps
  ```
- **Direct Ethernet connection (no router):**
  ```sh
  ./tools/direct-ethernet.sh initiate
  ```
- **Run tests and linter locally:**
  ```sh
  ruff check contents/code tests
  pytest -v
  ```

For full protocol details and design rationale, see [`Docs/README.md`](Docs/README.md).
