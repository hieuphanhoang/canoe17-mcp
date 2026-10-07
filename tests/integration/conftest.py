"""Opt-in live CANoe probes.

Run with ``CANOE17_MCP_PROBE=1`` and ``CANOE17_MCP_SANDBOX=<folder>`` where the
folder holds a *copy* of the CANoe ``UDSBasic`` sample (``UDSBasic/UDSBasic.cfg``)
and of ``Easy`` (``Easy/Easy.cfg``). Never point it at original samples or
customer projects. Missing prerequisites skip; automation failures fail.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "canoe: needs a live CANoe 17 (opt-in, see conftest)")


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
