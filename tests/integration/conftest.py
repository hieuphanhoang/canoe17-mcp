"""Opt-in live CANoe probes.

Run with ``CANOE17_MCP_PROBE=1`` and ``CANOE17_MCP_SANDBOX=<folder>`` where the
folder holds a *copy* of the CANoe ``UDSBasic`` sample (``UDSBasic/UDSBasic.cfg``)
and of ``Easy`` (``Easy/Easy.cfg``). Never point it at original samples or
customer projects. Missing prerequisites skip; automation failures fail.

Bench profile (for a PC with a **licensed** CANoe, e.g. the test bench):
add ``CANOE17_MCP_BENCH=1``. Probes marked ``licensed`` then run for real
(save-copy with backup and persistence, saving before an open, measurement
start/stop events); without it they skip. They still use only the sandbox
copies. Nothing here needs Vector hardware yet: UDS, Tester Present, values
and frames are not implemented in the backend, so there is no hardware probe.
Report the run with ``pytest -m canoe -rA`` output and docs/com/api-evidence.md
rows for anything that differs.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "canoe: needs a live CANoe 17 (opt-in, see conftest)")
    config.addinivalue_line(
        "markers", "licensed: needs a licensed CANoe; runs only with CANOE17_MCP_BENCH=1"
    )


@pytest.fixture(scope="session")
def bench() -> None:
    """Skip unless the bench profile is on (a licensed CANoe)."""
    if os.environ.get("CANOE17_MCP_BENCH") != "1":
        pytest.skip("licensed probe: set CANOE17_MCP_BENCH=1 on a PC with a CANoe licence")


@pytest.fixture(scope="session")
def sandbox() -> Path:
    if sys.platform != "win32":
        pytest.skip("CANoe COM needs Windows")
    if os.environ.get("CANOE17_MCP_PROBE") != "1":
        pytest.skip("set CANOE17_MCP_PROBE=1 to run live CANoe probes")
    root = os.environ.get("CANOE17_MCP_SANDBOX")
    if not root:
        pytest.skip("set CANOE17_MCP_SANDBOX to a folder with sample copies")
    path = Path(root)
    if not (path / "UDSBasic" / "UDSBasic.cfg").is_file():
        pytest.skip(f"{path} has no UDSBasic/UDSBasic.cfg copy")
    return path
