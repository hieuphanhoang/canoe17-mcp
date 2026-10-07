from __future__ import annotations

import json
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@asynccontextmanager
async def client(tmp_path: Path, *, read_only: bool = False) -> AsyncIterator[ClientSession]:
    config = tmp_path / "server.toml"
    path = str(tmp_path / "demo.cfg")
    config.write_text(
        'backend_kind = "fake"\n'
        f"read_only = {str(read_only).lower()}\n"
        f"allowed_roots = {json.dumps([str(tmp_path)])}\n"
        f"fake_config_paths = {json.dumps([path])}\n"
        "fake_licensed = true\n",
        encoding="utf-8",
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "canoe17_mcp.server", "--config", str(config), "--backend", "fake"],
        env={"PYTHONUNBUFFERED": "1"},
    )
    with anyio.fail_after(20):
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.serverInfo.name == "canoe17-mcp"
                yield session


def payload(result: types.CallToolResult) -> dict:
    assert result.structuredContent is not None
    text = result.content[0]
    assert isinstance(text, types.TextContent)
    assert json.loads(text.text) == result.structuredContent
    return result.structuredContent


@pytest.mark.anyio
async def test_stdio_initialize_discovery_read_preview_confirm_poll(tmp_path: Path) -> None:
    async with client(tmp_path) as session:
        listing = await session.list_tools()
        tools = {tool.name: tool for tool in listing.tools}
        assert len(tools) == 11
        assert tools["canoe_database"].inputSchema["properties"]["action"]["enum"] == [
            "list",
            "set_channel",
        ]
        for tool in listing.tools:
            assert tool.inputSchema["type"] == "object"
            assert not {"oneOf", "anyOf", "allOf"} & tool.inputSchema.keys()
        status = await session.call_tool("canoe_status", {})
        assert not status.isError
        assert payload(status)["result"]["session"]["backend"] == "fake"
        assert not payload(status)["result"]["session"]["connected"]
        args = {"path": str(tmp_path / "demo.cfg"), "launch_if_absent": False}
        preview = await session.call_tool("canoe_open_config", args)
        assert not preview.isError and payload(preview)["result"]["needs_confirmation"]
        confirmed = await session.call_tool("canoe_open_config", {**args, "confirm": True})
        assert not confirmed.isError
        op = payload(confirmed)["result"]["operation"]
        assert op["state"] == "completed"
        polled = await session.call_tool(
            "canoe_operation",
            {
                "action": "status",
                "operation_id": op["operation_id"],
            },
        )
        assert payload(polled)["result"]["value"] == op
        summary = await session.call_tool("canoe_get_config_summary", {})
        assert payload(summary)["result"]["value"]["configuration_path"] == args["path"]
        compile_result = await session.call_tool("canoe_compile", {"confirm": True})
        assert payload(compile_result)["result"]["operation"]["result"]["success"]
        saved = await session.call_tool(
            "canoe_save_config",
            {
                "as_path": str(tmp_path / "copy.cfg"),
                "confirm": True,
            },
        )
        assert payload(saved)["result"]["operation"]["state"] == "completed"
        assert not (tmp_path / "copy.cfg").exists()


@pytest.mark.anyio
async def test_stdio_structured_policy_validation_and_unavailable_errors(tmp_path: Path) -> None:
    async with client(tmp_path) as session:
        cases = [
            ("canoe_open_config", {"path": "relative.cfg"}, "path_not_allowed"),
            ("canoe_compile", {"confirm": "true"}, "invalid_argument"),
            (
                "canoe_database",
                {"action": "remove", "database_id": "db:X"},
                "capability_unavailable",
            ),
            ("canoe_write_window", {"action": "read", "max_chars": 65537}, "invalid_argument"),
            ("canoe_operation", {"action": "status", "operation_id": "op-missing"}, "not_found"),
        ]
        for name, arguments, code in cases:
            response = await session.call_tool(name, arguments)
            assert response.isError
            assert payload(response)["error"]["code"] == code


@pytest.mark.anyio
async def test_stdio_readonly_rejects_preview_and_confirm(tmp_path: Path) -> None:
    async with client(tmp_path, read_only=True) as session:
        assert not (await session.call_tool("canoe_status", {})).isError
        for confirm in (False, True):
            result = await session.call_tool("canoe_compile", {"confirm": confirm})
            assert result.isError
            assert payload(result)["error"]["code"] == "read_only_mode"
