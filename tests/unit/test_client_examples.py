"""Client templates must agree on mode, root policy and CANoe ownership."""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

import pytest

from canoe17_mcp.settings import load_settings

CLIENTS = Path(__file__).resolve().parents[2] / "clients"


@pytest.mark.parametrize("write", [False, True])
def test_clients_match_server_policy(write: bool) -> None:
    mode = "write" if write else "read-only"
    settings_file = CLIENTS / f"{mode}.toml"
    with settings_file.open("rb") as handle:
        data = tomllib.load(handle)
    assert data["read_only"] is not write
    assert data["allowed_roots"] == (["C:/Bench/projects"] if write else [])
    assert data["backend"]["lock_key"] == "default"
    assert data["backend"]["min_evidence"] == "demo_verified"

    claude = json.loads((CLIENTS / "claude-code" / f"{mode}.mcp.json").read_text())
    with (CLIENTS / "codex" / f"{mode}.config.toml").open("rb") as handle:
        codex = tomllib.load(handle)
    opencode = json.loads((CLIENTS / "opencode" / f"{mode}.opencode.json").read_text())
    servers = [
        claude["mcpServers"]["canoe17"], codex["mcp_servers"]["canoe17"],
        opencode["mcp"]["canoe17"],
    ]
    for client in servers:
        command = client["command"]
        args = command[1:] if isinstance(command, list) else client["args"]
        assert (command[0] if isinstance(command, list) else command) == "uv"
        assert args == [
            "--directory", "C:/Bench/canoe17-mcp", "run", "--frozen", "--no-sync", "canoe17-mcp",
            "--config", f"C:/Bench/settings/{mode}.toml",
        ]
        env = client.get("env", client.get("environment"))
        assert env["CANOE17_MCP_READ_ONLY"] == str(not write).lower()
        assert env["CANOE17_MCP_LOCK_KEY"] == "default"
        if write:
            assert json.loads(env["CANOE17_MCP_ALLOWED_ROOTS"]) == data["allowed_roots"]
    assert opencode["model"].startswith("ollama/qwen")
    assert opencode["provider"]["ollama"]["options"]["baseURL"] == "http://127.0.0.1:11434/v1"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows template paths")
@pytest.mark.parametrize("mode", ["read-only", "write"])
def test_server_template_loads(mode: str) -> None:
    settings = load_settings(CLIENTS / f"{mode}.toml", environ={})
    assert settings.backend_kind == "com"
    assert settings.read_only is (mode == "read-only")
