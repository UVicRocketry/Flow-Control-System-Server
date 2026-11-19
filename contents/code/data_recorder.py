#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
On-Cart Telemetry Session Recorder & Safe Data Store (`data_recorder.py`).

Encapsulates CSV session recording, filename traversal sanitization, NaN-safe
JSON telemetry sanitization, and CRUD operations over `data_dir` used by both
FastAPI `/api/data` endpoints and legacy WS `remote_data_req` messages.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from sensors_config import SensorsConfig, load_sensors_config

logger = logging.getLogger("fcs.data_recorder")

ALLOWED_DATA_EXTENSIONS = frozenset({".csv", ".jsonl"})


def sanitize_filename(name: str | None) -> str | None:
    """
    Sanitize a filename so recording/HTTP/WS requests cannot escape `data_dir`.
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


def _sanitize_json_value(val: Any) -> Any:
    if isinstance(val, dict):
        return {k: _sanitize_json_value(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_sanitize_json_value(v) for v in val]
    if isinstance(val, float) and not math.isfinite(val):
        return None
    return val


def sanitize_telemetry_for_json(sample: dict[str, Any]) -> dict[str, Any]:
    """Convert non-finite floats (NaN/Inf) to None for RFC 8259 JSON encoding."""
    return {k: _sanitize_json_value(v) for k, v in sample.items()}


def _format_cell_value(val: Any) -> str:
    if val is None:
        return ""
    try:
        fval = float(val)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(fval):
        return ""
    return f"{fval:.4f}"


def format_csv_row(
    flat: dict[str, Any],
    rec_seconds: float,
    csv_channels: tuple[str, ...],
) -> str:
    """Format a flattened telemetry sample into a CSV data row."""
    row_values: list[str] = []
    for ch in csv_channels:
        if ch == "seconds":
            row_values.append(f"{float(rec_seconds):.4f}")
        else:
            row_values.append(_format_cell_value(flat.get(ch)))
    return ",".join(row_values) + "\n"


def _normalize_upload_filename(raw_filename: str | None) -> str:
    filename = sanitize_filename(raw_filename)
    if not filename:
        raise ValueError("Invalid filename.")
    suffix = Path(filename).suffix.lower()
    if suffix == "":
        return f"{filename}.csv"
    if suffix not in ALLOWED_DATA_EXTENSIONS:
        raise ValueError(f"Unsupported file type for {filename}.")
    return filename


class DataRecorder:
    """Manages on-cart CSV recording sessions and safe file CRUD inside `data_dir`."""

    def __init__(
        self,
        data_dir: Path | str,
        sensors_config: SensorsConfig | None = None,
        flush_interval_sec: float = 2.5,
    ):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.sensors_config = sensors_config or load_sensors_config()
        self._flush_interval_sec = max(0.1, float(flush_interval_sec))

        self._recording_active: bool = False
        self._recording_filename: str | None = None
        self._recording_file: Any = None
        self._recording_start_mono: float = 0.0
        self._last_flush_mono: float = 0.0

    @property
    def is_recording(self) -> bool:
        return self._recording_active

    @property
    def recording_filename(self) -> str | None:
        return self._recording_filename

    @property
    def csv_channels(self) -> tuple[str, ...]:
        return self.sensors_config.csv_channels()

    @property
    def csv_units_row(self) -> str:
        return self.sensors_config.csv_units_row()

    @property
    def csv_channels_row(self) -> str:
        return self.sensors_config.csv_channels_row()

    def start_recording(self, raw_name: str) -> str:
        """Start (or re-sync) a CSV recording session and return the clean filename."""
        filename = sanitize_filename(raw_name)
        if not filename:
            raise ValueError(f"Invalid recording filename: {raw_name!r}")
        if not filename.lower().endswith(tuple(ALLOWED_DATA_EXTENSIONS)):
            filename = f"{filename}.csv"

        if self._recording_active and self._recording_filename == filename:
            logger.info(
                "Recording already active for %s (idempotent re-sync)",
                filename,
            )
            return filename

        if self._recording_active:
            self.stop_recording()

        self.data_dir.mkdir(parents=True, exist_ok=True)
        filepath = self.data_dir / filename
        exists_nonempty = filepath.exists() and filepath.stat().st_size > 0
        self._recording_file = open(filepath, "a", encoding="utf-8", buffering=1)
        if not exists_nonempty:
            self._recording_file.write(self.csv_units_row + "\n")
            self._recording_file.write(self.csv_channels_row + "\n")
            self._recording_file.flush()

        self._recording_filename = filename
        self._recording_active = True
        self._recording_start_mono = time.monotonic()
        self._last_flush_mono = self._recording_start_mono
        logger.info("Started on-cart telemetry recording -> %s", filepath)
        return filename

    def stop_recording(self) -> str | None:
        """Flush and close any active telemetry recording session."""
        finished_name = self._recording_filename if self._recording_active else None
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
        return finished_name

    def flush(self) -> None:
        """Flush buffered writes for the active recording file, if open."""
        if self._recording_file is not None:
            try:
                self._recording_file.flush()
            except Exception as exc:
                logger.debug("Flush failed for %s: %s", self._recording_filename, exc)

    def write_sample(self, sample: dict[str, Any]) -> None:
        """Append one telemetry sample row if recording is currently active."""
        if not self._recording_active or self._recording_file is None:
            return

        flat = _flatten_sample(sample)
        now_mono = time.monotonic()
        rec_seconds = flat.get("seconds")
        if rec_seconds is None:
            rec_seconds = round(now_mono - self._recording_start_mono, 4)

        row_line = format_csv_row(flat, float(rec_seconds), self.csv_channels)
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

    def list_files(self) -> list[str]:
        """Return reverse-sorted `.csv` and `.jsonl` files in `data_dir`."""
        if not self.data_dir.exists():
            return []
        files = [
            p.name
            for p in self.data_dir.iterdir()
            if p.is_file() and p.suffix.lower() in ALLOWED_DATA_EXTENSIONS
        ]
        files.sort(reverse=True)
        return files

    def resolve_download_path(
        self, raw_filename: str | None, mode: str = "load"
    ) -> Path:
        """Resolve a downloadable file inside `data_dir`, flushing active logs."""
        target_name = raw_filename
        if mode == "pull_recording" and not target_name:
            target_name = self.recording_filename

        filename = sanitize_filename(target_name)
        if not filename:
            raise ValueError("Invalid filename.")

        if self.is_recording and self.recording_filename == filename:
            self.flush()

        filepath = self.data_dir / filename
        if not filepath.exists() or not filepath.is_file():
            raise FileNotFoundError(f"Data file not found: {filename}")
        return filepath

    def save_upload_chunks(
        self, raw_filename: str, chunks: Iterable[bytes]
    ) -> tuple[str, bool, int]:
        """Stream uploaded bytes into `data_dir` via a temporary file."""
        filename = _normalize_upload_filename(raw_filename)
        if self.is_recording and self.recording_filename == filename:
            raise PermissionError(f"{filename} is currently being recorded.")

        self.data_dir.mkdir(parents=True, exist_ok=True)
        target = self.data_dir / filename
        overwrote = target.exists()
        tmp_path = self.data_dir / f".{filename}.upload.tmp"
        total_bytes = 0
        try:
            with open(tmp_path, "wb") as f:
                for chunk in chunks:
                    if chunk:
                        f.write(chunk)
                        total_bytes += len(chunk)
            tmp_path.replace(target)
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise
        logger.info(
            "Stored uploaded remote file: %s (%d bytes, overwrote=%s)",
            filename,
            total_bytes,
            overwrote,
        )
        return filename, overwrote, total_bytes

    def save_upload_text(
        self, raw_filename: str, content: str
    ) -> tuple[str, bool, int]:
        """Store UTF-8 uploaded content in `data_dir`."""
        return self.save_upload_chunks(raw_filename, (content.encode("utf-8"),))

    def rename_file(self, old_raw: str, new_raw: str) -> tuple[str, str]:
        """Rename a telemetry file safely inside `data_dir`."""
        old_name = sanitize_filename(old_raw)
        new_name = sanitize_filename(new_raw)
        if not old_name or not new_name:
            raise ValueError("Invalid source or target filename.")

        old_path = self.data_dir / old_name
        new_path = self.data_dir / new_name
        if not old_path.exists() or not old_path.is_file():
            raise FileNotFoundError(f"{old_name} does not exist.")
        if new_path.exists() and old_path != new_path:
            raise FileExistsError(f"{new_name} already exists.")

        old_path.rename(new_path)
        if self._recording_filename == old_name:
            self._recording_filename = new_name
        logger.info("Renamed remote file %s -> %s", old_name, new_name)
        return old_name, new_name

    def delete_file(self, raw_filename: str) -> str:
        """Delete a telemetry file inside `data_dir` unless actively recording."""
        filename = sanitize_filename(raw_filename)
        if not filename:
            raise ValueError("Invalid filename.")
        if self.is_recording and self.recording_filename == filename:
            raise PermissionError(f"Cannot delete active recording {filename}.")

        target = self.data_dir / filename
        if not target.exists() or not target.is_file():
            raise FileNotFoundError(f"Data file not found: {filename}")
        target.unlink()
        logger.info("Deleted remote data file: %s", filename)
        return filename
