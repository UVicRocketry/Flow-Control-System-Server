#!/usr/bin/env python3
"""
Mock WebSocket Server for Flow-Control-System-Server.
Loads ../config/actuators.json and serves ws://127.0.0.1:8080/ws using only the
Python standard library (zero external dependencies required).

Interactive terminal commands while running:
  arm                  -> Set system ARMED (true) and push to clients
  safe                 -> Set system SAFE (false) and push to clients
  reset                -> Reset all actuators to normal_state from actuators.json
  state <ID> <STATE>   -> Push a live state change (e.g. `state RTV FILL`)
"""

import argparse
import asyncio
import base64
import hashlib
import json
import struct
import sys
from pathlib import Path

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
REPO_ROOT = Path(__file__).resolve().parent.parent
ACTUATORS_CONFIG = REPO_ROOT / "contents" / "config" / "actuators.json"


def load_actuators_config() -> list[dict]:
    if not ACTUATORS_CONFIG.exists():
        print(f"[MOCK-WS] Warning: {ACTUATORS_CONFIG} not found.", file=sys.stderr)
        return []
    with open(ACTUATORS_CONFIG, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("actuators", [])


def encode_ws_text_frame(text: str) -> bytes:
    payload = text.encode("utf-8")
    length = len(payload)
    header = bytearray([0x81])  # FIN + text frame
    if length < 126:
        header.append(length)
    elif length < 65536:
        header.append(126)
        header.extend(struct.pack("!H", length))
    else:
        header.append(127)
        header.extend(struct.pack("!Q", length))
    return bytes(header) + payload


async def read_ws_frame(reader: asyncio.StreamReader) -> str | None:
    try:
        head = await reader.readexactly(2)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        return None

    opcode = head[0] & 0x0F
    if opcode == 0x8:  # Close frame
        return None

    masked = (head[1] & 0x80) != 0
    length = head[1] & 0x7F
    if length == 126:
        ext = await reader.readexactly(2)
        length = struct.unpack("!H", ext)[0]
    elif length == 127:
        ext = await reader.readexactly(8)
        length = struct.unpack("!Q", ext)[0]

    mask_key = await reader.readexactly(4) if masked else b""
    payload = bytearray(await reader.readexactly(length))
    if masked:
        for i in range(length):
            payload[i] ^= mask_key[i % 4]

    if opcode == 0x9:  # Ping
        return ""
    return payload.decode("utf-8", errors="replace")


class MockValveCartServer:
    def __init__(self, armed: bool = True, transit_delay: float = 0.75):
        self.armed = armed
        self.transit_delay = transit_delay
        self.actuators = load_actuators_config()
        self.valves: dict[str, str] = {}
        self.clients: set[asyncio.StreamWriter] = set()
        self.reset_states()

    def reset_states(self):
        self.actuators = load_actuators_config()
        self.valves = {
            item["id"]: str(item.get("normal_state", "CLOSED")).upper()
            for item in self.actuators
            if "id" in item
        }

    def get_actuator_def(self, valve_id: str) -> dict:
        for item in self.actuators:
            if item.get("id") == valve_id:
                return item
        return {}

    async def broadcast(self, message: dict):
        raw = encode_ws_text_frame(json.dumps(message))
        stale = []
        for writer in list(self.clients):
            try:
                writer.write(raw)
                await writer.drain()
            except Exception:
                stale.append(writer)
        for w in stale:
            self.clients.discard(w)

    async def send_init(self, writer: asyncio.StreamWriter):
        init_payload = {
            "type": "init",
            "system": "Flow-Control-System-Server (Mock)",
            "armed": self.armed,
            "actuators": self.actuators,
            "valves": self.valves,
        }
        writer.write(encode_ws_text_frame(json.dumps(init_payload)))
        await writer.drain()

    async def simulate_actuation(self, valve_id: str, action: str):
        vdef = self.get_actuator_def(valve_id)
        vtype = vdef.get("type", "servo")
        action = action.upper()

        if action == "CLOSE":
            target_state = "CLOSED"
        elif action == "DISCONNECT":
            target_state = "DISCONNECTED"
        elif action == "FIRE":
            target_state = "FIRED"
        else:
            target_state = action

        # Simulate limit-switch transit for servo and 3-way solenoid
        if vtype in ("servo", "solenoid_3way") and self.transit_delay > 0:
            self.valves[valve_id] = "ACTUATING"
            await self.broadcast(
                {"type": "valve_state", "valve": valve_id, "state": "ACTUATING"}
            )
            await asyncio.sleep(self.transit_delay)

        self.valves[valve_id] = target_state
        print(f"[MOCK-WS] State updated: {valve_id} -> {target_state}")
        await self.broadcast(
            {"type": "valve_state", "valve": valve_id, "state": target_state}
        )

    async def handle_message(self, raw_msg: str):
        try:
            msg = json.loads(raw_msg)
        except ValueError:
            return

        mtype = msg.get("type")
        cmd = msg.get("command")

        if mtype == "abort" or cmd == "ABORT":
            print("[MOCK-WS] !!! EMERGENCY ABORT RECEIVED !!!")
            if "N2OV" in self.valves:
                self.valves["N2OV"] = "CLOSED"
            if "DUMP" in self.valves:
                self.valves["DUMP"] = "OPEN"
            if "RTV" in self.valves:
                self.valves["RTV"] = "VENT"
            await self.broadcast({"type": "valve_state", "valves": self.valves})
            await self.broadcast(
                {
                    "type": "alert",
                    "level": "WARN",
                    "message": "Server executed EMERGENCY ABORT (fill closed, dump/vent open).",
                }
            )
            return

        if mtype == "summary" or cmd == "SUMMARY":
            await self.broadcast({"type": "summary", "valves": self.valves})
            return

        if mtype == "command" or cmd == "CTRL":
            vid = msg.get("valve", "")
            act = msg.get("action", "")
            print(f"[MOCK-WS] Command received: valve={vid}, action={act}")
            asyncio.create_task(self.simulate_actuation(vid, act))

    async def handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ):
        addr = writer.get_extra_info("peername")
        try:
            request_line = await reader.readline()
            if not request_line:
                writer.close()
                return
            headers = {}
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b"\n", b""):
                    break
                decoded = line.decode("latin1").strip()
                if ":" in decoded:
                    k, v = decoded.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            ws_key = headers.get("sec-websocket-key")
            if not ws_key:
                writer.close()
                return

            accept_val = base64.b64encode(
                hashlib.sha1((ws_key + WS_GUID).encode("ascii")).digest()
            ).decode("ascii")
            handshake = (
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept_val}\r\n\r\n"
            )
            writer.write(handshake.encode("ascii"))
            await writer.drain()

            self.clients.add(writer)
            print(f"[MOCK-WS] Client connected from {addr}")
            await self.send_init(writer)

            while True:
                frame = await read_ws_frame(reader)
                if frame is None:
                    break
                if frame:
                    await self.handle_message(frame)
        except Exception as err:
            print(f"[MOCK-WS] Client error ({addr}): {err}")
        finally:
            self.clients.discard(writer)
            writer.close()
            print(f"[MOCK-WS] Client disconnected ({addr})")


async def stdin_loop(server: MockValveCartServer):
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        parts = line.strip().split()
        if not parts:
            continue
        cmd = parts[0].lower()
        if cmd == "arm":
            server.armed = True
            print("[MOCK-WS] System set to ARMED")
            await server.broadcast({"type": "armed", "armed": True})
        elif cmd == "safe":
            server.armed = False
            print("[MOCK-WS] System set to SAFE")
            await server.broadcast({"type": "armed", "armed": False})
        elif cmd == "reset":
            server.reset_states()
            print("[MOCK-WS] Reloaded actuators.json and reset all states")
            await server.broadcast(
                {
                    "type": "init",
                    "armed": server.armed,
                    "actuators": server.actuators,
                    "valves": server.valves,
                }
            )
        elif cmd == "state" and len(parts) >= 3:
            vid = parts[1].upper()
            st = parts[2].upper()
            server.valves[vid] = st
            print(f"[MOCK-WS] Pushed state {vid} = {st}")
            await server.broadcast({"type": "valve_state", "valve": vid, "state": st})
        else:
            print(
                "[MOCK-WS] Commands: arm | safe | reset | state <VALVE_ID> <STATE>"
            )


async def main():
    parser = argparse.ArgumentParser(description="Mock Valve Cart WebSocket Server")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    parser.add_argument(
        "--safe",
        action="store_true",
        help="Start in SAFE (disarmed) mode instead of ARMED mode",
    )
    parser.add_argument(
        "--transit-delay",
        type=float,
        default=0.75,
        help="Simulated limit-switch transit time in seconds (default: 0.75)",
    )
    args = parser.parse_args()

    mock = MockValveCartServer(armed=not args.safe, transit_delay=args.transit_delay)
    srv = await asyncio.start_server(mock.handle_client, args.host, args.port)
    print(
        f"[MOCK-WS] Listening on ws://{args.host}:{args.port}/ws "
        f"(armed={mock.armed}, actuators={len(mock.actuators)})"
    )
    print("[MOCK-WS] Interactive commands: arm | safe | reset | state <ID> <STATE>")

    asyncio.create_task(stdin_loop(mock))
    async with srv:
        await srv.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[MOCK-WS] Stopped.")
