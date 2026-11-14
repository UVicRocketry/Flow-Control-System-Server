#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Flow Control System Server — Main Entrypoint.

Starts the unified asyncio WebSocket server (`ws_server.py`) on
0.0.0.0:8080/ws, attaching either physical hardware controllers or
`MockHardwareSimulator` (`mocks.py`).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
from pathlib import Path

from mocks import MockHardwareSimulator
from ws_server import (
    DEFAULT_ACTUATORS_CONFIG,
    DEFAULT_DATA_DIR,
    FlowControlWSServer,
)


def detect_hardware_present(serial_port: str) -> bool:
    """
    Check if physical Valve Cart hardware (Arduino serial port) is present.
    Until serial_controller / instrumentation modules are attached or if the
    device node is absent, the server falls back to mock simulation mode.
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
        help="Force hardware simulation / mock mode",
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
        help="Start server in ARMED state (default in mock mode)",
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
        help="Telemetry broadcast rate in Hz (default: 20.0)",
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
        help=(
            "Path to actuators.json config "
            f"(default: {DEFAULT_ACTUATORS_CONFIG})"
        ),
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=DEFAULT_DATA_DIR,
        help=(
            "Directory for recorded telemetry files "
            f"(default: {DEFAULT_DATA_DIR})"
        ),
    )
    parser.add_argument(
        "--log-level",
        default=os.environ.get("FCS_LOG_LEVEL", "INFO"),
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging verbosity (default: INFO)",
    )
    return parser.parse_args(argv)


async def async_main(args: argparse.Namespace) -> None:
    hw_present = detect_hardware_present(args.serial_port)
    mock_mode = bool(args.mock or not hw_present)

    if not args.mock and not hw_present:
        logging.getLogger("fcs.main").warning(
            "Hardware serial port '%s' not found; "
            "falling back to MOCK mode automatically.",
            args.serial_port,
        )

    # Default to ARMED in mock mode for immediate desktop testing unless --safe
    initial_armed = args.armed if args.armed is not None else mock_mode

    system_name = (
        "Flow-Control-System-Server (Mock)"
        if mock_mode
        else "Flow-Control-System-Server"
    )
    connect_alert = (
        {
            "level": "WARN",
            "message": (
                "Server is running in MOCK SIMULATION mode "
                "(no physical hardware active)."
            ),
        }
        if mock_mode
        else None
    )

    server = FlowControlWSServer(
        host=args.host,
        port=args.port,
        armed=initial_armed,
        config_path=args.config,
        data_dir=args.data_dir,
        system_name=system_name,
        connect_alert=connect_alert,
    )

    mock_sim: MockHardwareSimulator | None = None
    if mock_mode:
        mock_sim = MockHardwareSimulator(
            ws_server=server,
            broadcast_rate_hz=args.rate,
            transit_delay=args.transit_delay,
        )
        server.serial_controller = mock_sim
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
        if mock_sim is not None:
            await mock_sim.stop()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        asyncio.run(async_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
