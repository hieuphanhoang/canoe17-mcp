from __future__ import annotations

import json
from pathlib import Path

import pytest

from canoe17_mcp import check, server
from canoe17_mcp.settings import Settings


def statuses(report: dict) -> dict[str, str]:
    return {item["name"]: item["status"] for item in report["checks"]}


def test_fake_skips_com_and_preserves_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    audit = tmp_path / "audit.jsonl"
    audit.write_bytes(b"existing records\n")
    guard = tmp_path / "audit.jsonl.lock"
    guard.write_bytes(b"0")

    def forbidden() -> str:
        pytest.fail("Fake check must not inspect COM")

    monkeypatch.setattr(check, "_pywin32", forbidden)
    monkeypatch.setattr(check, "_registration", forbidden)
    result = check.installation_check(Settings(backend_kind="fake", audit_path=audit))
    assert result["ok"]
    assert statuses(result)["pywin32"] == "skip"
    assert audit.read_bytes() == b"existing records\n"
    assert guard.read_bytes() == b"0"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["audit.jsonl", "audit.jsonl.lock"]


def test_com_collects_independent_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check.sys, "platform", "win32")
    monkeypatch.setattr(check.struct, "calcsize", lambda _: 4)

    def missing_import() -> str:
        raise ImportError("pywin32 missing")

    def missing_registration() -> str:
        raise FileNotFoundError("ProgID missing")

    monkeypatch.setattr(check, "_pywin32", missing_import)
    monkeypatch.setattr(check, "_registration", missing_registration)
    result = check.installation_check(Settings(audit_path=tmp_path / "audit.jsonl"))
    assert not result["ok"]
    assert statuses(result) == {
        "settings": "pass", "windows": "pass", "python_bitness": "fail",
        "pywin32": "fail", "canoe_registration": "fail", "audit_destination": "pass",
    }
    assert not (tmp_path / "audit.jsonl").exists()


def test_non_windows_com_check_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(check.sys, "platform", "linux")
    result = check.installation_check(Settings(audit_path=tmp_path / "audit.jsonl"))
    assert not result["ok"]
    assert statuses(result)["windows"] == "fail"
    assert statuses(result)["canoe_registration"] == "skip"


def test_audit_destination_failure(tmp_path: Path) -> None:
    directory = tmp_path / "audit.jsonl"
    directory.mkdir()
    result = check.installation_check(Settings(backend_kind="fake", audit_path=directory))
    assert not result["ok"]
    assert statuses(result)["audit_destination"] == "fail"


@pytest.mark.parametrize("backend_kind", ["com", "fake"])
def test_cli_check_never_builds_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    backend_kind: str,
) -> None:
    config = tmp_path / "settings.toml"
    config.write_text(f'audit_path = {json.dumps(str(tmp_path / "audit.jsonl"))}\n')

    def forbidden(_: Settings) -> None:
        pytest.fail("Installation check must not construct any backend")

    monkeypatch.setattr(server, "make_backend", forbidden)
    monkeypatch.setattr(check, "_pywin32", lambda: "mock import ok")
    monkeypatch.setattr(check, "_registration", lambda: "mock registry ok")
    monkeypatch.setattr(check.sys, "platform", "win32")
    monkeypatch.setattr(check.struct, "calcsize", lambda _: 8)
    assert server.main(["--check", "--config", str(config), "--backend", backend_kind]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["ok"] and report["backend"] == backend_kind


def test_cli_invalid_settings_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    config = tmp_path / "invalid.toml"
    config.write_text("read_only = 7\n")

    def forbidden(_: Settings) -> None:
        pytest.fail("Invalid settings must not construct a backend")

    monkeypatch.setattr(server, "make_backend", forbidden)
    assert server.main(["--check", "--config", str(config)]) == 1
    result = json.loads(capsys.readouterr().out)
    assert not result["ok"] and statuses(result) == {"settings": "fail"}


def test_registration_reads_only(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import nullcontext
    from types import SimpleNamespace

    keys: list[str] = []

    def open_key(root: object, path: str) -> object:
        assert root == "HKCR"
        keys.append(path)
        return nullcontext(path)

    registry = SimpleNamespace(
        HKEY_CLASSES_ROOT="HKCR", OpenKey=open_key,
        QueryValueEx=lambda key, _: (
            "{test-clsid}" if key == r"CANoe.Application\CLSID" else '"C:/Vector/CANoe.exe"',
            1,
        ),
    )
    monkeypatch.setitem(check.sys.modules, "winreg", registry)
    assert "CANoe.Application" in check._registration()
    assert keys == [r"CANoe.Application\CLSID", r"CLSID\{test-clsid}\LocalServer32"]
