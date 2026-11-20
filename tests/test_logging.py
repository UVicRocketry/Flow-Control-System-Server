"""Unit tests for unified server and Uvicorn logging (`logging_config.py`)."""

import asyncio
import io
import logging
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

CODE_DIR = str(Path(__file__).resolve().parents[1] / "contents" / "code")
if CODE_DIR not in sys.path:
    sys.path.insert(0, CODE_DIR)

import logging_config  # noqa: E402
import ws_server  # noqa: E402

DATE_PREFIX_RE = re.compile(r"^[A-Z]+:\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} ")


class TestFCSLogFormatterAndConfig(unittest.TestCase):
    def setUp(self) -> None:
        root = logging.getLogger()
        self._saved_root_handlers = list(root.handlers)
        self._saved_root_level = root.level
        self._saved_uvicorn_state = {}
        for name in logging_config.UVICORN_LOGGERS:
            lgr = logging.getLogger(name)
            self._saved_uvicorn_state[name] = (
                list(lgr.handlers),
                lgr.propagate,
                lgr.level,
            )

    def tearDown(self) -> None:
        root = logging.getLogger()
        root.handlers = self._saved_root_handlers
        root.setLevel(self._saved_root_level)
        for name, (handlers, propagate, level) in self._saved_uvicorn_state.items():
            lgr = logging.getLogger(name)
            lgr.handlers = handlers
            lgr.propagate = propagate
            lgr.setLevel(level)

    def test_level_formatting_and_date_column_alignment(self) -> None:
        formatter = logging_config.FCSLogFormatter(use_colors=False)
        width = logging_config.LOGGER_TYPE_WIDTH
        self.assertEqual(width, 8)
        samples = [
            (logging.DEBUG, "fcs.main", "main", "debug tick", "DBG:  "),
            (logging.INFO, "fcs.ws_server", "server", "server ready", "INFO: "),
            (
                logging.WARNING,
                "fcs.instrumentation",
                "labjack",
                "falling back to MOCK",
                "WARN: ",
            ),
            (logging.ERROR, "uvicorn.error", "uvicorn", "serial failed", "ERR:  "),
            (
                logging.CRITICAL,
                "fcs.data_recorder",
                "recorder",
                "abort failure",
                "CRIT: ",
            ),
            (logging.INFO, "fcs.custom", "custom", "custom helper", "INFO: "),
        ]
        date_offsets: set[int] = set()
        sep_offsets: set[int] = set()
        for level, name, expected_type, msg, expected_prefix in samples:
            rec = logging.LogRecord(name, level, __file__, 1, msg, None, None)
            line = formatter.format(rec)
            self.assertTrue(line.startswith(expected_prefix), line)
            self.assertRegex(line, DATE_PREFIX_RE)
            self.assertTrue(
                line.endswith(f" {expected_type.rjust(width)} - {msg}"), line
            )
            date_offsets.add(line.index("20"))
            sep_offsets.add(line.index(" - "))
        self.assertEqual(date_offsets, {6})
        self.assertEqual(sep_offsets, {6 + 19 + 1 + width})

    def test_uvicorn_type_aliases_and_access_status_phrases(self) -> None:
        formatter = logging_config.FCSLogFormatter(use_colors=False)
        w = logging_config.LOGGER_TYPE_WIDTH
        uv_pad = "uvicorn".rjust(w)
        http_pad = "http".rjust(w)

        uv_err = logging.LogRecord(
            "uvicorn.error",
            logging.INFO,
            __file__,
            1,
            "Application startup complete.",
            None,
            None,
        )
        self.assertIn(
            f" {uv_pad} - Application startup complete.", formatter.format(uv_err)
        )

        uv_ok = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:50000", "GET", "/api/data", "1.1", 200),
            None,
        )
        self.assertIn(
            f' {http_pad} - 127.0.0.1:50000 - "GET /api/data HTTP/1.1" 200 OK',
            formatter.format(uv_ok),
        )

        uv_404 = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:50001", "GET", "/api/data/missing.csv", "1.1", 404),
            None,
        )
        self.assertIn(
            f' {http_pad} - 127.0.0.1:50001 - "GET /api/data/missing.csv HTTP/1.1" 404 Not Found',
            formatter.format(uv_404),
        )

        uv_plain = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            "raw %s message",
            ("text",),
            None,
        )
        self.assertIn(f" {http_pad} - raw text message", formatter.format(uv_plain))

        uv_bad_status = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %s',
            ("127.0.0.1:1", "GET", "/", "1.1", "NaN"),
            None,
        )
        self.assertIn(
            f' {http_pad} - 127.0.0.1:1 - "GET / HTTP/1.1" NaN',
            formatter.format(uv_bad_status),
        )

    def test_ansi_colors_preserve_spacing_and_color_status(self) -> None:
        formatter = logging_config.FCSLogFormatter(use_colors=True)
        warn_rec = logging.LogRecord(
            "fcs.main", logging.WARNING, __file__, 1, "mock mode", None, None
        )
        colored_warn = formatter.format(warn_rec)
        self.assertTrue(
            colored_warn.startswith("\033[33mWARN\033[0m: \033[2m"), repr(colored_warn)
        )
        self.assertIn("\033[0m     main - mock mode", colored_warn)

        access_rec = logging.LogRecord(
            "uvicorn.access",
            logging.INFO,
            __file__,
            1,
            '%s - "%s %s HTTP/%s" %d',
            ("127.0.0.1:9", "GET", "/api/data", "1.1", 500),
            None,
        )
        colored_access = formatter.format(access_rec)
        self.assertIn("\033[91m500 Internal Server Error\033[0m", colored_access)

    def test_exception_and_stack_info_preserved(self) -> None:
        formatter = logging_config.FCSLogFormatter(use_colors=False)
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            rec = logging.LogRecord(
                "fcs.ws_server",
                logging.ERROR,
                __file__,
                1,
                "handler crashed",
                None,
                sys.exc_info(),
                sinfo="Stack (fake):\n  line 1",
            )
        rendered = formatter.format(rec)
        lines = rendered.splitlines()
        self.assertTrue(lines[0].startswith("ERR:  "))
        self.assertIn("RuntimeError: boom", rendered)
        self.assertIn("Stack (fake):", rendered)

    def test_configure_logging_routes_uvicorn_and_avoids_duplicates(self) -> None:
        uv_err = logging.getLogger("uvicorn.error")
        uv_acc = logging.getLogger("uvicorn.access")
        uv_err.addHandler(logging.StreamHandler(io.StringIO()))
        uv_acc.addHandler(logging.StreamHandler(io.StringIO()))
        uv_err.propagate = False
        uv_acc.propagate = False

        buf = io.StringIO()
        logging_config.configure_logging("INFO", stream=buf, use_colors=False)

        self.assertEqual(uv_err.handlers, [])
        self.assertEqual(uv_acc.handlers, [])
        self.assertTrue(uv_err.propagate)
        self.assertTrue(uv_acc.propagate)
        self.assertEqual(uv_err.level, logging.NOTSET)

        logging.getLogger("fcs.ws_server").info("Client connected")
        uv_err.info("Uvicorn running on http://0.0.0.0:8080")
        uv_acc.info(
            '%s - "%s %s HTTP/%s" %d',
            "127.0.0.1:1234",
            "GET",
            "/api/data",
            "1.1",
            200,
        )

        lines = buf.getvalue().strip().splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual({line.index(" - ") for line in lines}, {34})
        self.assertRegex(
            lines[0],
            r"^INFO: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}   server - Client connected$",
        )
        self.assertRegex(
            lines[1],
            r"^INFO: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}  uvicorn - "
            r"Uvicorn running on http://0\.0\.0\.0:8080$",
        )
        self.assertRegex(
            lines[2],
            r"^INFO: \d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}     http - "
            r'127\.0\.0\.1:1234 - "GET /api/data HTTP/1\.1" 200 OK$',
        )

    def test_resolve_uvicorn_log_level_and_serve_uvicorn_passes_log_config_none(
        self,
    ) -> None:
        self.assertEqual(
            logging_config.resolve_uvicorn_log_level(logging.WARNING), "warning"
        )
        self.assertEqual(logging_config.resolve_uvicorn_log_level("DEBUG"), "debug")
        self.assertEqual(logging_config.resolve_uvicorn_log_level("UNKNOWN"), "info")

        buf = io.StringIO()
        logging_config.configure_logging("WARNING", stream=buf, use_colors=False)
        self.assertEqual(logging_config.resolve_uvicorn_log_level(), "warning")

        captured: dict[str, object] = {}

        class _FakeConfig:
            def __init__(self, app, **kwargs) -> None:
                captured["app"] = app
                captured.update(kwargs)

        class _FakeServer:
            def __init__(self, config) -> None:
                captured["server_config"] = config
                self.should_exit = False

            async def serve(self) -> None:
                return None

        fake_uvicorn = types.SimpleNamespace(Config=_FakeConfig, Server=_FakeServer)

        with tempfile.TemporaryDirectory() as tmpdir:
            srv = ws_server.FlowControlWSServer(data_dir=Path(tmpdir), armed=False)
            srv._stop_event = asyncio.Event()
            with (
                mock.patch.object(ws_server, "uvicorn", fake_uvicorn),
                mock.patch.object(srv, "create_app", return_value="FAKE_APP"),
            ):
                asyncio.run(srv._serve_uvicorn())

        self.assertEqual(captured["app"], "FAKE_APP")
        self.assertIsNone(captured["log_config"])
        self.assertEqual(captured["log_level"], "warning")
