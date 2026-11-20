#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unified logging formatter and setup for Flow-Control-System-Server and Uvicorn."""

from __future__ import annotations

import http
import logging
import os
import sys
from typing import Any

DEFAULT_DATEFMT = "%Y-%m-%d %H:%M:%S"

LEVEL_LABELS: dict[int, str] = {
    logging.DEBUG: "DBG",
    logging.INFO: "INFO",
    logging.WARNING: "WARN",
    logging.ERROR: "ERR",
    logging.CRITICAL: "CRIT",
}

LEVEL_COLORS: dict[int, str] = {
    logging.DEBUG: "\033[36m",
    logging.INFO: "\033[32m",
    logging.WARNING: "\033[33m",
    logging.ERROR: "\033[31m",
    logging.CRITICAL: "\033[91m",
}

ANSI_DIM = "\033[2m"
ANSI_RESET = "\033[0m"

LOGGER_TYPE_ALIASES: dict[str, str] = {
    "fcs.main": "main",
    "fcs.mocks": "mocks",
    "fcs.ws_server": "server",
    "fcs.data_recorder": "recorder",
    "fcs.instrumentation": "labjack",
    "uvicorn": "uvicorn",
    "uvicorn.error": "uvicorn",
    "uvicorn.access": "http",
    "uvicorn.asgi": "asgi",
    "websockets.server": "ws",
    "websockets.protocol": "ws",
}

LOGGER_TYPE_WIDTH: int = max(len(alias) for alias in LOGGER_TYPE_ALIASES.values())

UVICORN_LOGGERS: tuple[str, ...] = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
    "uvicorn.asgi",
)

_UVICORN_LEVEL_NAMES: dict[int, str] = {
    logging.DEBUG: "debug",
    logging.INFO: "info",
    logging.WARNING: "warning",
    logging.ERROR: "error",
    logging.CRITICAL: "critical",
}


class FCSLogFormatter(logging.Formatter):
    """Formatter emitting `INFO: <DATE> <type> - <message>` across FCS and Uvicorn."""

    def __init__(
        self,
        *,
        datefmt: str = DEFAULT_DATEFMT,
        use_colors: bool = False,
        logger_type_width: int = LOGGER_TYPE_WIDTH,
    ):
        super().__init__(datefmt=datefmt)
        self.use_colors = bool(use_colors)
        self.logger_type_width = max(0, int(logger_type_width))

    def _format_level_prefix(self, record: logging.LogRecord) -> str:
        label = LEVEL_LABELS.get(record.levelno, record.levelname)
        pad = " " * max(1, 6 - (len(label) + 1))
        if self.use_colors:
            color = LEVEL_COLORS.get(record.levelno, "")
            colored_label = f"{color}{label}{ANSI_RESET}" if color else label
            return f"{colored_label}:{pad}"
        return f"{label}:{pad}"

    def _format_status_code(self, status_code: int) -> str:
        try:
            phrase = http.HTTPStatus(status_code).phrase
            text = f"{status_code} {phrase}"
        except ValueError:
            text = str(status_code)

        if not self.use_colors:
            return text

        if 200 <= status_code < 300:
            color = "\033[32m"
        elif 300 <= status_code < 400:
            color = "\033[36m"
        elif 400 <= status_code < 500:
            color = "\033[33m"
        elif status_code >= 500:
            color = "\033[91m"
        else:
            return text
        return f"{color}{text}{ANSI_RESET}"

    def _format_message(self, record: logging.LogRecord) -> str:
        if (
            record.name == "uvicorn.access"
            and isinstance(record.args, tuple)
            and len(record.args) == 5
        ):
            client_addr, method, full_path, http_version, raw_status = record.args
            try:
                status_text = self._format_status_code(int(raw_status))
                return f'{client_addr} - "{method} {full_path} HTTP/{http_version}" {status_text}'
            except (TypeError, ValueError):
                return record.getMessage()
        return record.getMessage()

    @staticmethod
    def _resolve_logger_type(name: str) -> str:
        alias = LOGGER_TYPE_ALIASES.get(name)
        if alias is not None:
            return alias
        return name.removeprefix("fcs.")

    def format(self, record: logging.LogRecord) -> str:
        timestamp = self.formatTime(record, self.datefmt)
        if self.use_colors:
            timestamp = f"{ANSI_DIM}{timestamp}{ANSI_RESET}"
        logger_type = self._resolve_logger_type(record.name).rjust(
            self.logger_type_width
        )
        prefix = self._format_level_prefix(record)
        msg = self._format_message(record)
        formatted = f"{prefix}{timestamp} {logger_type} - {msg}"

        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            formatted = f"{formatted}\n{record.exc_text}"
        if record.stack_info:
            formatted = f"{formatted}\n{self.formatStack(record.stack_info)}"
        return formatted


def _normalize_numeric_level(level: int | str) -> int:
    if isinstance(level, int):
        return level
    resolved = getattr(logging, str(level).strip().upper(), None)
    return resolved if isinstance(resolved, int) else logging.INFO


def resolve_uvicorn_log_level(level: int | str | None = None) -> str:
    """Resolve a Python/string log level to a Uvicorn CLI log-level name."""
    if level is None:
        numeric = logging.getLogger().getEffectiveLevel()
    else:
        numeric = _normalize_numeric_level(level)
    return _UVICORN_LEVEL_NAMES.get(numeric, "info")


def configure_logging(
    level: int | str = logging.INFO,
    *,
    stream: Any = None,
    use_colors: bool | None = None,
) -> logging.Handler:
    """Configure root logging and route Uvicorn loggers through `FCSLogFormatter`."""
    numeric_level = _normalize_numeric_level(level)
    target_stream = stream if stream is not None else sys.stderr

    if use_colors is None:
        colors = (
            not os.environ.get("NO_COLOR")
            and hasattr(target_stream, "isatty")
            and bool(target_stream.isatty())
        )
    else:
        colors = bool(use_colors)

    handler = logging.StreamHandler(target_stream)
    handler.setFormatter(FCSLogFormatter(use_colors=colors))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(numeric_level)

    for logger_name in UVICORN_LOGGERS:
        lgr = logging.getLogger(logger_name)
        lgr.handlers.clear()
        lgr.propagate = True
        lgr.setLevel(logging.NOTSET)

    return handler
