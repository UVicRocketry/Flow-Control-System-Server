#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flow Control System Server — Main Entrypoint.

Starts the unified asyncio WebSocket server (`ws_server.py`) on
0.0.0.0:8080/ws, attaching physical hardware controllers (`instrumentation.py`
for LabJack U6 telemetry) or falling back to `MockHardwareSimulator`
(`mocks.py`) per subsystem when hardware is absent or `--mock` is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from instrumentation import LabJackInstrumentation, detect_labjack_present
from logging_config import configure_logging
from mocks import MockHardwareSimulator
from sensors_config import DEFAULT_SENSORS_CONFIG
from ws_server import (
    DEFAULT_ACTUATORS_CONFIG,
    DEFAULT_DATA_DIR,
    FlowControlWSServer,
)


def detect_hardware_present(serial_port: str) -> bool:
    """
    Check if physical Valve Cart hardware (Arduino serial port) is present.
    """
    return Path(serial_port).exists()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flow Control System Server (Valve Cart WebSocket Backend)"
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("FCS_HOST", "0.0.0.0"),
        help="WebSocket bind address (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("FCS_PORT", "8080")),
        help="WebSocket bind port (default: 8080)",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        default=os.environ.get("FCS_MOCK", "0") == "1",
        help="Force hardware simulation / mock mode for all subsystems",
    )
    parser.add_argument(
        "--mock-labjack",
        action="store_true",
        default=os.environ.get("FCS_MOCK_LABJACK", "0") == "1",
        help="Force mock telemetry simulation for LabJack U6 only",
    )
    parser.add_argument(
        "--mock-serial",
        action="store_true",
        default=os.environ.get("FCS_MOCK_SERIAL", "0") == "1",
        help="Force mock valve actuation for Controls Arduino serial only",
    )
    parser.add_argument(
        "--serial-port",
        default=os.environ.get("FCS_SERIAL_PORT", "/dev/ttyACM0"),
        help="Controls Arduino serial port path (default: /dev/ttyACM0)",
    )
    arm_group = parser.add_mutually_exclusive_group()
    arm_group.add_argument(
        "--armed",
        dest="armed",
        action="store_true",
        help="Start server in ARMED state (default in full mock mode)",
    )
    arm_group.add_argument(
        "--safe",
        dest="armed",
        action="store_false",
        help="Start server in SAFE (disarmed) state",
    )
    parser.set_defaults(armed=None)
    parser.add_argument(
        "--rate",
        type=float,
        default=float(os.environ.get("FCS_BROADCAST_HZ", "20.0")),
        help="WebSocket telemetry broadcast rate in Hz (default: 20.0; LabJack logs full rate)",
    )
    parser.add_argument(
        "--transit-delay",
        type=float,
        default=float(os.environ.get("FCS_TRANSIT_DELAY", "1.5")),
        help="Mock valve limit-switch transit delay in seconds (default: 1.5)",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_ACTUATORS_CONFIG,
        help=(f"Path to actuators.json config (default: {DEFAULT_ACTUATORS_CONFIG})"),
    )
    parser.add_argument(
        "--sensors-config",
        type=Path,
        default=DEFAULT_SENSORS_CONFIG,
        help=(f"Path to sensors.json config (default: {DEFAULT_SENSORS_CONFIG})"),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=(f"Directory for recorded telemetry files (default: {DEFAULT_DATA_DIR})"),
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("FCS_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    return parser.parse_args(argv)


def _resolve_mode_banner(
    mock_labjack: bool, mock_serial: bool
) -> tuple[str, dict[str, str] | None]:
    if mock_labjack and mock_serial:
        return (
            "Flow-Control-System-Server (Mock)",
            {
                "level": "WARN",
                "message": (
                    "Server is running in MOCK SIMULATION mode "
                    "(no physical hardware active)."
                ),
            },
        )
    if mock_serial:
        return (
            "Flow-Control-System-Server (Live LabJack)",
            {
                "level": "INFO",
                "message": (
                    "LabJack U6 live telemetry active "
                    "(valve actuation in mock simulation mode)."
                ),
            },
        )
    return "Flow-Control-System-Server", None


async def async_main(args: argparse.Namespace) -> None:
    log = logging.getLogger("fcs.main")

    lj_present = False if (args.mock or args.mock_labjack) else detect_labjack_present()
    serial_present = (
        False
        if (args.mock or args.mock_serial)
        else detect_hardware_present(args.serial_port)
    )

    mock_labjack = bool(args.mock or args.mock_labjack or not lj_present)
    # Until serial_controller is wired in, serial falls back to mock actuation
    mock_serial = True

    if not args.mock and not args.mock_labjack and not lj_present:
        log.warning(
            "LabJack U6 not detected (or u6/exodriver unavailable); "
            "falling back to MOCK telemetry automatically."
        )
    elif not mock_labjack:
        log.info("LabJack U6 detected; using live full-rate LabJack telemetry.")

    if not args.mock and not args.mock_serial and not serial_present:
        log.warning(
            "Hardware serial port '%s' not found; "
            "falling back to MOCK valve actuation.",
            args.serial_port,
        )

    full_mock = mock_labjack and mock_serial
    # Default to ARMED only in full mock mode for desktop testing unless --safe
    initial_armed = args.armed if args.armed is not None else full_mock
    system_name, connect_alert = _resolve_mode_banner(mock_labjack, mock_serial)

    server = FlowControlWSServer(
        host=args.host,
        port=args.port,
        armed=initial_armed,
        config_path=args.config,
        sensors_config_path=args.sensors_config,
        data_dir=args.data_dir,
        system_name=system_name,
        connect_alert=connect_alert,
    )

    lj_inst: LabJackInstrumentation | None = None
    mock_sim: MockHardwareSimulator | None = None

    if not mock_labjack:
        lj_inst = LabJackInstrumentation(
            ws_server=server,
            broadcast_rate_hz=args.rate,
            sensors_config=server.sensors_config,
        )
        server.instrumentation = lj_inst
        lj_inst.start()

    if mock_labjack or mock_serial:
        mock_sim = MockHardwareSimulator(
            ws_server=server,
            broadcast_rate_hz=args.rate,
            transit_delay=args.transit_delay,
            enable_telemetry=mock_labjack,
        )
        if mock_serial:
            server.serial_controller = mock_sim
        if mock_labjack:
            server.instrumentation = mock_sim
        mock_sim.start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, server.request_shutdown)
        except NotImplementedError:
            pass

    try:
        await server.serve_forever()
    finally:
        if lj_inst is not None:
            await lj_inst.stop()
        if mock_sim is not None:
            await mock_sim.stop()


def main() -> None:
    args = parse_args()
    configure_logging(args.log_level)
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
