"""SerialController: talks to the Controls Arduino (Controls.ino) over USB serial.

Protocol (actuation-spec.md):
  Server -> Arduino:  MCC,CONNECT / MCC,SUMMARY / MCC,CTRL,<VALVE>,<ACTION> / MCC,ABORT / MCC,UNABORT
  Arduino -> Server:  VC,SUMMARY,... / VC,STATUS,... / VC,ERROR,... / VC,CF,...

Commands are queued on a FIFO asyncio.Queue and drained by a writer task.
send_abort() is the one exception: it drains the queue and writes immediately.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

import serial  # pyserial

log = logging.getLogger("serial_controller")

# UI-friendly actions -> the only two actions Controls.ino understands
ACTION_MAP = {
    "OPEN": "OPEN",
    "FILL": "OPEN",
    "CLOSE": "CLOSE",
    "VENT": "CLOSE",
    "FIRE": "OPEN",       # igniter opens via OPEN; see actuators.json
    "DISCONNECT": "CLOSE",
}

RECONNECT_DELAY = 2.0  # seconds between serial-port open attempts


class SerialController:
    def __init__(self, ws_server, port: str, baudrate: int = 115200):
        self.ws_server = ws_server
        self.port = port
        self.baudrate = baudrate

        self._serial: Optional[serial.SerialBase] = None
        self._connected = asyncio.Event()
        self._stopping = asyncio.Event()

        # FIFO command queue (NOT LifoQueue -- see actuation-spec.md section 5.2)
        self._queue: asyncio.Queue[str] = asyncio.Queue()

        # Serializes raw writes so the abort bypass never interleaves with a
        # queued command mid-write.
        self._write_lock = asyncio.Lock()

        self._tasks: list[asyncio.Task] = []
        self._reader_thread: Optional[threading.Thread] = None
        self._rx_queue: asyncio.Queue[str] = asyncio.Queue()

    # ------------------------------------------------------------------ lifecycle

    def start(self):
        self._tasks.append(asyncio.create_task(self._connect_loop(), name="serial-connect"))
        self._tasks.append(asyncio.create_task(self._writer_loop(), name="serial-writer"))
        self._tasks.append(asyncio.create_task(self._dispatch_loop(), name="serial-dispatch"))

    async def stop(self):
        self._stopping.set()
        for t in self._tasks:
            t.cancel()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=1.0)
        if self._serial is not None and self._serial.is_open:
            self._serial.close()

    # ------------------------------------------------------------------ commands

    async def send_valve_command(self, valve: str, action: str):
        """Queue MCC,CTRL,<VALVE>,<ACTION>. One valve per line (Controls.ino
        setTarget() bug with the N2OF empty index block, spec 5.3)."""
        valve = valve.strip().upper()
        action = ACTION_MAP.get(action.strip().upper())
        if action is None:
            log.warning("Unknown action %r for valve %r; ignored", action, valve)
            return
        await self._queue.put(f"MCC,CTRL,{valve},{action}")

    async def send_abort(self):
        """PRIORITY BYPASS (spec 2.1): clear every pending command, then write
        MCC,ABORT directly to serial without waiting in the FIFO queue."""
        # 1. Drain the FIFO of pending commands so nothing executes after/with
        #    the abort. Items already pulled by the writer (at most one,
        #    in-flight) cannot be recalled.
        drained = 0
        while True:
            try:
                self._queue.get_nowait()
                self._queue.task_done()
                drained += 1
            except asyncio.QueueEmpty:
                break
        if drained:
            log.info("send_abort: drained %d pending command(s)", drained)

        # 2. Write immediately. The write lock means we either preempt the
        #    writer before it starts a line or wait for the single in-flight
        #    line to finish, then jump the queue.
        await self._write_line("MCC,ABORT")

        # Firmware replies VC,STATUS,ABORTED; the dispatch loop then calls
        # ws_server.send_alert("ERR", ...) and ws_server.set_armed(False).

    async def send_unabort(self):
        await self._queue.put("MCC,UNABORT")

    async def request_summary(self):
        await self._queue.put("MCC,SUMMARY")

    # ------------------------------------------------------------------ serial I/O

    async def _connect_loop(self):
        """Open the port, handshake, and survive USB hotplug (spec 4.1)."""
        while not self._stopping.is_set():
            try:
                s = serial.Serial(self.port, self.baudrate, timeout=1.0)
            except (serial.SerialException, OSError) as e:
                log.warning("Serial open failed on %s: %s", self.port, e)
                if not self._connected.is_set():
                    await self.ws_server.send_alert(
                        "WARN", "Arduino serial disconnected; retrying..."
                    )
                await asyncio.sleep(RECONNECT_DELAY)
                continue

            self._serial = s
            self._connected.set()
            log.info("Serial opened on %s @ %d", self.port, self.baudrate)

            # Dedicated reader thread: pyserial readline() blocks, so it must
            # not run on the event loop (spec 4.2).
            self._reader_thread = threading.Thread(
                target=self._reader_thread_fn, daemon=True, name="serial-reader"
            )
            self._reader_thread.start()

            # Handshake + state resync (spec 3.1)
            await self._write_line("MCC,CONNECT")
            return  # connected; writer/dispatch tasks take over

    def _reader_thread_fn(self):
        """Push \\n-terminated lines from the Arduino into _rx_queue."""
        s = self._serial
        loop = asyncio.get_event_loop()
        while not self._stopping.is_set() and s is not None and s.is_open:
            try:
                raw = s.readline()
            except (serial.SerialException, OSError):
                loop.call_soon_threadsafe(self._handle_disconnect)
                return
            if not raw:
                continue
            line = raw.decode("ascii", errors="replace").strip()
            if line:
                loop.call_soon_threadsafe(self._rx_queue.put_nowait, line)

    def _handle_disconnect(self):
        if self._stopping.is_set():
            return
        log.warning("Arduino serial line dropped (disconnect/reset)")
        self._connected.clear()
        if self._serial is not None and self._serial.is_open:
            try:
                self._serial.close()
            except OSError:
                pass
        asyncio.create_task(self._reconnect_after_drop())

    async def _reconnect_after_drop(self):
        await self.ws_server.send_alert("WARN", "Arduino serial disconnected; retrying...")
        self._tasks.append(asyncio.create_task(self._connect_loop(), name="serial-reconnect"))

    async def _write_line(self, line: str):
        """Serialize raw writes through the lock; run blocking write off-loop."""
        if not line.endswith("\n"):
            line += "\n"
        async with self._write_lock:
            s = self._serial
            if s is None or not s.is_open or not self._connected.is_set():
                log.warning("Write skipped (disconnected): %s", line.strip())
                return
            try:
                await asyncio.to_thread(s.write, line.encode("ascii"))
                await asyncio.to_thread(s.flush)
            except (serial.SerialException, OSError) as e:
                log.error("Serial write failed: %s", e)
                self._handle_disconnect()

    async def _writer_loop(self):
        """Drain the FIFO queue one command at a time."""
        while not self._stopping.is_set():
            cmd = await self._queue.get()
            try:
                await self._connected.wait()
                await self._write_line(cmd)
            finally:
                self._queue.task_done()

    # ------------------------------------------------------------------ inbound

    async def _dispatch_loop(self):
        while not self._stopping.is_set():
            line = await self._rx_queue.get()
            try:
                await self._handle_line(line)
            except Exception:
                log.exception("Failed handling serial line: %r", line)

    async def _handle_line(self, line: str):
        parts = [p.strip().upper() for p in line.split(",")]
        if len(parts) < 2 or parts[0] != "VC":
            log.debug("Ignoring non-VC line: %r", line)
            return

        kind = parts[1]

        if kind == "SUMMARY":
            # Parse ALL pairs (legacy early-return bug, spec 5.1)
            valves_map = {
                parts[i]: parts[i + 1]
                for i in range(2, len(parts) - 1, 2)
            }
            await self.ws_server.update_valve_summary(valves_map, only_changed=True)
            return

        if kind == "STATUS":
            status = parts[2] if len(parts) > 2 else ""
            if status == "ESTABLISH":
                log.info("Arduino handshake established")
            elif status == "STARTUP":
                log.info("Arduino booted; sending MCC,CONNECT to resync")
                await self._write_line("MCC,CONNECT")
            elif status == "ABORTED":
                await self.ws_server.send_alert(
                    "ERR", "Hardware Emergency Abort triggered on Valve Cart!"
                )
                await self.ws_server.set_armed(False)
            elif status == "CANCELLED ABORT":
                await self.ws_server.send_alert("INFO", "Valve Cart abort state cleared.")
            return

        if kind == "ERROR":
            if "UNKOWNVALVE" in line.upper():
                await self.ws_server.send_alert(
                    "WARN", f"Arduino Unknown Valve Error: {line}"
                )
            else:
                await self.ws_server.send_alert(
                    "WARN", f"Arduino Serial Error: {line}"
                )
            return

        if kind == "CF":
            log.info("Arduino: %s", line)
            return

        log.debug("Unhandled VC frame: %r", line)
