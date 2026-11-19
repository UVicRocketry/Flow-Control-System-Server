#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Hardware Simulation & Mock Mode (`mocks.py`) for Flow-Control-System-Server.

Provides `MockHardwareSimulator`, which emulates both:
  1. Controls Arduino USB serial valve controller (`send_valve_command`,
     `send_abort`, `send_unabort`, `request_summary`) with non-blocking
     async limit-switch transit delays.
  2. LabJack U6 telemetry stream (`P_*` in kPa, `T_*` in °C, `L_*` in
     N) with physics-inspired waveforms that respond dynamically to virtual
     valve states.
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ws_server import FlowControlWSServer

logger = logging.getLogger("fcs.mocks")

# Safe valve positions applied on simulated hardware ABORT
ABORT_VALVE_OVERRIDES: dict[str, str] = {
    "N2OV": "CLOSED",
    "N2OF": "CLOSED",
    "N2F": "CLOSED",
    "NCV": "CLOSED",
    "DUMP": "OPEN",
    "ERV": "OPEN",
    "RTV": "VENT",
}


def action_to_target_state(action: str) -> str:
    """Map an inbound command action (CLOSE, DISCONNECT, FIRE) to state."""
    act = str(action or "").strip().upper()
    if act == "CLOSE":
        return "CLOSED"
    if act == "DISCONNECT":
        return "DISCONNECTED"
    if act == "FIRE":
        return "FIRED"
    return act


def _noise(amplitude: float) -> float:
    return random.uniform(-amplitude, amplitude)


class MockHardwareSimulator:
    """
    Simulates the Controls Arduino and LabJack U6 hardware for `--mock` mode or
    desktop development when physical hardware is not connected.
    """

    def __init__(
        self,
        ws_server: FlowControlWSServer,
        broadcast_rate_hz: float = 20.0,
        transit_delay: float = 1.5,
        enable_telemetry: bool = True,
    ):
        self.ws_server = ws_server
        self.broadcast_rate_hz = max(1.0, float(broadcast_rate_hz))
        self.transit_delay = max(0.0, float(transit_delay))
        self.enable_telemetry = bool(enable_telemetry)

        self._start_mono: float = time.monotonic()
        self._packet_id: int = 0
        self._actuation_tasks: dict[str, asyncio.Task[Any]] = {}
        self._telemetry_task: asyncio.Task[Any] | None = None

        # Simulated sensor state in client engineering units (kPa, kg, °C, N)
        self._sim_state: dict[str, float] = {
            "P_RUN_TANK": 101.3,
            "P_INJECTOR": 101.3,
            "P_N2O_FLOW": 101.3,
            "P_N2_FLOW": 101.3,
            "P_COMB_CHMBR": 101.3,
            "T_RUN_TANK": 20.5,
            "T_INJECTOR": 21.0,
            "T_COMB_CHMBR": 22.0,
            "T_POST_COMB": 21.5,
            "M_RUN_TANK": 5.6,
            "L_THRUST": 0.0,
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the background simulated telemetry task (if enabled)."""
        self._start_mono = time.monotonic()
        if self.enable_telemetry and (
            self._telemetry_task is None or self._telemetry_task.done()
        ):
            self._telemetry_task = asyncio.create_task(self._telemetry_loop())

    async def stop(self) -> None:
        """Cancel simulated valve transits and stop telemetry generation."""
        tasks = list(self._actuation_tasks.values())
        if self._telemetry_task is not None:
            tasks.append(self._telemetry_task)
            self._telemetry_task = None

        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._actuation_tasks.clear()

    # ------------------------------------------------------------------
    # Simulated Controls Arduino Serial Interface
    # ------------------------------------------------------------------
    async def send_valve_command(self, valve: str, action: str) -> None:
        """Simulate `MCC,CTRL,<VALVE>,<ACTION>` with async transit delay."""
        vid = str(valve).strip().upper()
        act = str(action).strip().upper()

        existing = self._actuation_tasks.pop(vid, None)
        if existing is not None and not existing.done():
            existing.cancel()

        self._actuation_tasks[vid] = asyncio.create_task(
            self._simulate_actuation(vid, act)
        )

    async def _simulate_actuation(self, valve: str, action: str) -> None:
        vdef = self.ws_server.get_actuator_def(valve) or {}
        vtype = vdef.get("type", "servo")
        target_state = action_to_target_state(action)

        try:
            if vtype in ("servo", "solenoid_3way") and self.transit_delay > 0:
                await self.ws_server.update_valve_state(valve, "ACTUATING")
                await asyncio.sleep(self.transit_delay)

            await self.ws_server.update_valve_state(valve, target_state)
            logger.info(
                "[MOCK] Valve %s transitioned to %s", valve, target_state
            )
        except asyncio.CancelledError:
            logger.debug("[MOCK] Transit cancelled for valve %s", valve)
            raise

    async def send_abort(self) -> None:
        """Simulate `MCC,ABORT`: cancel transits and set safe valve states."""
        for task in self._actuation_tasks.values():
            if not task.done():
                task.cancel()
        self._actuation_tasks.clear()

        updated: dict[str, str] = {}
        for vid in list(self.ws_server.valve_states.keys()):
            if vid in ABORT_VALVE_OVERRIDES:
                updated[vid] = ABORT_VALVE_OVERRIDES[vid]
            else:
                vdef = self.ws_server.get_actuator_def(vid) or {}
                if vdef.get("type") == "servo":
                    normal = str(vdef.get("normal_state", "CLOSED")).upper()
                    updated[vid] = normal

        if updated:
            await self.ws_server.update_valve_summary(updated)

        await self.ws_server.send_alert(
            "ERR",
            (
                "EMERGENCY ABORT executed (Mock): "
                "Fill/supply valves closed, dump/vent valves opened!"
            ),
        )

    async def send_unabort(self) -> None:
        """Simulate `MCC,UNABORT`: clear abort lockout and notify clients."""
        await self.ws_server.send_alert(
            "INFO", "Valve Cart abort state cleared (Mock)."
        )

    async def request_summary(self) -> None:
        """Simulate `MCC,SUMMARY`: broadcast current valve state dict."""
        await self.ws_server.broadcast(
            {"type": "summary", "valves": dict(self.ws_server.valve_states)}
        )

    # ------------------------------------------------------------------
    # Simulated LabJack U6 Telemetry Stream
    # ------------------------------------------------------------------
    def step_telemetry(self, dt: float) -> dict[str, Any]:
        """
        Advance physics-inspired telemetry waveforms by `dt` seconds based on
        current virtual valve states (`N2OV`, `DUMP`, `RTV`, `MEV`, etc.).
        """
        self._packet_id += 1
        uptime = time.monotonic() - self._start_mono
        s = self._sim_state
        valves = self.ws_server.valve_states

        n2ov_open = (
            valves.get("N2OV") == "OPEN" or valves.get("N2OF") == "OPEN"
        )
        dump_open = (
            valves.get("DUMP") == "OPEN" or valves.get("ERV") == "OPEN"
        )
        rtv_state = valves.get("RTV", "CLOSED")
        mev_active = valves.get("MEV") in ("OPEN", "FIRED")

        # Supply line pressures [kPa]
        target_n2o_flow = 5240.0 if n2ov_open else 101.3
        target_n2_flow = 5654.0 if n2ov_open else 101.3
        s["P_N2O_FLOW"] += (
            (target_n2o_flow - s["P_N2O_FLOW"]) * min(1.0, dt * 3.5)
        )
        s["P_N2_FLOW"] += (
            (target_n2_flow - s["P_N2_FLOW"]) * min(1.0, dt * 3.0)
        )

        # Run tank pressure [kPa] & propellant mass [kg] response
        if n2ov_open and rtv_state == "FILL" and not dump_open:
            s["P_RUN_TANK"] = min(5205.0, s["P_RUN_TANK"] + 448.0 * dt)
            s["M_RUN_TANK"] = min(22.0, s["M_RUN_TANK"] + 0.8 * dt)
            s["T_RUN_TANK"] += (16.5 - s["T_RUN_TANK"]) * min(1.0, dt * 0.8)
        elif dump_open or rtv_state == "VENT":
            s["P_RUN_TANK"] = max(101.3, s["P_RUN_TANK"] - 758.0 * dt)
            s["M_RUN_TANK"] = max(5.4, s["M_RUN_TANK"] - 1.0 * dt)
            s["T_RUN_TANK"] += (8.0 - s["T_RUN_TANK"]) * min(1.0, dt * 0.5)
        else:
            s["T_RUN_TANK"] += (20.5 - s["T_RUN_TANK"]) * min(1.0, dt * 0.1)

        # Engine / injector / chamber response when MEV is open or fired
        if mev_active and s["P_RUN_TANK"] > 206.8:
            s["P_RUN_TANK"] = max(101.3, s["P_RUN_TANK"] - 586.0 * dt)
            s["M_RUN_TANK"] = max(4.5, s["M_RUN_TANK"] - 1.6 * dt)
            target_inj = s["P_RUN_TANK"] * 0.78
            target_cc = s["P_RUN_TANK"] * 0.58
            target_thrust = max(0.0, (target_cc - 101.3) * 0.61)
            s["P_INJECTOR"] += (
                (target_inj - s["P_INJECTOR"]) * min(1.0, dt * 6.0)
            )
            s["P_COMB_CHMBR"] += (
                (target_cc - s["P_COMB_CHMBR"]) * min(1.0, dt * 6.0)
            )
            s["L_THRUST"] += (
                (target_thrust - s["L_THRUST"]) * min(1.0, dt * 7.0)
            )
            s["T_INJECTOR"] += (18.0 - s["T_INJECTOR"]) * min(1.0, dt * 1.5)
            s["T_COMB_CHMBR"] += (
                (680.0 - s["T_COMB_CHMBR"]) * min(1.0, dt * 2.5)
            )
            s["T_POST_COMB"] += (
                (515.0 - s["T_POST_COMB"]) * min(1.0, dt * 2.0)
            )
        else:
            s["P_INJECTOR"] += (101.3 - s["P_INJECTOR"]) * min(1.0, dt * 4.0)
            s["P_COMB_CHMBR"] += (101.3 - s["P_COMB_CHMBR"]) * min(1.0, dt * 4)
            s["L_THRUST"] += (0.0 - s["L_THRUST"]) * min(1.0, dt * 8.0)
            s["T_INJECTOR"] += (21.0 - s["T_INJECTOR"]) * min(1.0, dt * 0.5)
            s["T_COMB_CHMBR"] += (
                (22.0 - s["T_COMB_CHMBR"]) * min(1.0, dt * 0.4)
            )
            s["T_POST_COMB"] += (21.5 - s["T_POST_COMB"]) * min(1.0, dt * 0.4)

        ripple = math.sin(uptime * 1.7)
        return {
            "packet_id": self._packet_id,
            "seconds": round(uptime, 3),
            "uptime": round(uptime, 3),
            "P_RUN_TANK": round(
                max(0.0, s["P_RUN_TANK"] + 2.0 * ripple + _noise(1.7)), 2
            ),
            "P_INJECTOR": round(
                max(0.0, s["P_INJECTOR"] + 1.4 * ripple + _noise(1.4)), 2
            ),
            "P_N2O_FLOW": round(
                max(0.0, s["P_N2O_FLOW"] + 1.7 * ripple + _noise(1.4)), 2
            ),
            "P_N2_FLOW": round(
                max(0.0, s["P_N2_FLOW"] + 1.7 * ripple + _noise(1.4)), 2
            ),
            "P_COMB_CHMBR": round(
                max(0.0, s["P_COMB_CHMBR"] + 1.4 * ripple + _noise(1.4)), 2
            ),
            "T_RUN_TANK": round(s["T_RUN_TANK"] + _noise(0.05), 2),
            "T_INJECTOR": round(s["T_INJECTOR"] + _noise(0.05), 2),
            "T_COMB_CHMBR": round(s["T_COMB_CHMBR"] + _noise(0.15), 2),
            "T_POST_COMB": round(s["T_POST_COMB"] + _noise(0.12), 2),
            "M_RUN_TANK": round(max(0.0, s["M_RUN_TANK"] + _noise(0.02)), 2),
            "L_THRUST": round(max(0.0, s["L_THRUST"] + _noise(0.65)), 2),
        }

    async def _telemetry_loop(self) -> None:
        interval = 1.0 / self.broadcast_rate_hz
        last_t = time.monotonic()
        logger.info(
            "Mock telemetry generator started at %.1f Hz",
            self.broadcast_rate_hz,
        )
        try:
            while True:
                await asyncio.sleep(interval)
                now = time.monotonic()
                dt = max(0.001, now - last_t)
                last_t = now
                if self.ws_server.clients or self.ws_server.is_recording:
                    sample = self.step_telemetry(dt)
                    await self.ws_server.broadcast_telemetry(sample)
        except asyncio.CancelledError:
            logger.debug("Mock telemetry loop stopped.")
            raise
