"""Installation diagnostics: never construct a backend or activate COM."""

from __future__ import annotations

import importlib
import struct
import sys
import tempfile
from pathlib import Path
from typing import Any

from .settings import Settings


def _pywin32() -> str:
    for module in ("pythoncom", "pywintypes", "win32com.client"):
        importlib.import_module(module)
    return "pythoncom, pywintypes and win32com.client import successfully"


def _registration() -> str:
    # OpenKey defaults to KEY_READ. No Dispatch/GetActiveObject/CoCreateInstance.
    import winreg

    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"CANoe.Application\CLSID") as key:
        clsid = winreg.QueryValueEx(key, "")[0]
    if not isinstance(clsid, str) or not clsid.strip():
        raise ValueError("CANoe.Application has no CLSID")
    with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{clsid}\LocalServer32") as key:
        server = winreg.QueryValueEx(key, "")[0]
    if not isinstance(server, str) or not server.strip():
        raise ValueError("CANoe.Application has no LocalServer32 command")
    return f"CANoe.Application -> {clsid}; LocalServer32: {server}"


def _audit_destination(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Check existing destinations without creating/truncating journal or lock files.
    for candidate in (path, path.with_name(path.name + ".lock")):
        if candidate.exists():
            with candidate.open("r+b"):
                pass
    # A sibling probe also checks creation/deletion needed for rotation.
    with tempfile.NamedTemporaryFile(prefix=".canoe17-check-", dir=path.parent) as probe:
        probe.write(b"check\n")
        probe.flush()
    return f"Writable destination: {path}; existing audit contents preserved"


def installation_check(settings: Settings) -> dict[str, Any]:
    """Return a JSON-ready report. A fake check makes no COM readiness claim."""
    checks: list[dict[str, str]] = []

    def record(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    record("settings", "pass", "TOML/environment settings and backend override are valid")
    if settings.backend_kind == "com":
        windows = sys.platform == "win32"
        record("windows", "pass" if windows else "fail", f"Platform: {sys.platform}")
        bits = struct.calcsize("P") * 8
        record("python_bitness", "pass" if bits == 64 else "fail", f"Python is {bits}-bit")
        for name, probe in (("pywin32", _pywin32), ("canoe_registration", _registration)):
            if not windows:
                record(name, "skip", "COM backend requires Windows")
                continue
            try:
                record(name, "pass", probe())
            except (ImportError, OSError, ValueError) as exc:
                record(name, "fail", str(exc))
    else:
        for name in ("windows", "python_bitness", "pywin32", "canoe_registration"):
            record(name, "skip", "Fake backend selected; no real CANoe readiness claim")
    try:
        record("audit_destination", "pass", _audit_destination(settings.audit_path))
    except OSError as exc:
        record("audit_destination", "fail", str(exc))
    return {
        "ok": all(item["status"] != "fail" for item in checks),
        "backend": settings.backend_kind,
        "checks": checks,
        "limits": "Does not check running CANoe, version, elevation, lock, licence or hardware; "
        "never starts or connects to CANoe",
    }
