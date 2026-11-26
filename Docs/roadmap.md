# Flow Control System Server — Implementation Roadmap

This roadmap tracks the Python Valve Cart backend architecture (`Flow-Control-System-Server`) replacing [`PDP-Monitoring-System`](../../PDP-Monitoring-System/). All server components run in modern asynchronous Python (`asyncio` + FastAPI + Uvicorn), reserving C++ strictly for the embedded Arduino firmware (`Controls.ino`).

---

## Repository Directory Structure

```text
Flow-Control-System-Server/
├── Docs/
│   ├── README.md                       # Documentation index & network topology
│   ├── actuation-spec.md               # Contract for Controls Arduino serial_controller.py
│   ├── protocol-and-architecture.md    # FastAPI/WS, LabJack U6, sensors.json & serial overview
│   ├── roadmap.md                      # This implementation roadmap
│   └── websocket-protocol.md           # WS /ws + HTTP /api/data wire protocol
├── contents/
│   ├── code/
│   │   ├── main.py                     # Asyncio entrypoint & CLI flags (--mock, --host, --port, --rate)
│   │   ├── ws_server.py                # FastAPI + Uvicorn server (WS /ws + HTTP /api/data)
│   │   ├── instrumentation.py          # LabJack U6 stream reader thread & decimated WS publisher
│   │   ├── thermocouple.py             # NIST ITS-90 Type-K math (open-TC -> math.nan)
│   │   ├── sensors_config.py           # Pydantic/stdlib sensors.json validation & WS channel metadata
│   │   ├── data_recorder.py            # Two-row CSV session recorder & path sanitization
│   │   ├── logging_config.py           # Unified FCS + Uvicorn log formatter
│   │   ├── mocks.py                    # Mock telemetry & valve limit-switch transit simulator
│   │   └── serial_controller.py        # Controls Arduino USB serial manager (assigned; see actuation-spec.md)
│   └── config/
│       ├── actuators.json              # Canonical actuator definitions (N2OV, DUMP, RTV, QD, MEV, MRV)
│       ├── sensors.json                # Validated LabJack U6 stream & sensor calibration definitions
│       └── flow-control-server.service # systemd service running .venv/bin/python
├── tests/                              # pytest/unittest suite (interlocks, HTTP, CSV, sensors, logging)
└── tools/
    ├── _common.sh                      # Shared SSH/sudo/password.txt helpers
    ├── push-code.sh                    # Incremental rsync (--exclude data/ & .venv/) + restart
    ├── services.sh                     # systemd control, journalctl, offline wheels/exodriver sync
    └── direct-ethernet.sh              # PC-to-cart direct Ethernet bridge helper
```

---

## Roadmap
Check `[UVR-CH-075] Propulsion Control System` for the overall timeline and work needed for the server.
