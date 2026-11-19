#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LabJack U6 Telemetry Acquisition (`instrumentation.py`) for
Flow-Control-System-Server.

Streams 11 analog channels (5 pressures in kPa, 2 load cells in N,
4 Type-K thermocouples in °C) from a LabJack U6 at full packet rate
(~83.3 Hz at 1000 Hz scan frequency / 12 samples per packet) in a dedicated
background thread and forwards every packet directly to `FlowControlWSServer`
for on-cart CSV recording and live WebSocket broadcast.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import TYPE_CHECKING, Any

from thermocouple import V_to_C, V_to_K, get_ref_voltage

try:
    import u6  # type: ignore
except ImportError:
    u6 = None  # type: ignore

if TYPE_CHECKING:
    from ws_server import FlowControlWSServer

logger = logging.getLogger("fcs.instrumentation")

# Gains
X1    = 0b00000000
X10   = 0b00010000
X100  = 0b00100000
X1000 = 0b00110000

# Channel mode
SING = 0b00000000
DIFF = 0b10000000


'''
NOTE:
  Only change the values between
  ######### BEGIN USER ADJUSTABLE ########

  #########  END USER ADJUSTABLE  ########

  (If you add a new sensor you will need to adjust other code too)


Overview:

  LabJackPython provides wraps the low level functions for communicating
  with the labjack so it can be used with python. All of the low level
  functions are discussed in section 5.2 of the U6 user guide.

  This file sets up the LJ in stream mode to acquire data at the fastest
  rate possible. All channels and their settings for each sensor are 
  configured here. Data produced by the LJ is consumed and streamed to
  the WebSocket server and on-cart session recorder.


Sensor Overview:

  Both the CTL-190 and HKM-375 are absolute pressure transducers that have
  a supply voltage of 10VDC, output impedance of 1000 ohms and a differential
  output with full scale range of 100mV.

  All thermocouples used are K-type.

  Load cells used are typical bridge sensors with low output impedance and
  a very small full scale range. Supplied with 5VDC.


streamConfig():

  Streaming data from the LJ is the fastest way to acquire data.
  See section 5.2.12 for StreamConfig documentation for details.

  samples_per_packet:
    The number of samples in each packet sent over USB by the LJ. If the
    number of channels being scanned is low, then this number should be
    set lower than the default of 25, otherwise the packet frequency will
    be lower than desirable. If the number of channels is high then the number
    should be left at 25 or increased.

  scan_frequency:
    The frequency in Hz to scan the channel list (ChannelNumbers). 
    The sample rate (Hz) = scan_frequency * num_channels.

  num_channels:
    Number of channels to stream. Equal to length of channel_numbers.

  resolution_index:
    See section 3.1. Higher values increase ENOB but at the expense
    of longer sample times.

  settling_factor:
    For high impedance sensors, a longer settling time is required to acquire
    enough charge in the ADC to get an accurate reading. LJ says that the
    auto setting is good for any sensor with at most 1000 ohm output 
    impedance. 
    See https://support.labjack.com/docs/analog-input-settling-time-app-note
 
  channel_settings[]:
    Channels to read from and their settings. For differential pairs, only
    specify the positive channel (even numbered). OR a channel mode and 
    gain value together to conveniently set the options for that channel.

    Each channel has options that can be set with the ChannelOptions byte.
    These options should be configured for each type of sensor to account
    for their output impedance, fullscale range etc. Each bit of the byte
    configures a different setting:

      Set bit 4-5 = GainIndex:
        GainIndex   Gain   Max V    Min V
        b00         1      10.1     -10.6
        b01         10     1.01     -1.06
        b10         100    0.101    -0.106
        b11         1000   0.0101   -0.0106
    
      Set bit 7 for differential reading.


Adding a Sensor: 
    1) Add a new tuple to channel_settings with the LJ pin the sensor is
       connected to. For single ended sensors, this is just the single
       sensor pin. For differential, it is the positive channel. LJ will
       automatically configure the negative channel.

       The tuple should have the form: (CHAN_NUM, SETTINGS)
       Ex: Channel 0 single ended with gain 10:  (0, SING | X10)
           Channel 2,3 differential with gain 1: (2, DIFF | X1)

    2) Add a new GAIN variable and OFFSET variable if needed that converts
       the raw voltage from the sensor to the appropriate unit.

    3) Add a new variable (called CHAN_XXX where XXX refers to the sensor name)
       to refer to the pin the sensor is connected to. Again for single ended
       just use the pin, and differential the positive channel. This is used
       for lookup in the LJ results dictionary. The LJ naming convention is 
       'AINX' where X is the pin number (1, 2, .. 56 etc)

    4) Add a few lines of code to read and convert the voltage in
       `convert_reading()`:

       if reading is not None:

           # Add and fill out this line
           SENSOR_NAME = values[CHAN_XXX]
           converted['SENSOR_NAME'] = GAIN*SENSOR_NAME + OFFSET # etc...

        Thats it, Have fun with your new sensor!
    
'''

######### BEGIN USER ADJUSTABLE #########

# Stream settings
scan_frequency   = 1000
resolution_index = 2
settling_factor  = 2
samples_per_packet = 12
channel_settings = [(86, DIFF | X1000), # L_RUN_TANK (SEEMS GOOD. CHECK CAL)
                    (87, DIFF | X1000), # L_THRUST (VERY NOISY)

                    (82, DIFF | X100),  # P_INJECTOR (GOOD!)
                    (85, DIFF | X100),  # P_COMB_CHMBR
                    (84, DIFF | X100),  # P_N2O_FLOW
                    (83, DIFF | X100),  # P_N2_FLOW
                    (81, DIFF | X100),  # P_RUN_TANK
                    
                    (55, SING | X100),  # T_COMB_CHMBR
                    (53, SING | X100),  # T_POST_COMB_CHMBR
                    (57, SING | X100),  # T_INJECTOR
                    (49, SING | X100)]  # T_RUN_TANK 

# These gains and offsets convert raw volts to target engineering units
# (Pressures in kPa, Loads in N, Temperatures in °C)

# 6.895 converts PSI to kPa (6895 Pa / 1000). *2 since running on 5V supply rather than 10V
GAIN_P_COMB_CHMBR = 2 * 6.895 * 0.0       # [kPa/V] at 10V supply TODO: Not in use
GAIN_P_N2O_FLOW   = 2 * 6.895 * 9982.03   # [kPa/V] at 10V supply 
GAIN_P_N2_FLOW    = 2 * 6.895 * 9936.41   # [kPa/V] at 10V supply
GAIN_P_RUN_TANK   = 2 * 6.895 * 10206.74  # [kPa/V] at 10V supply
GAIN_P_INJECTOR   = 2 * 6.895 * 10170.95  # [kPa/V] at 10V supply

GAIN_L_RUN_TANK   = 2014.22   # [N/V]  at 5V supply
GAIN_L_THRUST     = 466000    # [N/V]  at 5V supply 

OFFSET_L_THRUST   = -25.8     # [N] # TODO double check
OFFSET_L_RUN_TANK = -5.8      # [N] # TODO add 

# Channel mapping from LJ to sensor
CHAN_L_RUN_TANK   = 'AIN86'
CHAN_L_THRUST     = 'AIN87'

CHAN_P_COMB_CHMBR = 'AIN85'
CHAN_P_N2O_FLOW   = 'AIN84'
CHAN_P_N2_FLOW    = 'AIN83'
CHAN_P_RUN_TANK   = 'AIN81'
CHAN_P_INJECTOR   = 'AIN82'

CHAN_T_RUN_TANK   = 'AIN49'
CHAN_T_INJECTOR   = 'AIN57'
CHAN_T_COMB_CHMBR = 'AIN55'
CHAN_T_POST_COMB  = 'AIN53'

CHAN_SHUNT        = 'AIN82' # Not implemented yet on cart

#########  END USER ADJUSTABLE  #########


def _mean(samples: list[float]) -> float:
    return (sum(samples) / len(samples)) if samples else 0.0


def convert_stream_values(
    values: dict[str, list[float]],
    v_ref: float,
    packet_id: int | None = None,
    uptime: float | None = None,
) -> dict[str, Any]:
    """
    Convert a processed LabJack stream packet (`values = d.processStreamData(...)`)
    into a flat telemetry sample dictionary with pressures in kPa, loads in N,
    and temperatures in °C.
    """
    P_INJECTOR   = values[CHAN_P_INJECTOR]
    P_COMB_CHMBR = values[CHAN_P_COMB_CHMBR]
    P_N2O_FLOW   = values[CHAN_P_N2O_FLOW]
    P_N2_FLOW    = values[CHAN_P_N2_FLOW]
    P_RUN_TANK   = values[CHAN_P_RUN_TANK]

    L_RUN_TANK   = values[CHAN_L_RUN_TANK]
    L_THRUST     = values[CHAN_L_THRUST]

    T_RUN_TANK   = values[CHAN_T_RUN_TANK]
    T_INJECTOR   = values[CHAN_T_INJECTOR]
    T_COMB_CHMBR = values[CHAN_T_COMB_CHMBR]
    T_POST_COMB  = values[CHAN_T_POST_COMB]

    converted: dict[str, Any] = {}
    if packet_id is not None:
        converted["packet_id"] = packet_id
    if uptime is not None:
        rounded_t = round(uptime, 4)
        converted["seconds"] = rounded_t
        converted["uptime"] = rounded_t

    # Pressures [kPa]
    converted["P_INJECTOR"] = _mean(P_INJECTOR) * GAIN_P_INJECTOR
    converted["P_COMB_CHMBR"] = _mean(P_COMB_CHMBR) * GAIN_P_COMB_CHMBR
    converted["P_N2O_FLOW"] = _mean(P_N2O_FLOW) * GAIN_P_N2O_FLOW
    converted["P_N2_FLOW"] = _mean(P_N2_FLOW) * GAIN_P_N2_FLOW
    converted["P_RUN_TANK"] = _mean(P_RUN_TANK) * GAIN_P_RUN_TANK

    # Load cells [N]
    converted["L_RUN_TANK"] = (
        _mean(L_RUN_TANK) * GAIN_L_RUN_TANK + OFFSET_L_RUN_TANK
    )
    converted["L_THRUST"] = (
        _mean(L_THRUST) * GAIN_L_THRUST + OFFSET_L_THRUST
    )

    # Thermocouples [°C]
    converted["T_RUN_TANK"] = V_to_C(_mean(T_RUN_TANK), v_ref)
    converted["T_INJECTOR"] = V_to_C(_mean(T_INJECTOR), v_ref)
    converted["T_COMB_CHMBR"] = V_to_C(_mean(T_COMB_CHMBR), v_ref)
    converted["T_POST_COMB"] = V_to_C(_mean(T_POST_COMB), v_ref)

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

    Runs the synchronous `u6.U6.streamData(convert=False)` loop in a dedicated
    background thread and forwards every converted sample packet directly to
    the asyncio event loop for CSV recording and WebSocket broadcast.
    """

    def __init__(
        self,
        ws_server: FlowControlWSServer,
        reconnect_delay: float = 2.0,
        max_queue_size: int = 512,
    ):
        self.ws_server = ws_server
        self.reconnect_delay = max(0.5, float(reconnect_delay))
        self.max_queue_size = max(16, int(max_queue_size))

        self._start_mono: float = time.monotonic()
        self._packet_id: int = 0
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
            raise RuntimeError(
                "LabJackPython ('u6' module) is not installed."
            )
        if samples_per_packet < len(channel_settings):
            raise ValueError(
                f"samples_per_packet ({samples_per_packet}) must be at least "
                f"the number of channels ({len(channel_settings)})!"
            )

        self._start_mono = time.monotonic()
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
    # Cross-Thread Sample & Alert Forwarding
    # ------------------------------------------------------------------
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
                if self.ws_server.clients or self.ws_server.is_recording:
                    await self.ws_server.broadcast_telemetry(sample)
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

        d.streamConfig(
            ScanFrequency=scan_frequency,
            ChannelNumbers=[x[0] for x in channel_settings],
            ChannelOptions=[x[1] for x in channel_settings],
            NumChannels=len(channel_settings),
            ResolutionIndex=resolution_index,
            SettlingFactor=settling_factor,
            SamplesPerPacket=samples_per_packet,
        )

        t_ref_k = d.getTemperature()
        v_ref = get_ref_voltage(t_ref_k)
        logger.info(
            "LabJack U6 configured (scan=%d Hz, channels=%d, spp=%d, "
            "T_ref=%.2f K / %.2f °C, V_ref=%.6f V)",
            scan_frequency,
            len(channel_settings),
            samples_per_packet,
            t_ref_k,
            t_ref_k - 273.15,
            v_ref,
        )
        return d, v_ref

    def _stream_thread_main(self) -> None:
        had_failure = False
        while not self._stop_event.is_set():
            d = None
            stream_started = False
            try:
                d, v_ref = self._open_and_configure_device()
                d.streamStart()
                stream_started = True

                if had_failure:
                    had_failure = False
                    self._emit_alert_threadsafe(
                        "INFO", "LabJack U6 telemetry stream reconnected."
                    )

                for reading in d.streamData(convert=False):
                    if self._stop_event.is_set():
                        break
                    if reading is None:
                        continue

                    if reading.get("errors", 0) or reading.get("missed", 0):
                        logger.debug(
                            "LabJack stream packet diagnostic: "
                            "errors=%s, missed=%s",
                            reading.get("errors"),
                            reading.get("missed"),
                        )

                    result = reading.get("result")
                    if not result:
                        continue

                    values = d.processStreamData(result)
                    self._packet_id += 1
                    uptime = time.monotonic() - self._start_mono

                    sample = convert_stream_values(
                        values=values,
                        v_ref=v_ref,
                        packet_id=self._packet_id,
                        uptime=uptime,
                    )

                    if self._loop is not None and not self._loop.is_closed():
                        self._loop.call_soon_threadsafe(
                            self._enqueue_sample_on_loop, sample
                        )

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
                if d is not None:
                    if stream_started:
                        try:
                            d.streamStop()
                            logger.info("LabJack U6 stream stopped.")
                        except Exception:
                            pass
                    try:
                        d.close()
                    except Exception:
                        pass


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
