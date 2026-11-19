#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Unified FastAPI + WebSocket Server (`ws_server.py`) for
Flow-Control-System-Server.

Provides:
  - `WS /ws`: Real-time JSON frames (`init` including `channels` from
    `sensors.json`, decimated `telemetry`, `valve_state`, `summary`, `armed`,
    `alert`, and control/abort/unabort/summary/record dispatch).
  - HTTP `/api/data/...`: Streaming log list, download, upload, rename, and
    delete backed by `DataRecorder`.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any

try:
    import uvicorn  # type: ignore
    from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect  # type: ignore
    from fastapi.responses import FileResponse  # type: ignore

    HAS_FASTAPI = True
except ImportError:  # pragma: no cover - optional in lightweight local envs
    uvicorn = None  # type: ignore[assignment]
    FastAPI = None  # type: ignore[assignment,misc]
    HTTPException = Exception  # type: ignore[assignment,misc]
    Request = Any  # type: ignore[assignment,misc]
    WebSocket = Any  # type: ignore[assignment,misc]
    WebSocketDisconnect = ConnectionError  # type: ignore[assignment,misc]
    FileResponse = None  # type: ignore[assignment,misc]
    HAS_FASTAPI = False

try:
    from websockets.asyncio.server import serve as _ws_serve  # type: ignore
    from websockets.exceptions import ConnectionClosed as _WSConnectionClosed  # type: ignore
except ImportError:  # pragma: no cover - fallback when fastapi/ws absent
    try:
        import websockets  # type: ignore
        from websockets.exceptions import ConnectionClosed as _WSConnectionClosed  # type: ignore

        _ws_serve = websockets.serve
    except ImportError:
        _ws_serve = None  # type: ignore[assignment]
        _WSConnectionClosed = ConnectionError  # type: ignore[assignment,misc]

from data_recorder import (
    DataRecorder,
    sanitize_filename,
    sanitize_telemetry_for_json,
)
from sensors_config import (
    DEFAULT_SENSORS_CONFIG,
    SensorsConfig,
    load_sensors_config,
)

logger = logging.getLogger("fcs.ws_server")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ACTUATORS_CONFIG = REPO_ROOT / "contents" / "config" / "actuators.json"
DEFAULT_DATA_DIR = REPO_ROOT / "data"

_DEFAULT_SENSORS = load_sensors_config(DEFAULT_SENSORS_CONFIG)
CSV_CHANNELS: tuple[str, ...] = _DEFAULT_SENSORS.csv_channels()
CSV_UNITS_ROW: str = _DEFAULT_SENSORS.csv_units_row()
CSV_CHANNELS_ROW: str = _DEFAULT_SENSORS.csv_channels_row()

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


def _timestamp_filename() -> str:
    from datetime import datetime

    return f"telemetry_{datetime.now().strftime('%Y_%m_%d_%H%M%S')}.csv"


class FlowControlWSServer:
    """
    Unified FastAPI + WebSocket server for the Valve Cart Flow Control System.
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
        sensors_config_path: Path | str | None = None,
        sensors_config: SensorsConfig | None = None,
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
        self.sensors_config = sensors_config or load_sensors_config(
            sensors_config_path or DEFAULT_SENSORS_CONFIG
        )

        self.serial_controller = serial_controller
        self.instrumentation = instrumentation
        self.data_recorder = data_recorder or DataRecorder(
            self.data_dir, sensors_config=self.sensors_config
        )

        self.clients: set[Any] = set()
        self.actuators: list[dict[str, Any]] = []
        self.valve_states: dict[str, str] = {}
        self.reload_actuators_config()

        self._background_tasks: list[asyncio.Task[Any]] = []
        self._stop_event: asyncio.Event | None = None

    # ------------------------------------------------------------------
    # Properties & Configuration
    # ------------------------------------------------------------------
    @property
    def is_recording(self) -> bool:
        return bool(getattr(self.data_recorder, "is_recording", False))

    @property
    def recording_filename(self) -> str | None:
        return getattr(self.data_recorder, "recording_filename", None)

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
    @staticmethod
    async def _send_raw(client: Any, raw: str) -> None:
        if hasattr(client, "send_text"):
            await client.send_text(raw)
        else:
            await client.send(raw)

    async def send_to(self, client: Any, payload: dict[str, Any]) -> None:
        """Send a JSON packet to a single WebSocket client."""
        raw = json.dumps(payload, allow_nan=False)
        try:
            await self._send_raw(client, raw)
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
        raw = json.dumps(payload, allow_nan=False)
        stale: list[Any] = []
        for client in list(self.clients):
            try:
                await self._send_raw(client, raw)
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
            "channels": self.sensors_config.ws_channels_metadata(),
            "valves": dict(self.valve_states),
        }

    async def send_init(self, client: Any) -> None:
        """Send initial state, actuators, and sensor channel config to a client."""
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

    def record_telemetry(self, sample: dict[str, Any]) -> None:
        """Write one telemetry sample to disk when recording is active."""
        if hasattr(self.data_recorder, "write_sample"):
            self.data_recorder.write_sample(sample)

    async def broadcast_telemetry(
        self, sample: dict[str, Any], *, record: bool = True
    ) -> None:
        """Optionally record a telemetry sample and broadcast NaN-safe JSON."""
        if record:
            self.record_telemetry(sample)
        clean_sample = sanitize_telemetry_for_json(sample)
        await self.broadcast({"type": "telemetry", "data": clean_sample})

    # ------------------------------------------------------------------
    # Inbound WebSocket Message Router
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
    # Command Handlers & Interlocks
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
    # Recording & HTTP /api/data + Legacy WS File Management
    # ------------------------------------------------------------------
    def _build_remote_data_list_payload(self) -> dict[str, Any]:
        return {
            "type": "remote_data_list",
            "files": self.data_recorder.list_files(),
        }

    def api_list_data_files(self) -> dict[str, Any]:
        """Return HTTP `/api/data` directory listing payload."""
        return {
            "files": self.data_recorder.list_files(),
            "recording": self.is_recording,
            "recording_filename": self.recording_filename,
        }

    def api_resolve_download(self, filename: str, mode: str = "load") -> Path:
        """Resolve a downloadable file path inside `data_dir`."""
        return self.data_recorder.resolve_download_path(filename, mode=mode)

    async def api_upload_data_chunks(
        self, filename: str, chunks: Iterable[bytes]
    ) -> dict[str, Any]:
        """Save uploaded byte chunks and broadcast the updated file list."""
        saved_name, overwrote, total_bytes = self.data_recorder.save_upload_chunks(
            filename, chunks
        )
        await self.broadcast(self._build_remote_data_list_payload())
        return {
            "status": "ok",
            "filename": saved_name,
            "overwrote": overwrote,
            "bytes": total_bytes,
        }

    async def api_upload_data_file(
        self, filename: str, content: bytes | str
    ) -> dict[str, Any]:
        """Save uploaded content and broadcast the updated file list."""
        raw_bytes = content.encode("utf-8") if isinstance(content, str) else content
        return await self.api_upload_data_chunks(filename, (raw_bytes,))

    async def api_rename_data_file(
        self, filename: str, new_name: str
    ) -> dict[str, Any]:
        """Rename a telemetry log and broadcast the updated file list."""
        old_clean, new_clean = self.data_recorder.rename_file(filename, new_name)
        await self.broadcast(self._build_remote_data_list_payload())
        return {
            "status": "ok",
            "old_name": old_clean,
            "new_name": new_clean,
        }

    async def api_delete_data_file(self, filename: str) -> dict[str, Any]:
        """Delete a telemetry log and broadcast the updated file list."""
        clean_name = self.data_recorder.delete_file(filename)
        await self.broadcast(self._build_remote_data_list_payload())
        return {"status": "ok", "filename": clean_name}

    async def _handle_record(self, client: Any, msg: dict[str, Any]) -> None:
        active = bool(msg.get("active", False))
        raw_name = (
            msg.get("filename")
            or msg.get("session_name")
            or msg.get("name")
            or _timestamp_filename()
        )

        if hasattr(self.data_recorder, "set_recording"):
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
        was_same = (
            self.is_recording and sanitize_filename(raw_name) == self.recording_filename
        )
        try:
            filename = self.data_recorder.start_recording(raw_name)
        except ValueError:
            await self.send_alert(
                "ERR",
                f"Invalid recording filename: {raw_name!r}",
                client=client,
            )
            return
        if not was_same:
            await self.send_alert(
                "INFO", f"Server telemetry recording started: {filename}"
            )

    async def _stop_recording_session(self) -> None:
        finished_name = self.data_recorder.stop_recording()
        if finished_name is None:
            return
        await self.broadcast(self._build_remote_data_list_payload())
        await self.send_alert(
            "INFO", f"Server telemetry recording saved: {finished_name}"
        )

    async def _handle_remote_data_req(self, client: Any, msg: dict[str, Any]) -> None:
        """Handle legacy WS `remote_data_req` operations (list/get/put/rename/del)."""
        if hasattr(self.data_recorder, "handle_remote_request"):
            res = self.data_recorder.handle_remote_request(client, msg)
            if inspect.isawaitable(res):
                await res
            return

        operation = str(msg.get("operation") or "").strip().lower()
        handlers = {
            "list": self._ws_remote_list,
            "get": self._remote_get,
            "put": self._remote_put,
            "upload": self._remote_put,
            "rename": self._remote_rename,
            "delete": self._remote_delete,
        }
        handler = handlers.get(operation)
        if handler is None:
            await self.send_alert(
                "WARN",
                f"Unknown remote_data_req operation: {operation!r}",
                client=client,
            )
            return
        await handler(client, msg)

    async def _ws_remote_list(self, client: Any, _msg: dict[str, Any]) -> None:
        await self.send_to(client, self._build_remote_data_list_payload())

    async def _remote_get(self, client: Any, msg: dict[str, Any]) -> None:
        mode = str(msg.get("mode") or "load").strip()
        raw_filename = msg.get("filename")
        try:
            filepath = self.api_resolve_download(str(raw_filename or ""), mode=mode)
            content = filepath.read_text(encoding="utf-8", errors="replace")
        except ValueError:
            await self.send_alert(
                "ERR", "Remote data request failed: invalid filename.", client=client
            )
            return
        except FileNotFoundError:
            await self.send_alert(
                "ERR", f"Remote data file not found: {raw_filename}", client=client
            )
            return
        except Exception as exc:
            await self.send_alert(
                "ERR",
                f"Failed to read remote file {raw_filename}: {exc}",
                client=client,
            )
            return

        await self.send_to(
            client,
            {
                "type": "remote_data_content",
                "filename": filepath.name,
                "mode": mode,
                "content": content,
            },
        )

    async def _remote_put(self, client: Any, msg: dict[str, Any]) -> None:
        raw_name = str(msg.get("filename") or "")
        content = msg.get("content")
        if not isinstance(content, str):
            await self.send_alert(
                "ERR", "Upload failed: missing or invalid file content.", client=client
            )
            return
        try:
            result = await self.api_upload_data_file(raw_name, content)
        except (ValueError, PermissionError, OSError) as exc:
            await self.send_alert("ERR", f"Upload failed: {exc}", client=client)
            return

        suffix = " (overwrote existing)" if result["overwrote"] else ""
        await self.send_alert(
            "INFO",
            f'Stored "{result["filename"]}" on the valve cart{suffix}.',
            client=client,
        )

    async def _remote_rename(self, client: Any, msg: dict[str, Any]) -> None:
        old_raw = str(msg.get("old_name") or "")
        new_raw = str(msg.get("new_name") or "")
        try:
            await self.api_rename_data_file(old_raw, new_raw)
        except (ValueError, FileNotFoundError, FileExistsError, OSError) as exc:
            await self.send_alert("ERR", f"Rename failed: {exc}", client=client)

    async def _remote_delete(self, client: Any, msg: dict[str, Any]) -> None:
        raw_name = str(msg.get("filename") or "")
        try:
            await self.api_delete_data_file(raw_name)
        except FileNotFoundError:
            logger.debug("Delete skipped for missing file %s", raw_name)
        except (ValueError, PermissionError, OSError) as exc:
            await self.send_alert(
                "WARN", f"Could not delete {raw_name}: {exc}", client=client
            )

    # ------------------------------------------------------------------
    # FastAPI Application & Routes
    # ------------------------------------------------------------------
    async def _http_download_data_file(self, filename: str) -> Any:
        try:
            filepath = self.api_resolve_download(filename)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(filepath, filename=filepath.name, media_type="text/csv")

    async def _http_upload_data_file(
        self, filename: str, request: Request
    ) -> dict[str, Any]:
        chunks = [chunk async for chunk in request.stream()]
        try:
            return await self.api_upload_data_chunks(filename, chunks)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def _http_rename_data_file(
        self, filename: str, request: Request
    ) -> dict[str, Any]:
        try:
            body = await request.json()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid JSON body") from exc
        new_name = body.get("new_name") if isinstance(body, dict) else None
        if not isinstance(new_name, str) or not new_name.strip():
            raise HTTPException(status_code=400, detail="Missing 'new_name'")
        try:
            return await self.api_rename_data_file(filename, new_name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except FileExistsError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def _http_delete_data_file(self, filename: str) -> dict[str, Any]:
        try:
            return await self.api_delete_data_file(filename)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _register_http_routes(self, app: Any) -> None:
        server = self

        @app.get("/api/data")
        async def list_data_files() -> dict[str, Any]:
            return server.api_list_data_files()

        @app.get("/api/data/{filename}")
        async def download_data_file(filename: str) -> Any:
            return await server._http_download_data_file(filename)

        @app.put("/api/data/{filename}")
        async def upload_data_file(filename: str, request: Request) -> dict[str, Any]:
            return await server._http_upload_data_file(filename, request)

        @app.post("/api/data/{filename}/rename")
        async def rename_data_file(filename: str, request: Request) -> dict[str, Any]:
            return await server._http_rename_data_file(filename, request)

        @app.delete("/api/data/{filename}")
        async def delete_data_file(filename: str) -> dict[str, Any]:
            return await server._http_delete_data_file(filename)

    async def _handle_fastapi_ws(self, websocket: WebSocket) -> None:
        await websocket.accept()
        label = self._client_label(websocket)
        self.clients.add(websocket)
        logger.info(
            "Client connected from %s on path '/ws' (total clients: %d)",
            label,
            len(self.clients),
        )
        try:
            await self.send_init(websocket)
            while True:
                raw_message = await websocket.receive_text()
                if raw_message:
                    await self.handle_message(websocket, raw_message)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.error("Unexpected error in client handler (%s): %s", label, exc)
        finally:
            self.clients.discard(websocket)
            logger.info(
                "Client disconnected: %s (remaining clients: %d)",
                label,
                len(self.clients),
            )

    def _register_ws_routes(self, app: Any) -> None:
        server = self

        @app.websocket("/ws")
        async def ws_endpoint(websocket: WebSocket) -> None:
            await server._handle_fastapi_ws(websocket)

    def create_app(self) -> Any:
        """Build the FastAPI application serving `/ws` and `/api/data/...`."""
        if not HAS_FASTAPI or FastAPI is None:
            raise RuntimeError("fastapi and uvicorn are required for create_app().")

        app = FastAPI(title="Flow Control System Server", version="0.2.0")
        self._register_http_routes(app)
        self._register_ws_routes(app)
        return app

    # ------------------------------------------------------------------
    # Legacy WebSocket Connection Lifecycle Fallback
    # ------------------------------------------------------------------
    @staticmethod
    def _client_label(client: Any) -> str:
        addr = getattr(client, "remote_address", None) or getattr(
            client, "client", None
        )
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
        """Handle an individual legacy `websockets` client lifecycle."""
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

    async def _serve_uvicorn(self) -> None:
        assert self._stop_event is not None and uvicorn is not None
        config = uvicorn.Config(
            self.create_app(),
            host=self.host,
            port=self.port,
            log_level="info",
            ws_max_size=16 * 1024 * 1024,
        )
        uv_server = uvicorn.Server(config)
        uv_server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
        serve_task = asyncio.create_task(uv_server.serve())
        stop_task = asyncio.create_task(self._stop_event.wait())
        done, _ = await asyncio.wait(
            {serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED
        )
        if stop_task in done:
            uv_server.should_exit = True
            await serve_task
        else:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)

    async def serve_forever(self, enable_stdin: bool = True) -> None:
        """Start the HTTP/WebSocket server and run until stopped."""
        self._stop_event = asyncio.Event()
        if enable_stdin and sys.stdin and sys.stdin.isatty():
            self._background_tasks.append(
                asyncio.create_task(self._interactive_stdin_loop())
            )

        logger.info(
            "Starting FlowControlWSServer on http://%s:%d (/ws, /api/data) "
            "(armed=%s, actuators=%d, sensors=%d)",
            self.host,
            self.port,
            self.armed,
            len(self.actuators),
            len(self.sensors_config.sensors),
        )

        try:
            if HAS_FASTAPI and uvicorn is not None:
                await self._serve_uvicorn()
            elif _ws_serve is not None:
                async with _ws_serve(
                    self._ws_client_handler,
                    self.host,
                    self.port,
                    max_size=16 * 1024 * 1024,
                ):
                    await self._stop_event.wait()
            else:
                raise RuntimeError(
                    "Neither uvicorn/fastapi nor websockets is installed."
                )
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        """Cancel background tasks, close recordings, and close client sockets."""
        for task in self._background_tasks:
            if not task.done():
                task.cancel()
        if self._background_tasks:
            await asyncio.gather(*self._background_tasks, return_exceptions=True)
        self._background_tasks.clear()

        if self.is_recording and hasattr(self.data_recorder, "stop_recording"):
            self.data_recorder.stop_recording()

        for client in list(self.clients):
            try:
                await client.close()
            except Exception:
                pass
        self.clients.clear()
        logger.info("FlowControlWSServer shut down cleanly.")
