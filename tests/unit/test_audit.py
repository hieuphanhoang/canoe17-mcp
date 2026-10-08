from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from canoe17_mcp.audit import AuditLog
from canoe17_mcp.settings import Settings, load_settings


def test_audit_redacts_paths_names_payloads_and_errors(tmp_path: Path) -> None:
    audit = AuditLog(Settings(audit_path=tmp_path / "audit.jsonl"))
    ticket = audit.begin("canoe_database", {
        "action": "add", "path": "credential-secret.dbc", "bus": "secret-name",
        "channel": 2, "confirm": True,
    }, "fake")
    audit.finish(ticket, {"operation": {
        "operation_id": "op-test", "state": "failed", "epoch": 1,
        "dispatched": True, "effects_possible": True,
        "error": {"code": "canoe_rejected", "message": "password-secret"},
        "result": {"payload": "secret-data"},
    }})
    text = audit.path.read_text()
    assert "secret" not in text
    rows = [json.loads(line) for line in text.splitlines()]
    assert rows[0]["params"]["channel"] == 2
    assert rows[1]["result"]["error_code"] == "canoe_rejected"
    assert rows[1]["duration"] >= 0 and rows[0]["call_id"] == rows[1]["call_id"]


@pytest.mark.parametrize("backups", [0, 2])
def test_audit_rotation_is_bounded_and_keeps_complete_jsonl(tmp_path: Path, backups: int) -> None:
    audit = AuditLog(Settings(
        audit_path=tmp_path / "audit.jsonl", audit_max_bytes=4096, audit_backup_count=backups,
    ))
    for _ in range(50):
        ticket = audit.begin("canoe_compile", {"confirm": True}, "fake")
        assert audit.finish(ticket, {})
    paths = list(tmp_path.glob("audit.jsonl*"))
    journals = [p for p in paths if p.suffix != ".lock"]
    assert len(journals) == backups + 1
    for path in journals:
        assert path.stat().st_size <= 4096
        assert all(
            json.loads(line)["tool"] == "canoe_compile" for line in path.read_text().splitlines()
        )


def test_audit_concurrent_tools_do_not_interleave_lines(tmp_path: Path) -> None:
    audit = AuditLog(Settings(audit_path=tmp_path / "audit.jsonl"))

    def call(_: int) -> None:
        ticket = audit.begin("canoe_compile", {"confirm": True}, "fake")
        assert audit.finish(ticket, {})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(call, range(20)))
    rows = [json.loads(line) for line in audit.path.read_text().splitlines()]
    assert len(rows) == 40
    assert len({row["call_id"] for row in rows}) == 20


def test_audit_settings_override_and_validation(tmp_path: Path) -> None:
    config = tmp_path / "config.toml"
    config.write_text('audit_max_bytes = 4096\naudit_backup_count = 1\n')
    settings = load_settings(
        config, environ={"CANOE17_MCP_AUDIT_PATH": str(tmp_path / "log.jsonl")}
    )
    assert settings.audit_path == tmp_path / "log.jsonl"
    assert settings.audit_max_bytes == 4096 and settings.audit_backup_count == 1
    with pytest.raises(ValueError):
        Settings(audit_path=Path("relative.jsonl"))
    with pytest.raises(ValueError):
        Settings(audit_max_bytes=100)
    with pytest.raises(ValueError):
        Settings(audit_backup_count=True)
