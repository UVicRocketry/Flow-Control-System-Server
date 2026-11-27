# Actuator Controls & Serial Interface Specification (`actuation-spec.md`)

This document specifies how to implement the **Controls Arduino serial controller** (`contents/code/serial_controller.py`) and wire it into [`contents/code/main.py`](../contents/code/main.py) and [`contents/code/ws_server.py`](../contents/code/ws_server.py).

It is derived from the legacy implementation in `PDP-Monitoring-System` (`src/serialInterface/serialInterface.py`, `src/controlsFirmaware/Controls.ino`, `src/controlsFirmaware/Valve.cpp`, and `src/main.py`) and updated to fix known legacy bugs within the new unified `asyncio` server architecture.

---

## 1. Architecture Overview

In the new server, **all WebSocket routing, safety interlocks, and client state synchronization live in [`ws_server.py`](../contents/code/ws_server.py)**. Your responsibility is to implement `SerialController` in `contents/code/serial_controller.py` as a drop-in replacement for [`MockHardwareSimulator`](../contents/code/mocks.py) on `server.serial_controller`.

```text
Flow-Control-System-Client (QML)
        │  JSON over WebSocket (ws://0.0.0.0:8080/ws)
        ▼
FlowControlWSServer (ws_server.py)
  - Validates SAFE/ARMED, external, single_use interlocks
  - Calls server.serial_controller.<method>(...)
        │
        ├──(--mock)──▶ MockHardwareSimulator (mocks.py)
        │
        └──(live)────▶ SerialController (serial_controller.py)  ◀── YOU BUILD THIS
                            │  115200 baud USB Serial (/dev/ttyACM0)
                            ▼
                       Controls.ino (Arduino Mega)
                            │  Limit switches + Steppers + Solenoids + EStop
```

---

## 2. Interface Contract with `ws_server.py`

### 2.1 Methods `ws_server.py` Calls on `SerialController`

[`FlowControlWSServer`](../contents/code/ws_server.py) dispatches inbound client messages to `self.serial_controller`. Your class **must** implement these four methods (they may be `async def` or synchronous `def`; `ws_server.py` awaits them if awaitable):

| Method | Triggered By (Client → Server) | Expected Behavior |
| :--- | :--- | :--- |
| `send_valve_command(valve: str, action: str)` | `{"type": "command", "valve": "MEV", "action": "OPEN"}` | Enqueue `MCC,CTRL,<VALVE>,<ACTION>\n` onto the FIFO command queue. |
| `send_abort()` | `{"type": "abort", "command": "ABORT"}` | **Priority bypass:** Clear all pending commands from the FIFO queue and immediately write `MCC,ABORT\n` to serial. |
| `send_unabort()` | `{"type": "unabort", "command": "UNABORT"}` | Write `MCC,UNABORT\n` to serial. |
| `request_summary()` | `{"type": "summary", "command": "SUMMARY"}` | Write `MCC,SUMMARY\n` to serial (Arduino replies with `VC,SUMMARY,...`). |

### 2.2 Callbacks `SerialController` Must Call on `ws_server`

Pass `ws_server: FlowControlWSServer` into `SerialController.__init__(...)`. When serial frames arrive from the Arduino, call these `async` methods on `self.ws_server`:

| `ws_server` Method | When to Call | Effect on Connected Clients |
| :--- | :--- | :--- |
| `await ws_server.update_valve_summary(valves_map, only_changed=True)` | On every `VC,SUMMARY,...` line | Normalizes states (`CLOSE` → `CLOSED`, `TRANSIT` → `ACTUATING`, `FILLING` → `FILL`, `VENTING` → `VENT`) and broadcasts `{"type": "valve_state", "valves": {...}}`. |
| `await ws_server.update_valve_state(valve, state)` | If updating a single actuator | Normalizes and broadcasts `{"type": "valve_state", "valve": ..., "state": ...}`. |
| `await ws_server.send_alert(level, message)` | On `ABORTED`, `CANCELLED ABORT`, `ERROR`, or USB disconnect/reconnect | Broadcasts toast notification (`level` is `"INFO"`, `"WARN"`, or `"ERR"`). |
| `await ws_server.set_armed(False)` | If hardware lockout / keyswitch / E-Stop forces safe state | Synchronizes client SAFE/ARMED interlock state (`{"type": "armed", "armed": false}`). |

---

## 3. Controls Arduino Serial Protocol (`Controls.ino`)

- **Port:** `/dev/ttyACM0` on Ubuntu Mini PC (`COM6` on Windows), configurable via `--serial-port` / `FCS_SERIAL_PORT`.
- **Serial Settings:** `115200` baud, `8N1`, newline (`\n`) terminated ASCII CSV, **no spaces**, all **UPPERCASE**.

### 3.1 Commands to Send (`Server -> Arduino`)

`Controls.ino` parses incoming lines using `readCSV()`:
- Token 0 (`SOURCE`): `"MCC"`
- Token 1 (`TYPE`): `"CONNECT"`, `"CTRL"`, `"ABORT"`, `"UNABORT"`, or `"SUMMARY"`

| Command String | Firmware Handler (`Controls.ino`) | Notes |
| :--- | :--- | :--- |
| `MCC,CONNECT\n` | Prints `VC,STATUS,ESTABLISH` and calls `sendState()` (`VC,SUMMARY,...`). | Send immediately upon opening or reconnecting the serial port. |
| `MCC,SUMMARY\n` | Calls `sendState()` (`VC,SUMMARY,...`). | Polls all current limit-switch states. |
| `MCC,CTRL,<VALVE>,<ACTION>\n` | Calls `setTarget()` to set `TargetState[index]`. | **Send only ONE valve per line** (see firmware bug in Section 5.2). |
| `MCC,ABORT\n` | Sets `ABORT = true`, prints `VC,STATUS,ABORTED`, cuts `Igniter` & `Solenoid` (`LOW`), closes `N2OF`, `MEV`, `RTV`, `NCV`, opens `N2OV` & `ERV`. | Overrides hardware `KeySwitch` lockout (`Controls.ino:190`). |
| `MCC,UNABORT\n` | Clears `ABORT = false`, prints `VC,STATUS,CANCELLED ABORT`. | Only clears if `ABORT == true`. |

#### Action Translation (`actuators.json` → `Controls.ino`)

In [`contents/config/actuators.json`](../contents/config/actuators.json), actuators use UI-friendly actions (`OPEN`, `CLOSE`, `FILL`, `VENT`, `FIRE`, `DISCONNECT`, `DUMP`), whereas `Controls.ino:268-278` only checks `Value == "OPEN"` (`-1`) and `Value == "CLOSE"` (`1`):

- Map `OPEN` / `FILL` → `OPEN`
- Map `CLOSE` / `VENT` → `CLOSE`
- Supported firmware valve labels in `Controls.ino:239-266`:
  - `N2OF`, `N2OV`, `ERV`, `MEV`, `RTV`, `IGPRIME`, `NCV`

---

### 3.2 Messages to Listen For (`Arduino -> Server`)

Your serial reader loop must read `\n`-terminated lines from the Arduino, strip `\r\n`, split on `,`, and dispatch based on the incoming prefix:

#### A. Limit Switch & Valve State Summary (`VC,SUMMARY,...`)
Emitted by `Controls.ino:199-210` (`sendState()`) on `CONNECT`, `SUMMARY`, and **automatically whenever `Valve::getChange()` detects a limit-switch state transition** in `MoveToTarget()`:

```text
VC,SUMMARY,N2OF,<s0>,N2OV,<s1>,RTV,<s2>,ERV,<s3>,MEV,<s4>,NCV,<s5>
```

Example:
```text
VC,SUMMARY,N2OF,CLOSE,N2OV,OPEN,RTV,VENTING,ERV,TRANSIT,MEV,CLOSE,NCV,CLOSE
```

**How `Valve.cpp` determines each state:**
- **Stepper & 2-LS valves (`strState()`):**
  - `OpenLimit == LOW` (active-low `INPUT_PULLUP`) → `"OPEN"`
  - `CloseLimit == LOW` → `"CLOSE"`
  - Neither limit switch active → `"TRANSIT"`
- **`RTV` (`solStrState()`):** Software-tracked solenoid (`activated == 1` → `"VENTING"`, `activated == -1` → `"FILLING"`, else `"TRANSIT"`).

**What `SerialController` must do:**
1. Parse **all** `(valve, state)` pairs from index `2` to `len(parts) - 1` in steps of `2`:
   ```python
   valves_map = {
       parts[i].strip().upper(): parts[i + 1].strip().upper()
       for i in range(2, len(parts) - 1, 2)
   }
   await self.ws_server.update_valve_summary(valves_map, only_changed=True)
   ```
2. `ws_server.update_valve_summary()` automatically handles normalizing `"CLOSE"` → `"CLOSED"`, `"TRANSIT"` → `"ACTUATING"`, `"FILLING"` → `"FILL"`, and `"VENTING"` → `"VENT"`.

#### B. Status & Safety Events (`VC,STATUS,...`)

| Raw Serial Line | Meaning in `Controls.ino` | Required Server Action |
| :--- | :--- | :--- |
| `VC,STATUS,STARTUP,SUCCESS` | Arduino booted / reset (`setup()`). | Log info; send `MCC,CONNECT\n` to sync initial valve states. |
| `VC,STATUS,ESTABLISH` | Handshake confirmed after `MCC,CONNECT`. | Mark serial connection as established; log info. |
| `VC,STATUS,ABORTED` | `MCC,ABORT` received **or** hardware `EStop` (`Pin 6 == HIGH`) triggered. | Immediately call `await ws_server.send_alert("ERR", "Hardware Emergency Abort triggered on Valve Cart!")` and `await ws_server.set_armed(False)`. |
| `VC,STATUS,CANCELLED ABORT` | `MCC,UNABORT` cleared abort state. | Call `await ws_server.send_alert("INFO", "Valve Cart abort state cleared.")`. |

#### C. Firmware Errors & Diagnostics (`VC,ERROR,...`, `VC,CF,...`)

| Raw Serial Line | Meaning in `Controls.ino` | Required Server Action |
| :--- | :--- | :--- |
| `VC,ERROR,UNKNOWNCOMMAND,...` | Firmware did not recognize `TYPE`. | Call `await ws_server.send_alert("WARN", f"Arduino Serial Error: {raw_line}")`. |
| `VC,ERROR,UNKOWNVALVE` | Firmware did not recognize valve label in `CTRL`. | Call `await ws_server.send_alert("WARN", f"Arduino Unknown Valve Error: {raw_line}")`. |
| `VC,CF,...` | Firmware boot/config log line (e.g. `Initalizing_to_safe_state:(ALL Closed)`). | Log at `INFO` level. |

---

## 4. Hardware Safety & Resilience Requirements

1. **USB Hotplug & Auto-Reconnect:**
   - EMI or vibration during hot-fire/cold-flow testing can briefly reset `/dev/ttyACM0`.
   - Catch `serial.SerialException` / `OSError` in both the reader and writer loops.
   - Broadcast `await ws_server.send_alert("WARN", "Arduino serial disconnected; retrying...")`.
   - Retry opening the serial port every `2.0` seconds without crashing the process.
   - On reconnect, immediately send `MCC,CONNECT\n` to resynchronize limit-switch states.
2. **Non-Blocking Serial I/O:**
   - `pyserial`'s `readline()` and `write()` are blocking. Run them via `asyncio.to_thread(...)` / `loop.run_in_executor(...)` (or a dedicated daemon `threading.Thread` pushing lines into an `asyncio.Queue` via `loop.call_soon_threadsafe(...)`) so telemetry and WebSocket heartbeats never stall.
3. **Hardware Keyswitch Awareness:**
   - In `Controls.ino:190`, stepper motion is gated by `digitalRead(KeySwitch) == LOW || ABORT`. If `Controls.ino` is later updated to emit a `VC,STATUS,KEYSWITCH,...` message when the physical keyswitch is in SAFE lockout, `SerialController` should forward a `"WARN"` alert and call `await ws_server.set_armed(False)`.

---

## 5. Legacy Bugs in `PDP-Monitoring-System` to Avoid

Do **not** copy these bugs from the old codebase:

1. **Early `return` in `SUMMARY` parsing (`serialInterface.py:274-282`):**
   - Legacy code returned after the *first* changed valve, dropping all other valve updates in `VC,SUMMARY,...` during `ABORT` or `CONNECT`. Iterate over **all** pairs before emitting.
2. **`asyncio.LifoQueue` (`PDP-Monitoring-System/src/main.py:15-16`):**
   - Legacy code used a LIFO stack, which executed rapid commands and processed limit-switch feedback in reverse order. Use **FIFO `asyncio.Queue()`** for normal commands, with `send_abort()` draining the queue and writing immediately.
3. **`N2OF` missing `index = 0` (`Controls.ino:239-240`):**
   - `setTarget()` has an empty `if (Label == "N2OF") {}` block. If multiple valves are chained in one `MCC,CTRL,...` line and `N2OF` is not first, `index` retains the previous valve's index. **Always send one valve per `MCC,CTRL,<VALVE>,<ACTION>\n` line.**
4. **Wrong `VC,` source prefix (`serialInterface.py:231,253`):**
   - Legacy code sent `VC,CTRL,...` and `VC,ABORT` instead of `MCC,CTRL,...` and `MCC,ABORT`. Always use `MCC,` for commands sent from the server to the Arduino.

---

## 6. What Needs to Be Placed in `contents/code/main.py`

In [`contents/code/main.py`](../contents/code/main.py), `async_main(args)` already branches on `mock_mode`. When `serial_controller.py` is implemented, wire it into `async_main(args)` as follows:

1. **Import `SerialController`** at the top of `contents/code/main.py`:
   ```python
   from serial_controller import SerialController
   ```
2. **Instantiate and attach `SerialController`** when `not mock_mode` inside `async_main(args)`:
   ```python
   mock_sim: MockHardwareSimulator | None = None
   serial_ctrl: SerialController | None = None

   if mock_mode:
       mock_sim = MockHardwareSimulator(
           ws_server=server,
           broadcast_rate_hz=args.rate,
           transit_delay=args.transit_delay,
       )
       server.serial_controller = mock_sim
       server.instrumentation = mock_sim
       mock_sim.start()
   else:
       serial_ctrl = SerialController(
           ws_server=server,
           port=args.serial_port,
           baudrate=115200,
       )
       server.serial_controller = serial_ctrl
       serial_ctrl.start()
   ```
3. **Clean shutdown in the `finally:` block** of `async_main(args)`:
   ```python
   try:
       await server.serve_forever()
   finally:
       if mock_sim is not None:
           await mock_sim.stop()
       if serial_ctrl is not None:
           await serial_ctrl.stop()
   ```
4. **Update `detect_hardware_present()` (when Phase 3 instrumentation is ready):**
   - Currently `detect_hardware_present(args.serial_port)` checks `Path(serial_port).exists()`. Once `serial_controller.py` supports hotplug/auto-reconnect, only enter `mock_mode` when `--mock` is explicitly passed (or keep the auto-fallback if neither Arduino nor LabJack is attached).
