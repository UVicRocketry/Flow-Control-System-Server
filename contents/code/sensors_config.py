#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Validated Sensor & LabJack U6 Stream Configuration (`sensors_config.py`).

Loads hardware channel and calibration definitions from
`contents/config/sensors.json` into Pydantic-validated models (with a stdlib
validation fallback when Pydantic is unavailable locally) so
`instrumentation.py`, `data_recorder.py`, and WS `init` share one hardware
truth without duplicating client UI metadata.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from pydantic import (  # type: ignore
        BaseModel,
        ValidationError,
        field_validator,
        model_validator,
    )

    HAS_PYDANTIC = True
except ImportError:  # pragma: no cover - stdlib fallback on minimal VMs
    BaseModel = None  # type: ignore[assignment,misc]
    ValidationError = None  # type: ignore[assignment,misc]
    field_validator = None  # type: ignore[assignment]
    model_validator = None  # type: ignore[assignment]
    HAS_PYDANTIC = False

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_SENSORS_CONFIG = REPO_ROOT / "contents" / "config" / "sensors.json"

GAIN_BITMASKS: dict[int, int] = {
    1: 0b00000000,
    10: 0b00010000,
    100: 0b00100000,
    1000: 0b00110000,
}

MODE_BITMASKS: dict[str, int] = {
    "SING": 0b00000000,
    "DIFF": 0b10000000,
}


class SensorConfigValidationError(ValueError):
    """Raised when `sensors.json` fails schema or domain validation."""


def _validate_non_empty_str(val: Any, field_name: str) -> str:
    if not isinstance(val, str) or not val.strip():
        raise SensorConfigValidationError(f"{field_name} must be a non-empty string")
    return val.strip()


def _validate_int_bound(
    val: Any, field_name: str, *, minimum: int, maximum: int | None = None
) -> int:
    if isinstance(val, bool) or not isinstance(val, int):
        raise SensorConfigValidationError(f"{field_name} must be an integer")
    if val < minimum or (maximum is not None and val > maximum):
        raise SensorConfigValidationError(f"{field_name} out of range: {val}")
    return val


def _validate_finite_float(val: Any, field_name: str) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise SensorConfigValidationError(f"{field_name} must be numeric")
    fval = float(val)
    if not math.isfinite(fval):
        raise SensorConfigValidationError(f"{field_name} must be finite")
    return fval


def _validate_stream_dict(data: dict[str, Any]) -> dict[str, int]:
    return {
        "scan_frequency": _validate_int_bound(
            data.get("scan_frequency"), "scan_frequency", minimum=1
        ),
        "resolution_index": _validate_int_bound(
            data.get("resolution_index"), "resolution_index", minimum=0
        ),
        "settling_factor": _validate_int_bound(
            data.get("settling_factor"), "settling_factor", minimum=0
        ),
        "samples_per_packet": _validate_int_bound(
            data.get("samples_per_packet"), "samples_per_packet", minimum=1
        ),
    }


def _validate_sensor_dict(data: dict[str, Any]) -> dict[str, Any]:
    mode = _validate_non_empty_str(data.get("mode"), "mode").upper()
    if mode not in MODE_BITMASKS:
        raise SensorConfigValidationError(f"Invalid mode: {mode!r}")
    gain_index = _validate_int_bound(data.get("gain_index"), "gain_index", minimum=1)
    if gain_index not in GAIN_BITMASKS:
        raise SensorConfigValidationError(f"Invalid gain_index: {gain_index!r}")
    return {
        "id": _validate_non_empty_str(data.get("id"), "id"),
        "pin": _validate_int_bound(data.get("pin"), "pin", minimum=0, maximum=127),
        "mode": mode,
        "gain_index": gain_index,
        "unit": _validate_non_empty_str(data.get("unit"), "unit"),
        "scale": _validate_finite_float(data.get("scale", 1.0), "scale"),
        "offset": _validate_finite_float(data.get("offset", 0.0), "offset"),
    }


def _validate_sensor_list_rules(stream_spp: int, sensor_ids: list[str]) -> None:
    if not sensor_ids:
        raise SensorConfigValidationError("sensors list must not be empty")
    seen: set[str] = set()
    for sid in sensor_ids:
        if sid in seen:
            raise SensorConfigValidationError(f"Duplicate sensor id: {sid!r}")
        seen.add(sid)
    if stream_spp < len(sensor_ids):
        raise SensorConfigValidationError(
            f"samples_per_packet ({stream_spp}) must be >= sensor count ({len(sensor_ids)})"
        )


class _SensorChannelMixin:
    """Shared derived properties for a configured LabJack channel."""

    id: str
    pin: int
    mode: str
    gain_index: int
    unit: str

    @property
    def ain_key(self) -> str:
        return f"AIN{self.pin}"

    @property
    def channel_option_byte(self) -> int:
        return MODE_BITMASKS[self.mode] | GAIN_BITMASKS[self.gain_index]

    @property
    def is_thermocouple(self) -> bool:
        return self.id.upper().startswith("T_") or self.unit.upper() in (
            "C",
            "°C",
            "K",
        )


class _SensorsConfigMixin:
    """Shared LabJack/CSV/WebSocket helper methods for both model backends."""

    stream: Any
    sensors: list[Any]

    def channel_settings(self) -> list[tuple[int, int]]:
        """Return `(pin, option_byte)` tuples for `u6.U6.streamConfig`."""
        return [(s.pin, s.channel_option_byte) for s in self.sensors]

    def csv_channels(self) -> tuple[str, ...]:
        """Return canonical ordered CSV telemetry headers ending in `seconds`."""
        return tuple(s.id for s in self.sensors) + ("seconds",)

    def csv_units_row(self) -> str:
        """Return row 0 of the two-row CSV header."""
        return ",".join([s.unit for s in self.sensors] + ["s"])

    def csv_channels_row(self) -> str:
        """Return row 1 of the two-row CSV header."""
        return ",".join(self.csv_channels())

    def ws_channels_metadata(self) -> dict[str, dict[str, Any]]:
        """Return minimal hardware unit/pin metadata keyed by channel ID."""
        return {
            s.id: {
                "unit": s.unit,
                "pin": s.ain_key,
            }
            for s in self.sensors
        }

    def get_sensor(self, channel_id: str) -> Any | None:
        """Look up a sensor channel config by ID."""
        for sensor in self.sensors:
            if sensor.id == channel_id:
                return sensor
        return None


if HAS_PYDANTIC:

    class StreamSettings(BaseModel):
        scan_frequency: int
        resolution_index: int
        settling_factor: int
        samples_per_packet: int

        @field_validator("scan_frequency", "samples_per_packet")
        @classmethod
        def _positive_int(cls, v: int, info: Any) -> int:
            return _validate_int_bound(v, info.field_name, minimum=1)

        @field_validator("resolution_index", "settling_factor")
        @classmethod
        def _non_negative_int(cls, v: int, info: Any) -> int:
            return _validate_int_bound(v, info.field_name, minimum=0)

    class SensorChannelConfig(_SensorChannelMixin, BaseModel):
        id: str
        pin: int
        mode: str
        gain_index: int
        unit: str
        scale: float = 1.0
        offset: float = 0.0

        @field_validator("id", "unit")
        @classmethod
        def _non_empty(cls, v: str, info: Any) -> str:
            return _validate_non_empty_str(v, info.field_name)

        @field_validator("mode")
        @classmethod
        def _valid_mode(cls, v: str) -> str:
            norm = _validate_non_empty_str(v, "mode").upper()
            if norm not in MODE_BITMASKS:
                raise ValueError(f"Invalid mode: {norm!r}")
            return norm

        @field_validator("pin")
        @classmethod
        def _valid_pin(cls, v: int) -> int:
            return _validate_int_bound(v, "pin", minimum=0, maximum=127)

        @field_validator("gain_index")
        @classmethod
        def _valid_gain(cls, v: int) -> int:
            if v not in GAIN_BITMASKS:
                raise ValueError(f"Invalid gain_index: {v!r}")
            return v

        @field_validator("scale", "offset")
        @classmethod
        def _finite_number(cls, v: float, info: Any) -> float:
            return _validate_finite_float(v, info.field_name)

    class SensorsConfig(_SensorsConfigMixin, BaseModel):
        stream: StreamSettings
        sensors: list[SensorChannelConfig]

        @model_validator(mode="after")
        def _validate_cross_fields(self) -> SensorsConfig:
            _validate_sensor_list_rules(
                self.stream.samples_per_packet, [s.id for s in self.sensors]
            )
            return self

else:  # pragma: no cover - stdlib fallback when pydantic is absent

    @dataclass(frozen=True)
    class StreamSettings:
        scan_frequency: int
        resolution_index: int
        settling_factor: int
        samples_per_packet: int

        @classmethod
        def model_validate(cls, data: Any) -> StreamSettings:
            if not isinstance(data, dict):
                raise SensorConfigValidationError("stream must be an object")
            return cls(**_validate_stream_dict(data))

        def model_dump(self) -> dict[str, Any]:
            return asdict(self)

    @dataclass(frozen=True)
    class SensorChannelConfig(_SensorChannelMixin):
        id: str
        pin: int
        mode: str
        gain_index: int
        unit: str
        scale: float = 1.0
        offset: float = 0.0

        @classmethod
        def model_validate(cls, data: Any) -> SensorChannelConfig:
            if not isinstance(data, dict):
                raise SensorConfigValidationError("sensor entry must be an object")
            return cls(**_validate_sensor_dict(data))

        def model_dump(self) -> dict[str, Any]:
            return asdict(self)

    @dataclass(frozen=True)
    class SensorsConfig(_SensorsConfigMixin):
        stream: StreamSettings
        sensors: list[SensorChannelConfig]

        @classmethod
        def model_validate(cls, data: Any) -> SensorsConfig:
            if not isinstance(data, dict):
                raise SensorConfigValidationError(
                    "sensors config must be a JSON object"
                )
            stream = StreamSettings.model_validate(data.get("stream"))
            raw_sensors = data.get("sensors")
            if not isinstance(raw_sensors, list):
                raise SensorConfigValidationError("sensors must be a list")
            sensors = [SensorChannelConfig.model_validate(s) for s in raw_sensors]
            _validate_sensor_list_rules(
                stream.samples_per_packet, [s.id for s in sensors]
            )
            return cls(stream=stream, sensors=sensors)

        def model_dump(self) -> dict[str, Any]:
            return {
                "stream": self.stream.model_dump(),
                "sensors": [s.model_dump() for s in self.sensors],
            }


def validate_sensors_payload(raw_data: Any) -> SensorsConfig:
    """Validate an in-memory dictionary against `SensorsConfig`."""
    try:
        return SensorsConfig.model_validate(raw_data)
    except Exception as exc:
        if (
            HAS_PYDANTIC
            and ValidationError is not None
            and isinstance(exc, ValidationError)
        ):
            raise SensorConfigValidationError(str(exc)) from exc
        if isinstance(exc, SensorConfigValidationError):
            raise
        raise SensorConfigValidationError(str(exc)) from exc


def load_sensors_config(path: Path | str | None = None) -> SensorsConfig:
    """Load and validate `sensors.json` from disk."""
    cfg_path = Path(path) if path is not None else DEFAULT_SENSORS_CONFIG
    with open(cfg_path, "r", encoding="utf-8") as f:
        raw_data = json.load(f)
    return validate_sensors_payload(raw_data)
