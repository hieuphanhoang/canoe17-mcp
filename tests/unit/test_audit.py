from __future__ import annotations

import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from pathlib import Path
from typing import Any

import pytest

from canoe17_mcp.audit import AuditLog
from canoe17_mcp.settings import Settings, load_settings


def test_audit_redacts_unvalidated_paths_names_payloads_and_errors(tmp_path: Path) -> None:
    audit = AuditLog(Settings(audit_path=tmp_path / "audit.jsonl"))
    ticket = audit.begin("canoe_database", {
        "action": "add", "path": "credential-secret.dbc", "bus": "CAN",
        "name": "secret-name", "ecu_identifier": "secret-ecu", "payload": "secret-data",
        "unknown": 42, "channel": 2, "confirm": True,
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
    assert rows[0]["params"]["bus"] == "CAN"
    assert rows[0]["params"]["unknown"] == "[redacted]"
    assert rows[1]["result"]["error_code"] == "canoe_rejected"
    assert rows[1]["duration"] >= 0 and rows[0]["call_id"] == rows[1]["call_id"]


def test_audit_retains_only_explicit_selectors_and_validated_paths(tmp_path: Path) -> None:
    audit = AuditLog(Settings(audit_path=tmp_path / "audit.jsonl"))
    args = {
        "action": "add", "database_id": "db:Demo", "node_id": "node:ECU",
        "environment_id": "env:A", "module_id": "tm:A/Test", "operation_id": "op-1",
        "qualifier": "diag:ECU", "bus": "CAN", "network": "CAN",
        "window": "console", "section": "all", "on_dirty": "discard",
        "path": "client-spelling", "name": "secret-name", "ecu_identifier": "secret-ecu",
    }
    path = str(tmp_path / "source.dbc")
    cfg = str(tmp_path / "demo.cfg")
    ticket = audit.begin(
        "canoe_database", args, "fake",
        validated_paths={"path": path, "name": "secret-name"}, configuration_path=cfg,
    )
    assert audit.finish(ticket, {})
    rows = [json.loads(line) for line in audit.path.read_text().splitlines()]
    for row in rows:
        assert row["configuration_path"] == cfg
        assert row["params"] == {
            **args, "path": path, "name": "[redacted]", "ecu_identifier": "[redacted]",
        }


def _hold_audit_lock(path: str, ready: Any, release: Any) -> None:
    with Path(path).open("a+b") as guard:
        guard.write(b"0")
        guard.flush()
        guard.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(guard.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(guard.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        ready.set()
        assert release.wait(10)
        # Closing the handle releases the OS lock in either platform.


@pytest.mark.parametrize("expires", [False, True])
def test_audit_waits_for_other_process_and_bounds_contention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, expires: bool,
) -> None:
    from canoe17_mcp import audit as module

    audit = AuditLog(Settings(audit_path=tmp_path / "audit.jsonl"))
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    child = context.Process(
        target=_hold_audit_lock,
        args=(str(audit.path) + ".lock", ready, release),
    )
    child.start()
    try:
        assert ready.wait(10)
        if expires:
            monkeypatch.setattr(module, "_LOCK_WAIT_S", 0.15)
            with pytest.raises(OSError, match="lock wait expired"):
                audit.begin("canoe_compile", {"confirm": True}, "fake")
            assert not audit.path.exists() and not audit._pending
        else:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(audit.begin, "canoe_compile", {"confirm": True}, "fake")
                try:
                    with pytest.raises(TimeoutError):
                        future.result(timeout=0.15)
                finally:
                    release.set()
                ticket = future.result(timeout=5)
                assert audit.finish(ticket, {})
    finally:
        release.set()
        child.join(timeout=5)
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
    assert child.exitcode == 0


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
