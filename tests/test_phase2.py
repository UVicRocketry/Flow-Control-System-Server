"""Phase 2 tests for sensors config, data recorder, thermocouple faults, and FastAPI server."""

import importlib.util
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

CODE_DIR = str(Path(__file__).resolve().parents[1] / "contents" / "code")
if CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)

import instrumentation  # noqa: E402
import thermocouple  # noqa: E402
import ws_server  # noqa: E402
from data_recorder import DataRecorder  # noqa: E402
from sensors_config import (  # noqa: E402
    GAIN_BITMASKS,
    MODE_BITMASKS,
    SensorConfigValidationError,
    load_sensors_config,
    validate_sensors_payload,
)


class _RecordingClient:
    """In-memory WebSocket client stand-in that captures sent JSON frames."""

    def __init__(self) -> None:
        self.messages: list[dict] = []
        self.remote_address = ("127.0.0.1", 50000)

    async def send(self, raw: str) -> None:
        self.messages.append(json.loads(raw))

    async def close(self) -> None:
        return None


class _DummySerialController:
    """Captures serial_controller hook calls preserving docs/actuation-spec.md."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, str]] = []
        self.abort_count: int = 0
        self.unabort_count: int = 0
        self.summary_count: int = 0

    async def send_valve_command(self, valve: str, action: str) -> None:
        self.commands.append((valve, action))

    async def send_abort(self) -> None:
        self.abort_count += 1

    async def send_unabort(self) -> None:
        self.unabort_count += 1

    async def request_summary(self) -> None:
        self.summary_count += 1


class TestSensorsConfig(unittest.TestCase):
    """Tests Pydantic/fallback validation of sensors.json and stream conversion."""

    def test_default_sensors_json_and_headers(self) -> None:
        cfg = load_sensors_config()
        self.assertEqual(len(cfg.sensors), 11)
        self.assertEqual(cfg.csv_channels()[-1], "seconds")
        self.assertEqual(len(cfg.csv_channels()), 12)
        self.assertEqual(cfg.csv_units_row(), ws_server.CSV_UNITS_ROW)
        self.assertEqual(cfg.csv_channels_row(), ws_server.CSV_CHANNELS_ROW)

        settings = cfg.channel_settings()
        self.assertEqual(settings[5], (86, MODE_BITMASKS["DIFF"] | GAIN_BITMASKS[1000]))
        self.assertEqual(settings[7], (49, MODE_BITMASKS["SING"] | GAIN_BITMASKS[100]))

        meta = cfg.ws_channels_metadata()
        self.assertEqual(meta["P_RUN_TANK"], {"unit": "kPa", "pin": "AIN81"})
        self.assertEqual(meta["M_RUN_TANK"], {"unit": "kg", "pin": "AIN86"})
        self.assertEqual(meta["T_RUN_TANK"], {"unit": "C", "pin": "AIN49"})
        for forbidden in (
            "title",
            "color",
            "fallback_color",
            "type",
            "sensor_type",
            "csv_unit",
        ):
            self.assertNotIn(forbidden, meta["P_RUN_TANK"])

        raw_sensors = json.loads(
            (
                Path(__file__).resolve().parents[1]
                / "contents"
                / "config"
                / "sensors.json"
            ).read_text(encoding="utf-8")
        )["sensors"]
        for entry in raw_sensors:
            for forbidden in (
                "title",
                "color",
                "fallback_color",
                "sensor_type",
                "csv_unit",
            ):
                self.assertNotIn(forbidden, entry)

    def test_convert_stream_values_uses_sensors_config(self) -> None:
        cfg = load_sensors_config()
        v_ref = thermocouple.get_ref_voltage(273.15)
        raw = {s.ain_key: [0.0] for s in cfg.sensors}
        raw["AIN86"] = [0.005]
        raw["AIN55"] = [0.004096]
        sample = instrumentation.convert_stream_values(
            raw, v_ref=v_ref, packet_id=7, uptime=1.23456, sensors_config=cfg
        )
        self.assertEqual(sample["packet_id"], 7)
        self.assertEqual(sample["seconds"], 1.2346)
        self.assertAlmostEqual(sample["M_RUN_TANK"], 0.005 * 2014.22 - 5.8, places=6)
        self.assertAlmostEqual(sample["T_COMB_CHMBR"], 100.0, delta=0.6)

    def test_invalid_sensor_configs_raise_validation_error(self) -> None:
        base = load_sensors_config().model_dump()

        bad_gain = json.loads(json.dumps(base))
        bad_gain["sensors"][0]["gain_index"] = 50
        with self.assertRaises((SensorConfigValidationError, ValueError)):
            validate_sensors_payload(bad_gain)

        bad_mode = json.loads(json.dumps(base))
        bad_mode["sensors"][0]["mode"] = "PSEUDO"
        with self.assertRaises((SensorConfigValidationError, ValueError)):
            validate_sensors_payload(bad_mode)

        dup_id = json.loads(json.dumps(base))
        dup_id["sensors"][1]["id"] = dup_id["sensors"][0]["id"]
        with self.assertRaises((SensorConfigValidationError, ValueError)):
            validate_sensors_payload(dup_id)

        bad_spp = json.loads(json.dumps(base))
        bad_spp["stream"]["samples_per_packet"] = 2
        with self.assertRaises((SensorConfigValidationError, ValueError)):
            validate_sensors_payload(bad_spp)


class TestThermocoupleFaultAndDecimation(unittest.IsolatedAsyncioTestCase):
    """Tests NaN thermocouple faults, rate-limited alerts, full-rate disk vs decimated WS."""

    async def test_open_tc_nan_full_rate_disk_and_decimated_ws(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            server = ws_server.FlowControlWSServer(data_dir=tmpdir, armed=False)
            client = _RecordingClient()
            server.clients.add(client)

            inst = instrumentation.LabJackInstrumentation(
                ws_server=server,
                broadcast_rate_hz=10.0,
                tc_alert_interval_sec=5.0,
            )
            server.data_recorder.start_recording("hotfire_test.csv")

            timestamps = [1.00, 1.02, 1.04, 1.06, 1.15, 6.50]
            sent_flags = []
            for idx, ts in enumerate(timestamps):
                sample = {
                    "packet_id": idx + 1,
                    "seconds": ts,
                    "uptime": ts,
                    "P_INJECTOR": 500.0 + idx,
                    "T_RUN_TANK": math.nan,
                    "T_INJECTOR": 21.0,
                }
                sent_flags.append(await inst.process_sample(sample, now_mono=ts))

            server.data_recorder.stop_recording()

            # 10 Hz -> 0.1s min interval: 1.00, 1.15, and 6.50 broadcast; intermediates skipped
            self.assertEqual(sent_flags, [True, False, False, False, True, True])
            telemetry_msgs = [
                m for m in client.messages if m.get("type") == "telemetry"
            ]
            self.assertEqual(len(telemetry_msgs), 3)
            self.assertIsNone(telemetry_msgs[0]["data"]["T_RUN_TANK"])

            # Warn alert emitted once at t=1.00 and again at t=6.50 (>= 5s cooldown)
            warn_msgs = [
                m
                for m in client.messages
                if m.get("type") == "alert" and "T_RUN_TANK" in m.get("message", "")
            ]
            self.assertEqual(len(warn_msgs), 2)

            # All 6 full-rate packets recorded on disk with empty cell for NaN
            csv_lines = (
                (Path(tmpdir) / "hotfire_test.csv")
                .read_text(encoding="utf-8")
                .splitlines()
            )
            self.assertEqual(len(csv_lines), 8)  # 2 headers + 6 samples
            t_run_idx = list(server.sensors_config.csv_channels()).index("T_RUN_TANK")
            row_vals = csv_lines[2].split(",")
            self.assertEqual(row_vals[t_run_idx], "")


class TestServerInterlocksAndInit(unittest.IsolatedAsyncioTestCase):
    """Tests WS init channel metadata, safety interlocks, and ABORT bypass."""

    async def test_init_contains_channels_and_interlocks_enforce_safety(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            ctrl = _DummySerialController()
            server = ws_server.FlowControlWSServer(
                armed=False, data_dir=tmpdir, serial_controller=ctrl
            )
            client = _RecordingClient()
            server.clients.add(client)

            init_payload = server.build_init_payload()
            self.assertIn("channels", init_payload)
            self.assertIn("P_RUN_TANK", init_payload["channels"])

            # Blocked in SAFE mode
            await server.handle_message(
                client,
                json.dumps({"type": "command", "valve": "N2OV", "action": "OPEN"}),
            )
            self.assertEqual(ctrl.commands, [])

            # ABORT bypasses SAFE mode
            await server.handle_message(
                client, json.dumps({"type": "abort", "command": "ABORT"})
            )
            self.assertEqual(ctrl.abort_count, 1)

            # Arm and verify external + single_use interlocks
            await server.set_armed(True)
            await server.handle_message(
                client,
                json.dumps({"type": "command", "valve": "MRV", "action": "FIRE"}),
            )
            self.assertEqual(ctrl.commands, [])

            await server.handle_message(
                client,
                json.dumps({"type": "command", "valve": "QD", "action": "DISCONNECT"}),
            )
            self.assertEqual(ctrl.commands, [("QD", "DISCONNECT")])

            await server.update_valve_state("QD", "DISCONNECTED")
            await server.handle_message(
                client,
                json.dumps({"type": "command", "valve": "QD", "action": "CONNECT"}),
            )
            self.assertEqual(ctrl.commands, [("QD", "DISCONNECT")])


class TestDataRecorderAndHttpApi(unittest.IsolatedAsyncioTestCase):
    """Tests DataRecorder safe CRUD and FastAPI `/api/data` + `/ws` routes."""

    async def test_data_recorder_crud_and_guards(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            server = ws_server.FlowControlWSServer(data_dir=tmpdir)
            recorder: DataRecorder = server.data_recorder

            with self.assertRaises(ValueError):
                recorder.resolve_download_path("../escape.csv")

            up = await server.api_upload_data_file(
                "run_01.csv", "kPa,s\nP_RUN_TANK,seconds\n100,0.0\n"
            )
            self.assertEqual(up["filename"], "run_01.csv")
            self.assertIn("run_01.csv", server.api_list_data_files()["files"])

            renamed = await server.api_rename_data_file(
                "run_01.csv", "run_01_renamed.csv"
            )
            self.assertEqual(renamed["new_name"], "run_01_renamed.csv")
            self.assertIn("run_01_renamed.csv", server.api_list_data_files()["files"])

            # Active recording protections and pull flush
            recorder.start_recording("live.csv")
            recorder.write_sample({"seconds": 0.5, "P_RUN_TANK": 321.5})
            live_path = server.api_resolve_download("", mode="pull_recording")
            self.assertIn("321.5000", live_path.read_text(encoding="utf-8"))

            with self.assertRaises(PermissionError):
                await server.api_upload_data_file("live.csv", "overwrite")
            with self.assertRaises(PermissionError):
                await server.api_delete_data_file("live.csv")

            recorder.stop_recording()
            await server.api_delete_data_file("run_01_renamed.csv")
            self.assertNotIn(
                "run_01_renamed.csv", server.api_list_data_files()["files"]
            )

    @unittest.skipUnless(
        ws_server.HAS_FASTAPI and importlib.util.find_spec("httpx") is not None,
        "fastapi/httpx not installed locally",
    )
    def test_fastapi_http_and_ws_endpoints(self) -> None:
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as tmpdir:
            ctrl = _DummySerialController()
            server = ws_server.FlowControlWSServer(
                armed=True, data_dir=tmpdir, serial_controller=ctrl
            )
            client = TestClient(server.create_app())

            put_resp = client.put(
                "/api/data/pad_test.csv",
                content=b"kPa,s\nP_RUN_TANK,seconds\n250,1.0\n",
            )
            self.assertEqual(put_resp.status_code, 200)
            self.assertIn("pad_test.csv", client.get("/api/data").json()["files"])

            dl_resp = client.get("/api/data/pad_test.csv")
            self.assertEqual(dl_resp.status_code, 200)
            self.assertIn("250,1.0", dl_resp.text)

            ren_resp = client.post(
                "/api/data/pad_test.csv/rename", json={"new_name": "pad_test_2.csv"}
            )
            self.assertEqual(ren_resp.status_code, 200)
            self.assertEqual(client.get("/api/data/pad_test.csv").status_code, 404)

            del_resp = client.delete("/api/data/pad_test_2.csv")
            self.assertEqual(del_resp.status_code, 200)

            with client.websocket_connect("/ws") as ws:
                init_msg = ws.receive_json()
                self.assertEqual(init_msg["type"], "init")
                self.assertIn("P_RUN_TANK", init_msg["channels"])


class _FakeU6Device:
    """In-memory LabJack U6 stub tracking lifecycle calls."""

    def __init__(self, *, fail_on_config: bool = False) -> None:
        self.fail_on_config = fail_on_config
        self.calls: list[str] = []

    def streamStop(self) -> None:
        self.calls.append("streamStop")

    def streamConfig(self, **_kwargs) -> None:
        self.calls.append("streamConfig")
        if self.fail_on_config:
            raise RuntimeError("config failed")

    def getTemperature(self) -> float:
        self.calls.append("getTemperature")
        return 298.15

    def streamStart(self) -> None:
        self.calls.append("streamStart")

    def close(self) -> None:
        self.calls.append("close")


class _FakeAsyncWSClient(_RecordingClient):
    """Async-iterable WebSocket stub that yields scripted frames or raises."""

    def __init__(
        self,
        server: ws_server.FlowControlWSServer,
        items: list[str | Exception],
    ) -> None:
        super().__init__()
        self._server = server
        self._items = list(items)
        self.seen_registered_on_iter = False
        self.path = "/ws"

    def __aiter__(self) -> "_FakeAsyncWSClient":
        return self

    async def __anext__(self) -> str:
        if self in self._server.clients:
            self.seen_registered_on_iter = True
        if not self._items:
            raise StopAsyncIteration
        nxt = self._items.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


class TestContextManagerDecorators(unittest.IsolatedAsyncioTestCase):
    """Tests stdlib @contextmanager and @asynccontextmanager helpers."""

    def test_open_stream_context_manager_lifecycle_and_errors(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            server = ws_server.FlowControlWSServer(data_dir=tmpdir)
            inst = instrumentation.LabJackInstrumentation(ws_server=server)
            created: list[_FakeU6Device] = []
            fail_next = False

            def _make_u6() -> _FakeU6Device:
                dev = _FakeU6Device(fail_on_config=fail_next)
                created.append(dev)
                return dev

            fake_u6 = mock.Mock(U6=_make_u6)
            with mock.patch.object(instrumentation, "u6", fake_u6):
                with inst._open_stream() as (device, v_ref):
                    self.assertIs(device, created[-1])
                    self.assertAlmostEqual(v_ref, thermocouple.get_ref_voltage(298.15))
                self.assertEqual(
                    created[-1].calls,
                    [
                        "streamStop",
                        "streamConfig",
                        "getTemperature",
                        "streamStart",
                        "streamStop",
                        "close",
                    ],
                )

                with self.assertRaises(RuntimeError):
                    with inst._open_stream():
                        raise RuntimeError("stream read error")
                self.assertEqual(
                    created[-1].calls,
                    [
                        "streamStop",
                        "streamConfig",
                        "getTemperature",
                        "streamStart",
                        "streamStop",
                        "close",
                    ],
                )

                fail_next = True
                with self.assertRaises(RuntimeError):
                    with inst._open_stream():
                        pass
                self.assertEqual(
                    created[-1].calls,
                    ["streamStop", "streamConfig", "close"],
                )

    def test_translate_http_data_errors_mapping(self) -> None:
        class _FakeHTTPException(Exception):
            def __init__(self, status_code: int, detail: str) -> None:
                super().__init__(detail)
                self.status_code = status_code
                self.detail = detail

        cases = [
            (ValueError("bad name"), 400),
            (FileNotFoundError("missing.csv"), 404),
            (PermissionError("recording active"), 409),
            (FileExistsError("already exists"), 409),
        ]
        with mock.patch.object(ws_server, "HTTPException", _FakeHTTPException):
            for err, expected_status in cases:
                with self.assertRaises(_FakeHTTPException) as ctx:
                    with ws_server.FlowControlWSServer._translate_http_data_errors():
                        raise err
                self.assertEqual(ctx.exception.status_code, expected_status)
                self.assertEqual(ctx.exception.detail, str(err))

    async def test_track_client_session_via_ws_client_handler(self) -> None:
        class _FakeDisconnect(Exception):
            pass

        with tempfile.TemporaryDirectory() as tmpdir:
            ctrl = _DummySerialController()
            server = ws_server.FlowControlWSServer(
                armed=True, data_dir=tmpdir, serial_controller=ctrl
            )
            with mock.patch.object(ws_server, "_WSConnectionClosed", _FakeDisconnect):
                disconnecting = _FakeAsyncWSClient(
                    server,
                    [
                        json.dumps(
                            {"type": "command", "valve": "N2OV", "action": "OPEN"}
                        ),
                        _FakeDisconnect("client closed"),
                    ],
                )
                await server._ws_client_handler(disconnecting)
                self.assertTrue(disconnecting.seen_registered_on_iter)
                self.assertEqual(disconnecting.messages[0]["type"], "init")
                self.assertEqual(ctrl.commands, [("N2OV", "OPEN")])
                self.assertNotIn(disconnecting, server.clients)

                crashing = _FakeAsyncWSClient(server, [RuntimeError("unexpected")])
                with self.assertLogs("fcs.ws_server", level="ERROR") as logs:
                    await server._ws_client_handler(crashing)
                self.assertTrue(crashing.seen_registered_on_iter)
                self.assertEqual(crashing.messages[0]["type"], "init")
                self.assertNotIn(crashing, server.clients)
                self.assertTrue(
                    any("Unexpected error in client handler" in msg for msg in logs.output)
                )


if __name__ == "__main__":
    unittest.main()
