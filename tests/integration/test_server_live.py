"""Gate-3 demo: the real stdio server with the COM backend, driven as an MCP client.

Opt-in like the other live probes. Scope is ASK-002 (b): licence-free actions
only, on sample copies in the sandbox. Writes a JSON transcript next to the
sandbox (``demo-transcript.json``) for the demo record in docs/com/.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest

pytestmark = pytest.mark.canoe

mcp = pytest.importorskip("mcp")
from mcp import ClientSession, StdioServerParameters  # noqa: E402
from mcp.client.stdio import stdio_client  # noqa: E402


class Demo:
    def __init__(self, session: ClientSession) -> None:
        self.session = session
        self.transcript: list[dict[str, Any]] = []

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        result = await self.session.call_tool(tool, args)
        payload = result.structuredContent or json.loads(result.content[0].text)  # type: ignore[union-attr]
        self.transcript.append(
            {"tool": tool, "arguments": args, "is_error": result.isError, "payload": payload}
        )
        return payload

    async def confirmed(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Preview, then confirm with the same arguments, as a client would."""
        preview = await self.call(tool, {**args, "confirm": False})
        assert "error" not in preview, preview
        assert preview["result"]["needs_confirmation"] is True
        done = await self.call(tool, {**args, "confirm": True})
        assert "error" not in done, done
        return done["result"]["operation"]


def test_server_demo_on_udsbasic_copy(sandbox: Path, tmp_path: Path):
    cfg = sandbox / "UDSBasic" / "UDSBasic.cfg"
    cdd = sandbox / "UDSBasic" / "Cdd" / "UDS-ExampleEcu-6.0.1.cdd"
    extra = sandbox / "UDSBasic" / "Cdd" / "demo-extra.cdd"
    shutil.copy2(cdd, extra)
    settings = tmp_path / "demo.toml"
    settings.write_text(
        "read_only = false\n"
        f"allowed_roots = [{json.dumps(str(sandbox))}]\n"
        'backend_kind = "com"\n'
        "[backend]\n"
        'lock_key = "server-demo"\n',
        encoding="utf-8",
    )
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "canoe17_mcp.server", "--config", str(settings), "--backend", "com"],
        env={**os.environ},
    )

    async def scenario() -> list[dict[str, Any]]:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                demo = Demo(session)
                tools = {t.name: t for t in (await session.list_tools()).tools}
                demo.transcript.append({"tools": sorted(tools)})
                for t in tools.values():
                    for combinator in ("oneOf", "anyOf", "allOf"):
                        assert combinator not in t.inputSchema

                status = await demo.call("canoe_status", {})
                assert status["result"]["session"]["backend"] == "com"
                assert status["result"]["session"]["connected"] is False  # status never attaches

                current = status["result"]["session"]
                on_dirty = "refuse"
                opened = await demo.confirmed(
                    "canoe_open_config",
                    {"path": str(cfg), "on_dirty": on_dirty, "launch_if_absent": True},
                )
                if opened["state"] == "failed" and opened["error"]["code"] == "dirty_config":
                    # Leftover from an earlier probe on a sandbox copy only.
                    path = (opened["error"]["details"] or [[None, ""]])[0][1]
                    assert str(path).lower().startswith(str(sandbox).lower()), opened
                    opened = await demo.confirmed(
                        "canoe_open_config",
                        {"path": str(cfg), "on_dirty": "discard", "launch_if_absent": True},
                    )
                assert opened["state"] == "completed", opened
                assert opened["result"]["active_path"].lower() == str(cfg).lower()
                del current

                summary = await demo.call("canoe_get_config_summary", {"section": "all"})
                value = summary["result"]["value"]
                assert {n["name"] for n in value["nodes"]} >= {"Tester", "SimDiagECU"}
                assert value["diag_descriptions"][0]["id"] == "diag:Door"

                assert {"canoe_database", "canoe_write_window"} <= set(tools)
                dbs = await demo.call("canoe_database", {"action": "list"})
                assert dbs["result"]["value"] == []  # UDSBasic has no databases
                await demo.call("canoe_write_window", {"action": "read"})

                compiled = await demo.confirmed("canoe_compile", {})
                assert compiled["state"] == "completed" and compiled["result"]["success"]

                added = await demo.confirmed(
                    "canoe_diag_description",
                    {"action": "add", "network": "CAN", "path": str(extra), "open_console": True},
                )
                assert added["state"] == "completed", added
                assert added["result"]["id"] == "diag:Door_1"

                closed = await demo.confirmed(
                    "canoe_diag_description",
                    {"action": "close_windows", "qualifier": "diag:Door_1", "window": "all"},
                )
                assert closed["state"] == "completed", closed

                removed = await demo.confirmed(
                    "canoe_diag_description", {"action": "remove", "qualifier": "diag:Door_1"}
                )
                assert removed["state"] == "completed", removed

                listed = await demo.call("canoe_diag_description", {"action": "list"})
                assert [d["id"] for d in listed["result"]["value"]] == ["diag:Door"]

                # Removing what we added still leaves the configuration modified;
                # reopen discards it, which is the demo's "reopen" step.
                reopened = await demo.confirmed(
                    "canoe_open_config",
                    {"path": str(cfg), "on_dirty": "discard", "launch_if_absent": False},
                )
                assert reopened["state"] == "completed", reopened
                final = await demo.call("canoe_status", {})
                assert final["result"]["session"]["configuration_modified"] is False
                return demo.transcript

    transcript = anyio.run(scenario)
    extra.unlink(missing_ok=True)
    out = sandbox / "demo-transcript.json"
    out.write_text(json.dumps(transcript, indent=1), encoding="utf-8")
