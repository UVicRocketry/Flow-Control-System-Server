#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unified Asyncio WebSocket Server (`ws_server.py`) for
Flow-Control-System-Server.

Implements the Server <-> Client WebSocket wire protocol on
ws://0.0.0.0:8080/ws:
  - Multi-client connection registry & non-blocking broadcast
  - Initial handshake (`init`) with dynamic actuator definitions
  - Inbound message router (`command`/`CTRL`, `abort`, `unabort`, `summary`,
    `record`, `remote_data_req`)
  - Hardware safety interlock validation (armed, external, single-use)
  - On-cart two-row header CSV telemetry recording & remote file RPC
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from websockets.asyncio.server import (  # type: ignore
        broadcast as _ws_broadcast,
        serve as _ws_serve,
    )
    from websockets.exceptions import (  # type: ignore
        ConnectionClosed as _WSConnectionClosed,
    )
except ImportError:
    import websockets  # type: ignore
    from websockets.exceptions import (  # type: ignore
        ConnectionClosed as _WSConnectionClosed,
    )

    _ws_serve = websockets.serve
    _ws_broadcast = getattr(websockets, "broadcast", None)

logger = logging.getLogger("fcs.ws_server")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ACTUATORS_CONFIG = REPO_ROOT / "contents" / "config" / "actuators.json"
DEFAULT_DATA_DIR = REPO_ROOT / "data"

# Two-row CSV format expected by Client and Propulsion-Test-Data
CSV_UNITS_ROW = "kPa,kPa,kPa,kPa,kPa,N,N,C,C,C,C,s"
CSV_CHANNELS = (
    "P_INJECTOR",
    "P_COMB_CHMBR",
    "P_N2O_FLOW",
    "P_N2_FLOW",
    "P_RUN_TANK",
    "L_RUN_TANK",
    "L_THRUST",
    "T_RUN_TANK",
    "T_INJECTOR",
    "T_COMB_CHMBR",
    "T_POST_COMB",
    "seconds",
)
CSV_CHANNELS_ROW = ",".join(CSV_CHANNELS)

# Firmware / legacy valve state aliases -> normalized client states
STATE_NORMALIZATION_MAP: dict[str, str] = {
    "CLOSE": "CLOSED",
    "CLOSED": "CLOSED",
    "OPEN": "OPEN",
    "TRANSIT": "ACTUATING",
    "ACTUATING": "ACTUATING",
    "FILLING": "FILL",
    "FILL": "FILL",
    "VENTING": "VENT",
    "VENT": "VENT",
    "CONNECT": "CONNECTED",
    "CONNECTED": "CONNECTED",
    "DISCONNECT": "DISCONNECTED",
    "DISCONNECTED": "DISCONNECTED",
    "FIRE": "FIRED",
    "FIRED": "FIRED",
    "READY": "READY",
}


def normalize_valve_state(state: str) -> str:
    """Normalize hardware/legacy state strings to standard client states."""
    raw = str(state or "").strip().upper()
    return STATE_NORMALIZATION_MAP.get(raw, raw)


def sanitize_filename(name: str | None) -> str | None:
    """
    Sanitize a filename so remote_data_req / record cannot escape data_dir.
    Returns None if the filename is empty or invalid.
    """
    if not name or not isinstance(name, str):
        return None
    cleaned = name.strip()
    if not cleaned or "/" in cleaned or "\\" in cleaned or ".." in cleaned:
        return None
    base = Path(cleaned).name
    if not base or base in (".", ".."):
        return None
    return base


def _flatten_sample(sample: dict[str, Any]) -> dict[str, Any]:
    """Flatten grouped telemetry sub-dictionaries into a single dict."""
    flat: dict[str, Any] = dict(sample)
    for group_key in ("pressures", "temperatures", "loads"):
        sub = sample.get(group_key)
        if isinstance(sub, dict):
            flat.update(sub)
    return flat


def _format_csv_row(flat: dict[str, Any], rec_seconds: float) -> str:
    """Format a flattened telemetry sample into a CSV data row."""
    row_values: list[str] = []
    for ch in CSV_CHANNELS:
        val = rec_seconds if ch == "seconds" else flat.get(ch, 0.0)
        try:
            row_values.append(f"{float(val):.4f}")
        except (TypeError, ValueError):
            row_values.append("0.0000")
    return ",".join(row_values) + "\n"


class FlowControlWSServer:
    """
    Unified WebSocket server for the Valve Cart Flow Control System.

    Manages:
      - Multi-client WebSocket connections (`ws://<host>:<port>/ws`)
      - Actuator definitions (`contents/config/actuators.json`) and states
      - Inbound command routing and safety interlock enforcement
      - On-cart CSV session recording and remote file RPC
    """

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 8080,
        armed: bool = False,
        config_path: Path | str | None = None,
        data_dir: Path | str | None = None,
        serial_controller: Any = None,
        instrumentation: Any = None,
        data_recorder: Any = None,
        system_name: str = "Flow-Control-System-Server",
        connect_alert: dict[str, str] | None = None,
    ):
        self.host = host
        self.port = int(port)
        self.armed = bool(armed)
        self.system_name = system_name
        self.connect_alert = connect_alert

        self.config_path = (
            Path(config_path) if config_path else DEFAULT_ACTUATORS_CONFIG
        )
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # Hardware / simulation / recorder subsystem hooks
        self.serial_controller = serial_controller
        self.instrumentation = instrumentation
        self.data_recorder = data_recorder

        # Connected WebSocket clients
        self.clients: set[Any] = set()

        # Actuator definitions and current states
        self.actuators: list[dict[str, Any]] = []
        self.valve_states: dict[str, str] = {}
        self.reload_actuators_config()

        # Built-in CSV recording state (fallback if no data_recorder)
        self._recording_active: bool = False
        self._recording_filename: str | None = None
        self._recording_file: Any = None
        self._recording_start_mono: float = 0.0
        self._last_flush_mono: float = 0.0
        self._flush_interval_sec: float = 2.5

        self._background_tasks: list[asyncio.Task[Any]] = []
        self._stop_event: asyncio.Event | None = None

    # ------------------------------------------------------------------
    # Properties & Configuration
    # ------------------------------------------------------------------
    @property
    def is_recording(self) -> bool:
        if self.data_recorder is not None and hasattr(
            self.data_recorder, "is_recording"
        ):
            return bool(self.data_recorder.is_recording)
        return self._recording_active

    @property
    def recording_filename(self) -> str | None:
        if self.data_recorder is not None and hasattr(
            self.data_recorder, "recording_filename"
        ):
            return self.data_recorder.recording_filename
        return self._recording_filename

    def reload_actuators_config(self) -> None:
        """Load actuator definitions from actuators.json and init states."""
        if not self.config_path.exists():
            logger.warning("Actuators config not found at %s", self.config_path)
            self.actuators = []
        else:
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                self.actuators = list(raw.get("actuators", []))
            except Exception as exc:
                logger.error(
                    "Failed to load actuators config %s: %s",
                    self.config_path,
                    exc,
                )
                self.actuators = []

        new_states: dict[str, str] = {}
        for item in self.actuators:
            vid = item.get("id")
            if vid:
                normal = str(item.get("normal_state", "CLOSED")).upper()
                new_states[vid] = normalize_valve_state(normal)
        self.valve_states = new_states

    def get_actuator_def(self, valve_id: str) -> dict[str, Any] | None:
        """Look up an actuator definition dict by its ID."""
        for item in self.actuators:
            if item.get("id") == valve_id:
                return item
        return None

    # ------------------------------------------------------------------
    # Outbound Messaging & Multi-Client Broadcast
    # ------------------------------------------------------------------
    async def send_to(self, client: Any, payload: dict[str, Any]) -> None:
        """Send a JSON packet to a single WebSocket client."""
        raw = json.dumps(payload)
        try:
            await client.send(raw)
        except Exception as exc:
            logger.debug(
                "Failed sending to client %s: %s",
                self._client_label(client),
                exc,
            )
            self.clients.discard(client)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        """Broadcast a JSON packet to all connected WebSocket clients."""
        if not self.clients:
            return
        raw = json.dumps(payload)

        if _ws_broadcast is not None:
            try:
                _ws_broadcast(self.clients, raw)
                return
            except Exception as exc:
                logger.debug("websockets.broadcast fallback: %s", exc)

        stale: list[Any] = []
        for client in list(self.clients):
            try:
                await client.send(raw)
            except Exception:
                stale.append(client)
        for client in stale:
            self.clients.discard(client)

    def build_init_payload(self) -> dict[str, Any]:
        """Construct the initial handshake (`type: 'init'`) payload."""
        return {
            "type": "init",
            "system": self.system_name,
            "armed": self.armed,
            "recording": self.is_recording,
            "recording_filename": self.recording_filename,
            "actuators": self.actuators,
            "valves": dict(self.valve_states),
        }

    async def send_init(self, client: Any) -> None:
        """Send initial state & actuator config to a newly connected client."""
        await self.send_to(client, self.build_init_payload())
        if self.connect_alert:
            await self.send_alert(
                self.connect_alert.get("level", "INFO"),
                self.connect_alert.get("message", ""),
                client=client,
            )

    async def send_alert(
        self, level: str, message: str, client: Any | None = None
    ) -> None:
        """Send or broadcast a diagnostic toast (`type: 'alert'`)."""
        payload = {
            "type": "alert",
            "level": str(level).upper(),
            "message": str(message),
        }
        if client is not None:
            await self.send_to(client, payload)
        else:
            await self.broadcast(payload)

    async def set_armed(self, armed: bool) -> None:
        """Update server armed interlock state and sync all clients."""
        self.armed = bool(armed)
        state_str = "ARMED" if self.armed else "SAFE"
        logger.info("System interlock state set to %s", state_str)
        await self.broadcast({"type": "armed", "armed": self.armed})

    async def update_valve_state(self, valve: str, state: str) -> None:
        """Normalize, store, and broadcast a single valve state transition."""
        vid = str(valve).strip().upper()
        norm_state = normalize_valve_state(state)
        self.valve_states[vid] = norm_state
        await self.broadcast(
            {
                "type": "valve_state",
                "valve": vid,
                "state": norm_state,
            }
        )

    async def update_valve_summary(
        self, valves_map: dict[str, str], only_changed: bool = False
    ) -> None:
        """
        Update multiple valve states from a `VC,SUMMARY,...` packet
        and broadcast updates for all changed valves (or the full map).
        """
        changed: dict[str, str] = {}
        for raw_vid, raw_state in valves_map.items():
            vid = str(raw_vid).strip().upper()
            norm_state = normalize_valve_state(raw_state)
            if self.valve_states.get(vid) != norm_state or not only_changed:
                changed[vid] = norm_state
            self.valve_states[vid] = norm_state

        if changed:
            await self.broadcast(
                {
                    "type": "valve_state",
                    "valves": dict(self.valve_states),
                }
            )

    async def broadcast_telemetry(self, sample: dict[str, Any]) -> None:
        """Record a telemetry sample (if active) and broadcast to clients."""
        self._write_recording_row(sample)
        await self.broadcast({"type": "telemetry", "data": sample})

    # ------------------------------------------------------------------
    # Inbound Message Router
    # ------------------------------------------------------------------
    async def handle_message(self, client: Any, raw_message: str) -> None:
        """Parse UTF-8 JSON / JSONL frames and dispatch each message."""
        for line in raw_message.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                msg = json.loads(stripped)
            except ValueError as exc:
                logger.warning(
                    "Invalid JSON from %s: %s", self._client_label(client), exc
                )
                continue

            if isinstance(msg, dict):
                await self._dispatch_message(client, msg)

    async def _dispatch_message(self, client: Any, msg: dict[str, Any]) -> None:
        msg_type = str(msg.get("type") or msg.get("identifier") or "").strip().lower()
        cmd = str(msg.get("command") or "").strip().upper()

        if msg_type == "abort" or cmd == "ABORT":
            await self._handle_abort(client, msg)
        elif msg_type == "unabort" or cmd == "UNABORT":
            await self._handle_unabort(client, msg)
        elif msg_type == "summary" or cmd == "SUMMARY":
            await self._handle_summary(client, msg)
        elif msg_type in ("command", "ctrl") or cmd == "CTRL":
            await self._handle_valve_command(client, msg)
        elif msg_type in ("armed", "arm", "safe") or cmd in ("ARM", "SAFE"):
            await self._handle_armed_request(client, msg, msg_type, cmd)
        elif msg_type == "record" or cmd == "RECORD":
            await self._handle_record(client, msg)
        elif msg_type == "remote_data_req":
            await self._handle_remote_data_req(client, msg)
        else:
            logger.warning(
                "Unrecognized message from %s: %s",
                self._client_label(client),
                msg,
            )

    # ------------------------------------------------------------------
    # Command Handlers
    # ------------------------------------------------------------------
    async def _check_actuator_interlocks(
        self, client: Any, valve: str, action: str
    ) -> bool:
        """Validate armed, external, and single-use interlocks."""
        if not self.armed:
            logger.warning(
                "Blocked actuation of %s (%s): system is SAFE.", valve, action
            )
            await self.send_alert(
                "WARN",
                f"Actuation blocked ({valve} -> {action}): System is in SAFE mode.",
                client=client,
            )
            return False

        vdef = self.get_actuator_def(valve)
        if vdef is None:
            return True

        if vdef.get("external", False):
            logger.warning("Blocked actuation of external actuator %s.", valve)
            await self.send_alert(
                "WARN",
                f"Actuation blocked: {valve} is marked as external.",
                client=client,
            )
            return False

        if vdef.get("single_use", False):
            current_state = self.valve_states.get(valve, "")
            if current_state in ("DISCONNECTED", "FIRED"):
                logger.warning(
                    "Blocked expended single-use actuator %s (%s).",
                    valve,
                    current_state,
                )
                await self.send_alert(
                    "WARN",
                    f"Actuation blocked: Single-use device {valve} "
                    f"is already {current_state}.",
                    client=client,
                )
                return False

        return True

    async def _handle_valve_command(self, client: Any, msg: dict[str, Any]) -> None:
        valve = str(msg.get("valve") or "").strip().upper()
        action = str(msg.get("action") or "").strip().upper()

        if not valve or not action:
            await self.send_alert(
                "WARN",
                "Malformed valve command: missing 'valve' or 'action'.",
                client=client,
            )
            return

        if not await self._check_actuator_interlocks(client, valve, action):
            return

        logger.info("Executing command: valve=%s, action=%s", valve, action)

        if self.serial_controller is not None:
            send_fn = getattr(self.serial_controller, "send_valve_command", None)
            if send_fn is not None:
                res = send_fn(valve, action)
                if inspect.isawaitable(res):
                    await res
                return

        await self.send_alert(
            "ERR",
            f"Cannot actuate {valve}: no serial or mock controller attached.",
            client=client,
        )

    async def _handle_abort(self, _client: Any, _msg: dict[str, Any]) -> None:
        """Emergency Abort handler. Always permitted regardless of armed."""
        logger.warning("!!! EMERGENCY ABORT TRIGGERED !!!")
        if self.serial_controller is not None:
            abort_fn = getattr(self.serial_controller, "send_abort", None)
            if abort_fn is not None:
                res = abort_fn()
                if inspect.isawaitable(res):
                    await res
                return

        await self.send_alert(
            "ERR",
            "EMERGENCY ABORT requested, but no controller is attached!",
        )

    async def _handle_unabort(self, _client: Any, _msg: dict[str, Any]) -> None:
        logger.info("Clearing abort lockout (UNABORT)")
        if self.serial_controller is not None:
            unabort_fn = getattr(self.serial_controller, "send_unabort", None)
            if unabort_fn is not None:
                res = unabort_fn()
                if inspect.isawaitable(res):
                    await res
                return

        await self.send_alert("INFO", "Valve Cart abort state cleared.")

    async def _handle_summary(self, _client: Any, _msg: dict[str, Any]) -> None:
        if self.serial_controller is not None:
            summary_fn = getattr(self.serial_controller, "request_summary", None)
            if summary_fn is not None:
                res = summary_fn()
                if inspect.isawaitable(res):
                    await res
                return

        await self.broadcast({"type": "summary", "valves": dict(self.valve_states)})

    async def _handle_armed_request(
        self, _client: Any, msg: dict[str, Any], msg_type: str, cmd: str
    ) -> None:
        if "armed" in msg:
            new_armed = bool(msg["armed"])
        elif msg_type == "arm" or cmd == "ARM":
            new_armed = True
        else:
            new_armed = False
        await self.set_armed(new_armed)

    # ------------------------------------------------------------------
    # Telemetry Recording & Remote Data File Management
    # ------------------------------------------------------------------
    async def _handle_record(self, client: Any, msg: dict[str, Any]) -> None:
        active = bool(msg.get("active", False))
        stamp = datetime.now().strftime("%Y_%m_%d_%H%M%S")
        raw_name = (
            msg.get("filename")
            or msg.get("session_name")
            or msg.get("name")
            or f"telemetry_{stamp}.csv"
        )

        if self.data_recorder is not None and hasattr(
            self.data_recorder, "set_recording"
        ):
            res = self.data_recorder.set_recording(active, raw_name)
            if inspect.isawaitable(res):
                await res
            if not active:
                await self.broadcast(self._build_remote_data_list_payload())
            return

        if active:
            await self._start_recording_session(client, str(raw_name))
        else:
            await self._stop_recording_session()

    async def _start_recording_session(self, client: Any, raw_name: str) -> None:
        filename = sanitize_filename(raw_name)
        if not filename:
            await self.send_alert(
                "ERR",
                f"Invalid recording filename: {raw_name!r}",
                client=client,
            )
            return
        if not filename.lower().endswith((".csv", ".jsonl")):
            filename = f"{filename}.csv"

        if self._recording_active and self._recording_filename == filename:
            logger.info(
                "Recording already active for %s (idempotent re-sync)",
                filename,
            )
            return

        if self._recording_active:
            self._stop_builtin_recording()

        self._start_builtin_recording(filename)
        await self.send_alert("INFO", f"Server telemetry recording started: {filename}")

    async def _stop_recording_session(self) -> None:
        if not self._recording_active:
            return
        finished_name = self._recording_filename
        self._stop_builtin_recording()
        await self.broadcast(self._build_remote_data_list_payload())
        await self.send_alert(
            "INFO", f"Server telemetry recording saved: {finished_name}"
        )

    def _start_builtin_recording(self, filename: str) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        filepath = self.data_dir / filename
        exists_nonempty = filepath.exists() and filepath.stat().st_size > 0
        self._recording_file = open(filepath, "a", encoding="utf-8", buffering=1)
        if not exists_nonempty:
            self._recording_file.write(CSV_UNITS_ROW + "\n")
            self._recording_file.write(CSV_CHANNELS_ROW + "\n")
            self._recording_file.flush()
        self._recording_filename = filename
        self._recording_active = True
        self._recording_start_mono = time.monotonic()
        self._last_flush_mono = self._recording_start_mono
        logger.info("Started on-cart telemetry recording -> %s", filepath)

    def _write_recording_row(self, sample: dict[str, Any]) -> None:
        if self.data_recorder is not None and hasattr(
            self.data_recorder, "write_sample"
        ):
            self.data_recorder.write_sample(sample)
            return

        if not self._recording_active or self._recording_file is None:
            return

        flat = _flatten_sample(sample)
        now_mono = time.monotonic()
        rec_seconds = flat.get("seconds")
        if rec_seconds is None:
            rec_seconds = round(now_mono - self._recording_start_mono, 4)

        row_line = _format_csv_row(flat, float(rec_seconds))
        try:
            self._recording_file.write(row_line)
            if (now_mono - self._last_flush_mono) >= self._flush_interval_sec:
                self._recording_file.flush()
                self._last_flush_mono = now_mono
        except Exception as exc:
            logger.error(
                "Failed writing telemetry row to %s: %s",
                self._recording_filename,
                exc,
            )

    def _stop_builtin_recording(self) -> None:
        if self._recording_file is not None:
            try:
                self._recording_file.flush()
                self._recording_file.close()
            except Exception as exc:
                logger.error("Error closing recording file: %s", exc)
            self._recording_file = None
        if self._recording_active:
            logger.info(
                "Stopped on-cart telemetry recording (%s)",
                self._recording_filename,
            )
        self._recording_active = False
        self._recording_filename = None

    def _list_data_files(self) -> list[str]:
        if not self.data_dir.exists():
            return []
        files = [
            p.name
            for p in self.data_dir.iterdir()
            if p.is_file() and p.suffix.lower() in (".csv", ".jsonl")
        ]
        files.sort(reverse=True)
        return files

    def _build_remote_data_list_payload(self) -> dict[str, Any]:
        return {
            "type": "remote_data_list",
            "files": self._list_data_files(),
        }

    async def _handle_remote_data_req(self, client: Any, msg: dict[str, Any]) -> None:
        """Handle client `remote_data_req` operations (list/get/rename/del)."""
        if self.data_recorder is not None and hasattr(
            self.data_recorder, "handle_remote_request"
        ):
            res = self.data_recorder.handle_remote_request(client, msg)
            if inspect.isawaitable(res):
                await res
            return

        operation = str(msg.get("operation") or "").strip().lower()
        if operation == "list":
            await self.send_to(client, self._build_remote_data_list_payload())
        elif operation == "get":
            await self._remote_get(client, msg)
        elif operation in ("put", "upload"):
            await self._remote_put(client, msg)
        elif operation == "rename":
            await self._remote_rename(client, msg)
        elif operation == "delete":
            await self._remote_delete(client, msg)
        else:
            await self.send_alert(
                "WARN",
                f"Unknown remote_data_req operation: {operation!r}",
                client=client,
            )

    async def _remote_get(self, client: Any, msg: dict[str, Any]) -> None:
        mode = str(msg.get("mode") or "load").strip()
        raw_filename = msg.get("filename")
        if mode == "pull_recording" and not raw_filename:
            raw_filename = self.recording_filename

        filename = sanitize_filename(str(raw_filename or ""))
        if not filename:
            await self.send_alert(
                "ERR",
                "Remote data request failed: invalid filename.",
                client=client,
            )
            return

        filepath = self.data_dir / filename
        if self._recording_active and self._recording_filename == filename:
            self._flush_active_recording()

        if not filepath.exists() or not filepath.is_file():
            await self.send_alert(
                "ERR",
                f"Remote data file not found: {filename}",
                client=client,
            )
            return

        try:
            content = filepath.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.error("Failed reading remote file %s: %s", filepath, exc)
            await self.send_alert(
                "ERR",
                f"Failed to read remote file {filename}: {exc}",
                client=client,
            )
            return

        await self.send_to(
            client,
            {
                "type": "remote_data_content",
                "filename": filename,
                "mode": mode,
                "content": content,
            },
        )

    def _flush_active_recording(self) -> None:
        if self._recording_file is not None:
            try:
                self._recording_file.flush()
            except Exception:
                pass

    async def _remote_put(self, client: Any, msg: dict[str, Any]) -> None:
        """Store an uploaded CSV/JSONL telemetry file in self.data_dir."""
        filename = sanitize_filename(str(msg.get("filename") or ""))
        if not filename:
            await self.send_alert(
                "ERR",
                "Upload failed: invalid filename.",
                client=client,
            )
            return

        if Path(filename).suffix == "":
            filename = f"{filename}.csv"
        elif Path(filename).suffix.lower() not in (".csv", ".jsonl"):
            await self.send_alert(
                "ERR",
                f"Upload failed: unsupported file type for {filename}.",
                client=client,
            )
            return

        content = msg.get("content")
        if not isinstance(content, str):
            await self.send_alert(
                "ERR",
                "Upload failed: missing or invalid file content.",
                client=client,
            )
            return

        if self._recording_active and self._recording_filename == filename:
            await self.send_alert(
                "ERR",
                f"Upload failed: {filename} is currently being recorded.",
                client=client,
            )
            return

        target = self.data_dir / filename
        overwrote = target.exists()
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            logger.info(
                "Stored uploaded remote file: %s (%d bytes, overwrote=%s)",
                filename,
                len(content.encode("utf-8")),
                overwrote,
            )
        except Exception as exc:
            logger.error("Failed storing uploaded file %s: %s", target, exc)
            await self.send_alert(
                "ERR",
                f"Failed to store {filename} on valve cart: {exc}",
                client=client,
            )
            return

        await self.broadcast(self._build_remote_data_list_payload())
        suffix = " (overwrote existing)" if overwrote else ""
        await self.send_alert(
            "INFO",
            f'Stored "{filename}" on the valve cart{suffix}.',
            client=client,
        )

    async def _remote_rename(self, client: Any, msg: dict[str, Any]) -> None:
        old_name = sanitize_filename(str(msg.get("old_name") or ""))
        new_name = sanitize_filename(str(msg.get("new_name") or ""))
        if not old_name or not new_name:
            await self.send_alert(
                "ERR",
                "Rename failed: invalid source or target filename.",
                client=client,
            )
            return

        old_path = self.data_dir / old_name
        new_path = self.data_dir / new_name
        if not old_path.exists():
            await self.send_alert(
                "ERR",
                f"Rename failed: {old_name} does not exist.",
                client=client,
            )
            return
        if new_path.exists() and old_path != new_path:
            await self.send_alert(
                "ERR",
                f"Rename failed: {new_name} already exists.",
                client=client,
            )
            return

        try:
            old_path.rename(new_path)
            if self._recording_filename == old_name:
                self._recording_filename = new_name
            logger.info("Renamed remote file %s -> %s", old_name, new_name)
        except Exception as exc:
            logger.error("Rename failed %s -> %s: %s", old_name, new_name, exc)
            await self.send_alert("ERR", f"Rename failed: {exc}", client=client)
            return

        await self.broadcast(self._build_remote_data_list_payload())

    async def _remote_delete(self, client: Any, msg: dict[str, Any]) -> None:
        filename = sanitize_filename(str(msg.get("filename") or ""))
        if not filename:
            await self.send_alert(
                "WARN", "Delete ignored: invalid filename.", client=client
            )
            return

        target = self.data_dir / filename
        try:
            if target.exists() and target.is_file():
                target.unlink()
                logger.info("Deleted remote data file: %s", filename)
            else:
                logger.debug("Delete skipped for missing file %s", filename)
        except Exception as exc:
            logger.error("Failed deleting %s: %s", filename, exc)
            await self.send_alert(
                "WARN", f"Could not delete {filename}: {exc}", client=client
            )
            return

        await self.broadcast(self._build_remote_data_list_payload())

    # ------------------------------------------------------------------
    # Client Connection Lifecycle
    # ------------------------------------------------------------------
    @staticmethod
    def _client_label(client: Any) -> str:
        addr = getattr(client, "remote_address", None)
        return str(addr) if addr else repr(client)

    @staticmethod
    def _extract_request_path(client: Any, legacy_path: str | None = None) -> str:
        if legacy_path:
            return legacy_path
        req = getattr(client, "request", None)
        if req is not None and hasattr(req, "path"):
            return str(req.path)
        return str(getattr(client, "path", "/ws"))

    async def _ws_client_handler(
        self, client: Any, legacy_path: str | None = None
    ) -> None:
        """Handle an individual WebSocket client lifecycle."""
        path = self._extract_request_path(client, legacy_path)
        label = self._client_label(client)
        self.clients.add(client)
        logger.info(
            "Client connected from %s on path '%s' (total clients: %d)",
            label,
            path,
            len(self.clients),
        )

        try:
            await self.send_init(client)
            async for raw_message in client:
                if isinstance(raw_message, bytes):
                    raw_message = raw_message.decode("utf-8", errors="replace")
                if raw_message:
                    await self.handle_message(client, raw_message)
        except _WSConnectionClosed:
            pass
        except Exception as exc:
            logger.error("Unexpected error in client handler (%s): %s", label, exc)
        finally:
            self.clients.discard(client)
            logger.info(
                "Client disconnected: %s (remaining clients: %d)",
                label,
                len(self.clients),
            )

    # ------------------------------------------------------------------
    # Interactive Terminal Loop (Optional for local debugging)
    # ------------------------------------------------------------------
    async def _process_stdin_command(self, line: str) -> None:
        parts = line.strip().split()
        if not parts:
            return
        cmd = parts[0].lower()
        if cmd == "arm":
            await self.set_armed(True)
        elif cmd == "safe":
            await self.set_armed(False)
        elif cmd == "abort":
            await self._handle_abort(None, {"type": "abort"})
        elif cmd == "reset":
            self.reload_actuators_config()
            logger.info("Reloaded actuators.json and reset all valve states.")
            await self.broadcast(self.build_init_payload())
        elif cmd == "state" and len(parts) >= 3:
            await self.update_valve_state(parts[1], parts[2])
        elif cmd == "alert" and len(parts) >= 3:
            await self.send_alert(parts[1], " ".join(parts[2:]))
        else:
            logger.info(
                "Commands: arm | safe | abort | reset | "
                "state <ID> <STATE> | alert <LVL> <MSG>"
            )

    async def _interactive_stdin_loop(self) -> None:
        """Optional CLI operator console when running interactively."""
        if not sys.stdin or not sys.stdin.isatty():
            return

        logger.info(
            "Interactive console active. "
            "Commands: arm | safe | abort | reset | state <ID> <STATE>"
        )
        loop = asyncio.get_running_loop()
        while True:
            try:
                line = await loop.run_in_executor(None, sys.stdin.readline)
            except Exception:
                break
            if not line:
                break
            await self._process_stdin_command(line)

    # ------------------------------------------------------------------
    # Server Start / Stop Lifecycle
    # ------------------------------------------------------------------
    def request_shutdown(self) -> None:
        """Signal the server to shut down cleanly."""
        if self._stop_event is not None and not self._stop_event.is_set():
            self._stop_event.set()

    async def serve_forever(self, enable_stdin: bool = True) -> None:
        """Start the WebSocket server and run until stopped."""
        self._stop_event = asyncio.Event()

        if enable_stdin and sys.stdin and sys.stdin.isatty():
            self._background_tasks.append(
                asyncio.create_task(self._interactive_stdin_loop())
            )

        logger.info(
            "Starting FlowControlWSServer on ws://%s:%d/ws (armed=%s, actuators=%d)",
            self.host,
            self.port,
            self.armed,
            len(self.actuators),
        )

        try:
            async with _ws_serve(
                self._ws_client_handler,
                self.host,
                self.port,
                max_size=16 * 1024 * 1024,
            ):
                await self._stop_event.wait()
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Cancel background tasks, close recordings and client sockets."""
        for task in self._background_tasks:
            if not task.done():
                task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        if self._recording_active:
            self._stop_builtin_recording()

        for client in list(self.clients):
            try:
                await client.close()
            except Exception:
                pass
        self.clients.clear()
        logger.info("FlowControlWSServer shut down cleanly.")
