"""Cross-repository end-to-end smoke test runner for Flow-Control-System-Server.

Locates the sibling `Flow-Control-System-Client` repository (via
`FCS_CLIENT_REPO` or `../Flow-Control-System-Client`) and runs its live
`tests/test_e2e_smoke.py` suite against this server checkout.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

SERVER_REPO_ROOT = Path(__file__).resolve().parents[1]


def _resolve_client_repo() -> Path | None:
    env_override = os.environ.get("FCS_CLIENT_REPO", "").strip()
    candidates = [
        Path(env_override) if env_override else None,
        SERVER_REPO_ROOT.parent / "Flow-Control-System-Client",
    ]
    for candidate in candidates:
        if candidate and (candidate / "tests" / "test_e2e_smoke.py").is_file():
            return candidate
    return None


class TestCrossRepoClientE2ESmoke(unittest.TestCase):
    """Runs the live Server <-> Client smoke test from the server repo."""

    def test_client_e2e_smoke_against_this_server(self) -> None:
        client_repo = _resolve_client_repo()
        if client_repo is None:
            raise unittest.SkipTest(
                "Sibling Flow-Control-System-Client repo not found "
                "(set FCS_CLIENT_REPO to enable cross-repo E2E smoke tests)."
            )

        missing = [
            mod
            for mod in ("fastapi", "uvicorn", "PySide6.QtWebSockets")
            if importlib.util.find_spec(mod) is None
        ]
        if missing:
            raise unittest.SkipTest(
                f"Missing dependencies for cross-repo E2E smoke test: {', '.join(missing)}"
            )

        env = os.environ.copy()
        env["FCS_SERVER_REPO"] = str(SERVER_REPO_ROOT)
        env.setdefault("QT_QPA_PLATFORM", "offscreen")

        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_e2e_smoke.py", "-v"],
            cwd=str(client_repo),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0:
            self.fail(
                "Cross-repo Client E2E smoke test failed "
                f"(exit code {proc.returncode}):\n"
                f"--- STDOUT ---\n{proc.stdout}\n"
                f"--- STDERR ---\n{proc.stderr}"
            )


if __name__ == "__main__":
    unittest.main()
