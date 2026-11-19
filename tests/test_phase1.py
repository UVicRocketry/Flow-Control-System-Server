"""Phase 1 unit tests for Flow-Control-System-Server core helpers."""

import importlib.util
import math
import sys
import types
import unittest
from pathlib import Path

CODE_DIR = str(Path(__file__).resolve().parents[1] / "contents" / "code")
if CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)

if importlib.util.find_spec("websockets") is None:
    _ws_exc = types.ModuleType("websockets.exceptions")
    _ws_exc.ConnectionClosed = ConnectionError
    _ws_srv = types.ModuleType("websockets.asyncio.server")
    _ws_srv.broadcast = lambda *args, **kwargs: None
    _ws_srv.serve = lambda *args, **kwargs: None
    _ws_asyncio = types.ModuleType("websockets.asyncio")
    _ws_asyncio.server = _ws_srv
    _ws_pkg = types.ModuleType("websockets")
    _ws_pkg.asyncio = _ws_asyncio
    _ws_pkg.exceptions = _ws_exc
    sys.modules.update(
        {
            "websockets": _ws_pkg,
            "websockets.asyncio": _ws_asyncio,
            "websockets.asyncio.server": _ws_srv,
            "websockets.exceptions": _ws_exc,
        }
    )

import mocks  # noqa: E402
import thermocouple  # noqa: E402
import ws_server  # noqa: E402


class TestWsServerHelpers(unittest.TestCase):
    """Tests filename sanitization and valve state normalization helpers."""

    def test_sanitize_filename_valid_and_traversal_cases(self) -> None:
        self.assertEqual(ws_server.sanitize_filename(" hotfire_01.csv "), "hotfire_01.csv")
        self.assertIsNone(ws_server.sanitize_filename(None))
        self.assertIsNone(ws_server.sanitize_filename(""))
        self.assertIsNone(ws_server.sanitize_filename("   "))
        self.assertIsNone(ws_server.sanitize_filename("../secret.csv"))
        self.assertIsNone(ws_server.sanitize_filename("sub/dir.csv"))
        self.assertIsNone(ws_server.sanitize_filename(r"sub\dir.csv"))
        self.assertIsNone(ws_server.sanitize_filename("."))
        self.assertIsNone(ws_server.sanitize_filename(".."))

    def test_normalize_valve_state(self) -> None:
        self.assertEqual(ws_server.normalize_valve_state(" close "), "CLOSED")
        self.assertEqual(ws_server.normalize_valve_state("transit"), "ACTUATING")
        self.assertEqual(ws_server.normalize_valve_state("filling"), "FILL")
        self.assertEqual(ws_server.normalize_valve_state("venting"), "VENT")
        self.assertEqual(ws_server.normalize_valve_state("connect"), "CONNECTED")
        self.assertEqual(ws_server.normalize_valve_state("disconnect"), "DISCONNECTED")
        self.assertEqual(ws_server.normalize_valve_state("fire"), "FIRED")
        self.assertEqual(ws_server.normalize_valve_state("custom_state"), "CUSTOM_STATE")
        self.assertEqual(ws_server.normalize_valve_state(""), "")


class TestThermocoupleConversion(unittest.TestCase):
    """Tests NIST ITS-90 Type-K thermocouple voltage conversion helpers."""

    def test_zero_celsius_reference_voltage_is_near_zero(self) -> None:
        v_ref = thermocouple.get_ref_voltage(273.15)
        self.assertAlmostEqual(v_ref, -1.7600413686e-5, places=10)

    def test_v_to_c_and_v_to_k_around_room_and_boiling(self) -> None:
        v_ref_room = thermocouple.get_ref_voltage(298.15)
        temp_c = thermocouple.V_to_C(0.0, v_ref_room)
        temp_k = thermocouple.V_to_K(0.0, v_ref_room)
        self.assertAlmostEqual(temp_c, 24.1312, places=3)
        self.assertAlmostEqual(temp_k, temp_c + 273.15, places=6)

        # ~4.096 mV corresponds to ~100 °C on Type-K relative to 0 °C cold junction
        t_100c = thermocouple.V_to_C(0.004096, 0.0)
        self.assertAlmostEqual(t_100c, 100.0, delta=0.5)
        self.assertTrue(math.isfinite(t_100c))

    def test_out_of_range_voltages_return_zero(self) -> None:
        self.assertTrue(math.isnan(thermocouple.V_to_C(-0.02, 0.0)))
        self.assertTrue(math.isnan(thermocouple.V_to_K(0.10, 0.0)))


class TestMockStateTransitions(unittest.TestCase):
    """Tests mock command action-to-state mapping."""

    def test_action_to_target_state(self) -> None:
        self.assertEqual(mocks.action_to_target_state("close"), "CLOSED")
        self.assertEqual(mocks.action_to_target_state(" disconnect "), "DISCONNECTED")
        self.assertEqual(mocks.action_to_target_state("FIRE"), "FIRED")
        self.assertEqual(mocks.action_to_target_state("open"), "OPEN")
        self.assertEqual(mocks.action_to_target_state(""), "")


if __name__ == "__main__":
    unittest.main()
