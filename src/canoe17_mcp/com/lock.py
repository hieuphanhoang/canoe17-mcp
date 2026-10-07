"""Session lock: one MCP server owns a CANoe session at a time.

A Windows named mutex ``Local\\canoe17-mcp-<key>`` plus a sidecar file with
the owner's PID (for status reporting only; the mutex is the lock). A mutex
is owned by a thread, so acquire and release must run on the same thread:
the STA worker. If the owning process dies, Windows abandons the mutex and
the next acquirer gets it (``WAIT_ABANDONED``), so a crashed server never
leaves a stale lock.
"""

from __future__ import annotations

import ctypes
import os
import re
import tempfile
import threading
from ctypes import wintypes
from pathlib import Path

WAIT_OBJECT_0 = 0x00000000
WAIT_ABANDONED = 0x00000080
WAIT_TIMEOUT = 0x00000102
WAIT_FAILED = 0xFFFFFFFF

_KEY_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _kernel32() -> ctypes.WinDLL:
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateMutexW.restype = wintypes.HANDLE
    k.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.ReleaseMutex.argtypes = [wintypes.HANDLE]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    return k


class SessionLock:
    def __init__(self, key: str, *, sidecar_dir: Path | None = None) -> None:
        if not _KEY_RE.match(key):
            raise ValueError(f"invalid lock key {key!r}")
        self.key = key
        self.name = f"Local\\canoe17-mcp-{key}"
        base = sidecar_dir or Path(tempfile.gettempdir()) / "canoe17-mcp"
        self.sidecar = base / f"lock-{key}.pid"
        self._handle: int | None = None
        self._thread: int | None = None
        self.abandoned_previous = False

    @property
    def held(self) -> bool:
        return self._handle is not None

    def acquire(self, timeout_s: float = 0.0) -> bool:
        """Try to take the lock on the calling thread. True when acquired."""
        if self._handle is not None:
            return True
        k = _kernel32()
        handle = k.CreateMutexW(None, False, self.name)
        if not handle:
            raise OSError(ctypes.get_last_error(), f"CreateMutexW({self.name}) failed")
        result = k.WaitForSingleObject(handle, max(0, int(timeout_s * 1000)))
        if result in (WAIT_OBJECT_0, WAIT_ABANDONED):
            self._handle = handle
            self._thread = threading.get_ident()
            self.abandoned_previous = result == WAIT_ABANDONED
            self._write_sidecar()
            return True
        k.CloseHandle(handle)
        if result == WAIT_FAILED:
            raise OSError(ctypes.get_last_error(), "WaitForSingleObject failed")
        return False

    def release(self) -> None:
        if self._handle is None:
            return
        if self._thread != threading.get_ident():
            raise RuntimeError("SessionLock must be released on the thread that acquired it")
        k = _kernel32()
        try:
            self.sidecar.unlink(missing_ok=True)
        except OSError:
            pass
        k.ReleaseMutex(self._handle)
        k.CloseHandle(self._handle)
        self._handle = None
        self._thread = None

    def holder_pid(self) -> int | None:
        """PID recorded by the current holder, if any (informational)."""
        try:
            text = self.sidecar.read_text(encoding="ascii").strip()
        except OSError:
            return None
        return int(text) if text.isdigit() else None

    def _write_sidecar(self) -> None:
        try:
            self.sidecar.parent.mkdir(parents=True, exist_ok=True)
            self.sidecar.write_text(str(os.getpid()), encoding="ascii")
        except OSError:
            pass  # informational only
