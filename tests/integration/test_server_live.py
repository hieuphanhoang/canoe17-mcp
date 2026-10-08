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


def test_server_edit_tools_and_audit(sandbox: Path, tmp_path: Path):
    """Milestone 3 demo: attach-on-read, the new edit tools and the audit log."""
    udsbasic = sandbox / "UDSBasic"
    cfg = udsbasic / "UDSBasic.cfg"
    dbc = sandbox / "Easy" / "CANdb" / "easy.dbc"
    tse = udsbasic / "ProbeTestSetup.tse"
    if not dbc.is_file() or not tse.is_file():
        pytest.skip("needs Easy/CANdb/easy.dbc and UDSBasic/ProbeTestSetup.tse in the sandbox")
    capl = udsbasic / "Nodes" / "DemoNode.can"
    capl.write_text("variables {}\n", encoding="ascii")
    audit = tmp_path / "audit.jsonl"
    settings = tmp_path / "demo3.toml"
    settings.write_text(
        "read_only = false\n"
        f"allowed_roots = [{json.dumps(str(sandbox))}]\n"
        'backend_kind = "com"\n'
        f"audit_path = {json.dumps(str(audit))}\n"
        "[backend]\n"
        'lock_key = "server-demo3"\n',
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
                tools = {t.name for t in (await session.list_tools()).tools}
                demo.transcript.append({"tools": sorted(tools)})
                assert {"canoe_node", "canoe_test_setup", "canoe_can_controller"} <= tools

                # Attach-on-read: summary before any open, CANoe already running.
                summary = await demo.call("canoe_get_config_summary", {"section": "all"})
                if "error" in summary:
                    # Only a missing prerequisite skips; any other failure fails (CONTRIBUTING).
                    if summary["error"]["code"] == "no_active_instance":
                        pytest.skip("CANoe is not running; attach-on-read needs it")
                    pytest.fail(f"attach-on-read failed: {summary['error']}")
                path = summary["result"]["value"]["configuration_path"]
                if (
                    path.lower() != str(cfg).lower()
                    or (
                        (await demo.call("canoe_status", {}))["result"]["session"][
                            "configuration_modified"
                        ]
                    )
                ):
                    opened = await demo.confirmed(
                        "canoe_open_config",
                        {"path": str(cfg), "on_dirty": "discard", "launch_if_absent": False},
                    )
                    assert opened["state"] == "completed", opened

                ctl = await demo.call(
                    "canoe_can_controller", {"action": "read", "bus": "CAN", "channel": 1}
                )
                assert ctl["result"]["value"]["bitrate_bps"] == 500000

                node = await demo.confirmed(
                    "canoe_node",
                    {"action": "add", "name": "DemoNode", "bus": "CAN", "capl_path": str(capl)},
                )
                assert node["state"] == "completed", node
                assert node["result"]["id"] == "node:DemoNode"
                off = await demo.confirmed(
                    "canoe_node",
                    {"action": "set_active", "node_id": "node:DemoNode", "active": False},
                )
                assert off["result"]["active"] is False
                gone = await demo.confirmed(
                    "canoe_node", {"action": "remove", "node_id": "node:DemoNode"}
                )
                assert gone["state"] == "completed", gone

                db = await demo.confirmed(
                    "canoe_database",
                    {"action": "add", "path": str(dbc), "bus": "CAN", "channel": 1},
                )
                assert db["state"] == "completed" and db["result"]["id"] == "db:easy", db
                dropped = await demo.confirmed(
                    "canoe_database", {"action": "remove", "database_id": "db:easy"}
                )
                assert dropped["state"] == "completed", dropped

                env = await demo.confirmed(
                    "canoe_test_setup", {"action": "add_environment", "tse_path": str(tse)}
                )
                assert env["state"] == "completed", env
                module = await demo.confirmed(
                    "canoe_test_setup",
                    {
                        "action": "add_module",
                        "environment_id": env["result"]["id"],
                        "can_path": str(udsbasic / "Tester" / "TestModule.can"),
                    },
                )
                assert module["state"] == "completed", module
                disabled = await demo.confirmed(
                    "canoe_test_setup",
                    {
                        "action": "set_enabled",
                        "module_id": module["result"]["id"],
                        "enabled": False,
                    },
                )
                assert disabled["result"]["enabled"] is False

                reset = await demo.confirmed(
                    "canoe_open_config",
                    {"path": str(cfg), "on_dirty": "discard", "launch_if_absent": False},
                )
                assert reset["state"] == "completed", reset
                return demo.transcript

    transcript = anyio.run(scenario)
    capl.unlink(missing_ok=True)
    (sandbox / "demo3-transcript.json").write_text(
        json.dumps(transcript, indent=1), encoding="utf-8"
    )

    records = [json.loads(line) for line in audit.read_text(encoding="utf-8").splitlines()]
    shutil.copy2(audit, sandbox / "demo3-audit.jsonl")
    confirmed = sum(1 for e in transcript[1:] if e.get("arguments", {}).get("confirm") is True)
    attempts = [r for r in records if r["result"].get("state") == "attempt"]
    assert len(attempts) == confirmed

    # Audit says what changed (review A2) but not client-invented text.
    def attempt(tool: str, action: str) -> dict[str, Any]:
        found = [r for r in attempts if r["tool"] == tool and r["params"].get("action") == action]
        assert len(found) == 1, (tool, action, found)
        return found[0]

    for record in attempts:
        if record["tool"] != "canoe_open_config":
            assert record["configuration_path"].lower() == str(cfg).lower(), record
    node_add = attempt("canoe_node", "add")["params"]
    assert node_add["name"] == "[redacted]" and node_add["bus"] == "CAN"
    assert node_add["capl_path"].lower() == str(capl).lower()
    assert attempt("canoe_node", "set_active")["params"]["node_id"] == "node:DemoNode"
    assert attempt("canoe_database", "add")["params"]["path"].lower() == str(dbc).lower()
    assert attempt("canoe_database", "remove")["params"]["database_id"] == "db:easy"
    assert (
        attempt("canoe_test_setup", "add_environment")["params"]["tse_path"].lower()
        == str(tse).lower()
    )
    finals = {
        r["call_id"]: r["result"]["state"] for r in records if r["result"].get("state") != "attempt"
    }
    assert {r["call_id"] for r in attempts} <= set(finals)
    assert all(finals[r["call_id"]] == "completed" for r in attempts), finals
