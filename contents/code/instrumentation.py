#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LabJack U6 Telemetry Acquisition (`instrumentation.py`) for
Flow-Control-System-Server.

Streams 11 analog channels (5 pressures in kPa, 1 runtank mass load cell in kg,
1 thrust load cell in N, 4 Type-K thermocouples in °C) from a LabJack U6 at full
packet rate (~83.3 Hz at 1000 Hz scan frequency / 12 samples per packet) in a
dedicated background thread, recording every full-rate packet via `DataRecorder`
while decimating live WebSocket broadcasts to `--rate` Hz.
"""

from __future__ import annotations

import asyncio
import logging
import math
import threading
import time
from typing import TYPE_CHECKING, Any

from sensors_config import (
    GAIN_BITMASKS,
    MODE_BITMASKS,
    SensorsConfig,
    load_sensors_config,
)
from thermocouple import V_to_C, get_ref_voltage

try:
    import u6  # type: ignore
except ImportError:
    u6 = None  # type: ignore

if TYPE_CHECKING:
    from ws_server import FlowControlWSServer

logger = logging.getLogger("fcs.instrumentation")

# Gains
X1 = GAIN_BITMASKS[1]
X10 = GAIN_BITMASKS[10]
X100 = GAIN_BITMASKS[100]
X1000 = GAIN_BITMASKS[1000]

# Channel mode
SING = MODE_BITMASKS["SING"]
DIFF = MODE_BITMASKS["DIFF"]


"""
NOTE:
  Channel pin assignments, gains, scales, offsets, and stream timing are now
  configured in `contents/config/sensors.json` (and validated at startup by
  `sensors_config.py`). See "Adding or Updating a Sensor" below.


Overview:

  LabJackPython wraps the low-level functions for communicating with the
  LabJack U6 in Python. All of the low-level functions are discussed in
  section 5.2 of the LabJack U6 user guide.

  This module configures the U6 in stream mode to acquire data at the fastest
  rate possible in a dedicated USB reader thread. Full-rate packets are
  logged directly on disk by `DataRecorder`, while live WebSocket broadcasts
  to the operator GUI are decimated to `--rate` Hz.


Sensor Overview:

  Both the CTL-190 and HKM-375 are absolute pressure transducers that have
  a supply voltage of 10VDC, output impedance of 1000 ohms and a differential
  output with full scale range of 100mV.

  All thermocouples used are K-type.

  Load cells used are typical bridge sensors with low output impedance and
  a very small full scale range. Supplied with 5VDC.


streamConfig() / `sensors.json` -> `"stream"` settings:

  Streaming data from the LJ is the fastest way to acquire data.
  See section 5.2.12 of the U6 user guide for StreamConfig details.

  samples_per_packet:
    The number of samples in each packet sent over USB by the LJ. If the
    number of channels being scanned is low, set this lower than the default
    of 25 so packet frequency stays responsive. Must always be >= len(sensors).

  scan_frequency:
    The frequency in Hz to scan the channel list (ChannelNumbers).
    The sample rate (Hz) = scan_frequency * num_channels.

  num_channels:
    Derived automatically from `len(sensors)` in `sensors.json`.

  resolution_index:
    See section 3.1 of the U6 user guide. Higher values increase ENOB at the
    expense of longer sample times.

  settling_factor:
    For high-impedance sensors, a longer settling time is required to acquire
    enough charge in the ADC for an accurate reading. LJ notes that the auto
    setting works well up to 1000 ohm output impedance:
    https://support.labjack.com/docs/analog-input-settling-time-app-note

  channel_settings (built from `pin`, `mode`, and `gain_index` in `sensors.json`):
    For differential pairs, specify only the positive channel (`pin`) with
    `"mode": "DIFF"`; the LabJack automatically pairs the negative channel
    (extended mux channels e.g. 81, 83, 85, 87 are valid differential pins).
    Options byte encoding in the LabJack U6 hardware:

      Set bits 4-5 = GainIndex:
        GainIndex   Gain   Max V    Min V
        b00         1      10.1     -10.6
        b01         10     1.01     -1.06
        b10         100    0.101    -0.106
        b11         1000   0.0101   -0.0106

      Set bit 7 for differential reading (`DIFF = 0x80`, `SING = 0x00`).


Adding or Updating a Sensor:

  All LabJack channel definitions live in `contents/config/sensors.json`:
    1) Add or update an object in the `"sensors"` array with:
         {
           "id": "P_NEW_SENSOR",
           "pin": 88,
           "mode": "DIFF",      // "DIFF" or "SING"
           "gain_index": 100,   // 1, 10, 100, or 1000
           "scale": 140000.0,   // linear multiplier; omit for Type-K TCs
           "offset": 0.0,       // optional zero offset (defaults to 0.0)
           "unit": "kPa"        // engineering/CSV unit ("kPa", "N", or "C")
         }
    2) For Type-K thermocouples, give `"unit": "C"` (or an `id` starting with
       `"T_"`). `convert_stream_values()` automatically applies cold-junction
       compensated `V_to_C()` instead of `scale * V + offset`.
    3) Ensure `"samples_per_packet"` in `"stream"` is >= total sensor count.
    4) `channel_settings`, `convert_stream_values()`, CSV recording headers, and
       WS `init` channel metadata automatically update from `sensors.json`—no
       changes to `instrumentation.py` or `data_recorder.py` are needed.
"""

DEFAULT_SENSORS = load_sensors_config()
_SENSORS_BY_ID = {s.id: s for s in DEFAULT_SENSORS.sensors}

# Module-level compatibility bindings loaded from `contents/config/sensors.json`
#
# Calibration reference notes preserved from bench calibration:
#   - P_* pressures: `2 * 6.895 * <psi_per_v>` converts PSI to kPa (6895 Pa / 1000)
#     with `*2` because transducers are excited on 5V supply rather than 10V.
#   - P_COMB_CHMBR: gain 0.0 (`2 * 6.895 * 0.0`) — TODO: Not in use.
#   - M_RUN_TANK (AIN86, DIFF | X1000): Runtank mass [kg] — SEEMS GOOD. CHECK CAL.
#   - L_THRUST (AIN87, DIFF | X1000): VERY NOISY (offset -25.8 TODO double check).
#   - P_INJECTOR (AIN82, DIFF | X100): GOOD!
scan_frequency = DEFAULT_SENSORS.stream.scan_frequency
resolution_index = DEFAULT_SENSORS.stream.resolution_index
settling_factor = DEFAULT_SENSORS.stream.settling_factor
samples_per_packet = DEFAULT_SENSORS.stream.samples_per_packet
channel_settings = DEFAULT_SENSORS.channel_settings()

GAIN_P_COMB_CHMBR = _SENSORS_BY_ID["P_COMB_CHMBR"].scale  # TODO: Not in use
GAIN_P_N2O_FLOW = _SENSORS_BY_ID[
    "P_N2O_FLOW"
].scale  # [kPa/V] at 5V (2 * 6.895 * 9982.03)
GAIN_P_N2_FLOW = _SENSORS_BY_ID[
    "P_N2_FLOW"
].scale  # [kPa/V] at 5V (2 * 6.895 * 9936.41)
GAIN_P_RUN_TANK = _SENSORS_BY_ID[
    "P_RUN_TANK"
].scale  # [kPa/V] at 5V (2 * 6.895 * 10206.74)
GAIN_P_INJECTOR = _SENSORS_BY_ID[
    "P_INJECTOR"
].scale  # [kPa/V] at 5V (2 * 6.895 * 10170.95)

GAIN_M_RUN_TANK = _SENSORS_BY_ID["M_RUN_TANK"].scale  # [kg/V] at 5V supply
GAIN_L_THRUST = _SENSORS_BY_ID["L_THRUST"].scale  # [N/V] at 5V supply
OFFSET_L_THRUST = _SENSORS_BY_ID["L_THRUST"].offset  # [N] TODO double check
OFFSET_M_RUN_TANK = _SENSORS_BY_ID["M_RUN_TANK"].offset  # [kg] TODO add

CHAN_M_RUN_TANK = _SENSORS_BY_ID["M_RUN_TANK"].ain_key
CHAN_L_THRUST = _SENSORS_BY_ID["L_THRUST"].ain_key
# Backwards-compatible aliases
GAIN_L_RUN_TANK = GAIN_M_RUN_TANK
OFFSET_L_RUN_TANK = OFFSET_M_RUN_TANK
CHAN_L_RUN_TANK = CHAN_M_RUN_TANK
CHAN_P_COMB_CHMBR = _SENSORS_BY_ID["P_COMB_CHMBR"].ain_key
CHAN_P_N2O_FLOW = _SENSORS_BY_ID["P_N2O_FLOW"].ain_key
CHAN_P_N2_FLOW = _SENSORS_BY_ID["P_N2_FLOW"].ain_key
CHAN_P_RUN_TANK = _SENSORS_BY_ID["P_RUN_TANK"].ain_key
CHAN_P_INJECTOR = _SENSORS_BY_ID["P_INJECTOR"].ain_key
CHAN_T_RUN_TANK = _SENSORS_BY_ID["T_RUN_TANK"].ain_key
CHAN_T_INJECTOR = _SENSORS_BY_ID["T_INJECTOR"].ain_key
CHAN_T_COMB_CHMBR = _SENSORS_BY_ID["T_COMB_CHMBR"].ain_key
CHAN_T_POST_COMB = _SENSORS_BY_ID["T_POST_COMB"].ain_key
CHAN_SHUNT = "AIN82"  # Not implemented yet on cart


def _mean(samples: list[float]) -> float:
    return (sum(samples) / len(samples)) if samples else 0.0


def convert_stream_values(
    values: dict[str, list[float]],
    v_ref: float,
    packet_id: int | None = None,
    uptime: float | None = None,
    sensors_config: SensorsConfig | None = None,
) -> dict[str, Any]:
    """
    Convert a processed LabJack stream packet (`values = d.processStreamData(...)`)
    into a flat telemetry sample dictionary driven by `sensors.json`.
    """
    cfg = sensors_config or DEFAULT_SENSORS
    converted: dict[str, Any] = {}
    if packet_id is not None:
        converted["packet_id"] = packet_id
    if uptime is not None:
        rounded_t = round(uptime, 4)
        converted["seconds"] = rounded_t
        converted["uptime"] = rounded_t

    for sensor in cfg.sensors:
        mean_v = _mean(values.get(sensor.ain_key, []))
        if sensor.is_thermocouple:
            converted[sensor.id] = V_to_C(mean_v, v_ref)
        else:
            converted[sensor.id] = mean_v * sensor.scale + sensor.offset

    return converted


def detect_labjack_present() -> bool:
    """
    Return True if `LabJackPython` (`u6`) is installed and a physical
    LabJack U6 device can be opened over USB.
    """
    if u6 is None:
        return False
    dev = None
    try:
        dev = u6.U6()
        return True
    except Exception:
        return False
    finally:
        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass


class LabJackInstrumentation:
    """
    Full-rate LabJack U6 stream reader for `FlowControlWSServer`.

    Writes every packet when recording is active (~83.3 Hz) while decimating
    live WebSocket broadcasts to `broadcast_rate_hz`.
    """

    def __init__(
        self,
        ws_server: FlowControlWSServer,
        reconnect_delay: float = 2.0,
        max_queue_size: int = 512,
        broadcast_rate_hz: float = 20.0,
        sensors_config: SensorsConfig | None = None,
        tc_alert_interval_sec: float = 10.0,
    ):
        self.ws_server = ws_server
        self.reconnect_delay = max(0.5, float(reconnect_delay))
        self.max_queue_size = max(16, int(max_queue_size))
        self.broadcast_rate_hz = max(1.0, float(broadcast_rate_hz))
        self.sensors_config = (
            sensors_config
            or getattr(ws_server, "sensors_config", None)
            or DEFAULT_SENSORS
        )
        self.tc_alert_interval_sec = max(0.0, float(tc_alert_interval_sec))

        self._start_mono: float = time.monotonic()
        self._packet_id: int = 0
        self._last_broadcast_mono: float = 0.0
        self._last_tc_alert_mono: float = 0.0
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._consumer_task: asyncio.Task[Any] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sample_queue: asyncio.Queue[dict[str, Any]] | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the background LabJack stream thread and asyncio consumer."""
        if u6 is None:
            raise RuntimeError("LabJackPython ('u6' module) is not installed.")
        spp = self.sensors_config.stream.samples_per_packet
        n_sensors = len(self.sensors_config.sensors)
        if spp < n_sensors:
            raise ValueError(
                f"samples_per_packet ({spp}) must be at least "
                f"the number of channels ({n_sensors})!"
            )

        self._start_mono = time.monotonic()
        self._last_broadcast_mono = 0.0
        self._last_tc_alert_mono = 0.0
        self._stop_event.clear()
        self._loop = asyncio.get_running_loop()
        self._sample_queue = asyncio.Queue(maxsize=self.max_queue_size)

        if self._consumer_task is None or self._consumer_task.done():
            self._consumer_task = asyncio.create_task(self._consume_loop())

        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(
                target=self._stream_thread_main,
                name="LabJackU6StreamThread",
                daemon=True,
            )
            self._thread.start()

    async def stop(self) -> None:
        """Stop the LabJack stream thread and drain/cancel the consumer task."""
        self._stop_event.set()

        if self._thread is not None and self._thread.is_alive():
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._thread.join, 3.0)
        self._thread = None

        if self._consumer_task is not None:
            if not self._consumer_task.done():
                self._consumer_task.cancel()
            await asyncio.gather(self._consumer_task, return_exceptions=True)
            self._consumer_task = None

    # ------------------------------------------------------------------
    # Cross-Thread Sample, Thermocouple Fault & Decimated Broadcasts
    # ------------------------------------------------------------------
    def find_faulted_thermocouples(self, sample: dict[str, Any]) -> list[str]:
        """Return thermocouple channel IDs whose converted value is NaN."""
        return [
            s.id
            for s in self.sensors_config.sensors
            if s.is_thermocouple
            and isinstance(sample.get(s.id), float)
            and math.isnan(sample[s.id])
        ]

    async def _maybe_emit_tc_fault_alert(
        self, sample: dict[str, Any], now_mono: float
    ) -> None:
        faulted = self.find_faulted_thermocouples(sample)
        if not faulted:
            return
        if (
            self._last_tc_alert_mono == 0.0
            or (now_mono - self._last_tc_alert_mono) >= self.tc_alert_interval_sec
        ):
            self._last_tc_alert_mono = now_mono
            await self.ws_server.send_alert(
                "WARN",
                f"Thermocouple out-of-range / open-circuit on: {', '.join(faulted)}",
            )

    async def process_sample(
        self, sample: dict[str, Any], now_mono: float | None = None
    ) -> bool:
        """
        Record full-rate packets on disk, emit rate-limited thermocouple fault
        alerts, and decimate WebSocket broadcasts to `broadcast_rate_hz`.
        """
        now = time.monotonic() if now_mono is None else float(now_mono)
        if self.ws_server.is_recording:
            self.ws_server.record_telemetry(sample)

        await self._maybe_emit_tc_fault_alert(sample, now)

        min_interval = 1.0 / self.broadcast_rate_hz
        if self.ws_server.clients and (
            self._last_broadcast_mono == 0.0
            or (now - self._last_broadcast_mono) >= min_interval
        ):
            self._last_broadcast_mono = now
            await self.ws_server.broadcast_telemetry(sample, record=False)
            return True
        return False

    def _enqueue_sample_on_loop(self, sample: dict[str, Any]) -> None:
        if self._sample_queue is None:
            return
        if self._sample_queue.full():
            try:
                self._sample_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._sample_queue.put_nowait(sample)
        except asyncio.QueueFull:
            pass

    def _emit_alert_threadsafe(self, level: str, message: str) -> None:
        if self._loop is None or self._loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(
                self.ws_server.send_alert(level, message),
                self._loop,
            )
        except Exception:
            pass

    async def _consume_loop(self) -> None:
        assert self._sample_queue is not None
        try:
            while True:
                sample = await self._sample_queue.get()
                await self.process_sample(sample)
        except asyncio.CancelledError:
            logger.debug("LabJack telemetry consumer loop stopped.")
            raise

    # ------------------------------------------------------------------
    # Dedicated LabJack U6 USB Stream Thread
    # ------------------------------------------------------------------
    def _open_and_configure_device(self) -> tuple[Any, float]:
        d = u6.U6()
        # Ensure any previous leftover stream is stopped before configuring
        try:
            d.streamStop()
        except Exception:
            pass

        stream_cfg = self.sensors_config.stream
        settings = self.sensors_config.channel_settings()
        d.streamConfig(
            ScanFrequency=stream_cfg.scan_frequency,
            ChannelNumbers=[x[0] for x in settings],
            ChannelOptions=[x[1] for x in settings],
            NumChannels=len(settings),
            ResolutionIndex=stream_cfg.resolution_index,
            SettlingFactor=stream_cfg.settling_factor,
            SamplesPerPacket=stream_cfg.samples_per_packet,
        )

        t_ref_k = d.getTemperature()
        v_ref = get_ref_voltage(t_ref_k)
        logger.info(
            "LabJack U6 configured (scan=%d Hz, channels=%d, spp=%d, "
            "T_ref=%.2f K / %.2f °C, V_ref=%.6f V)",
            stream_cfg.scan_frequency,
            len(settings),
            stream_cfg.samples_per_packet,
            t_ref_k,
            t_ref_k - 273.15,
            v_ref,
        )
        return d, v_ref

    def _stream_device_packets(self, device: Any, v_ref: float) -> None:
        for reading in device.streamData(convert=False):
            if self._stop_event.is_set():
                break
            if not reading or not reading.get("result"):
                continue

            if reading.get("errors", 0) or reading.get("missed", 0):
                logger.debug(
                    "LabJack stream packet diagnostic: errors=%s, missed=%s",
                    reading.get("errors"),
                    reading.get("missed"),
                )

            values = device.processStreamData(reading["result"])
            self._packet_id += 1
            uptime = time.monotonic() - self._start_mono
            sample = convert_stream_values(
                values=values,
                v_ref=v_ref,
                packet_id=self._packet_id,
                uptime=uptime,
                sensors_config=self.sensors_config,
            )

            if self._loop is not None and not self._loop.is_closed():
                self._loop.call_soon_threadsafe(self._enqueue_sample_on_loop, sample)

    @staticmethod
    def _close_device(device: Any | None, stream_started: bool) -> None:
        if device is None:
            return
        if stream_started:
            try:
                device.streamStop()
                logger.info("LabJack U6 stream stopped.")
            except Exception:
                pass
        try:
            device.close()
        except Exception:
            pass

    def _stream_thread_main(self) -> None:
        had_failure = False
        while not self._stop_event.is_set():
            device = None
            stream_started = False
            try:
                device, v_ref = self._open_and_configure_device()
                device.streamStart()
                stream_started = True
                if had_failure:
                    had_failure = False
                    self._emit_alert_threadsafe(
                        "INFO", "LabJack U6 telemetry stream reconnected."
                    )
                self._stream_device_packets(device, v_ref)
            except Exception as exc:
                if self._stop_event.is_set():
                    break
                logger.error("LabJack U6 stream error: %s", exc)
                if not had_failure:
                    had_failure = True
                    self._emit_alert_threadsafe(
                        "WARN",
                        f"LabJack U6 stream interrupted ({exc}); "
                        f"retrying in {self.reconnect_delay:.1f}s...",
                    )
                self._stop_event.wait(self.reconnect_delay)
            finally:
                self._close_device(device, stream_started)


if __name__ == "__main__":
    import json

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if u6 is None:
        raise SystemExit(
            "Error: LabJackPython ('u6') is not installed in this environment."
        )

    dev = u6.U6()
    try:
        dev.streamStop()
    except Exception:
        pass

    dev.streamConfig(
        ScanFrequency=scan_frequency,
        ChannelNumbers=[x[0] for x in channel_settings],
        ChannelOptions=[x[1] for x in channel_settings],
        NumChannels=len(channel_settings),
        ResolutionIndex=resolution_index,
        SettlingFactor=settling_factor,
        SamplesPerPacket=samples_per_packet,
    )
    t_ref = dev.getTemperature()
    v_ref_val = get_ref_voltage(t_ref)
    print("T_ref in K: ", t_ref)
    print("V_ref in V: ", v_ref_val)

    dev.streamStart()
    start_t = time.monotonic()
    pkt = 0
    try:
        for r in dev.streamData(convert=False):
            if r is not None and r.get("result"):
                pkt += 1
                vals = dev.processStreamData(r["result"])
                converted = convert_stream_values(
                    vals, v_ref_val, pkt, time.monotonic() - start_t
                )
                print(json.dumps(converted))
    except KeyboardInterrupt:
        print("Interrupt signal received!")
    finally:
        try:
            dev.streamStop()
            print("Stream stopped.")
        finally:
            dev.close()
