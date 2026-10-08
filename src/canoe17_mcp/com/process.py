"""Find running CANoe processes without opening them.

CANoe 17 does not register in the running-object table (api-evidence A2),
while ``Dispatch`` attaches to a running instance (A3). So "is CANoe
running?" is answered by a process snapshot, before any COM call that could
launch it. Uses the Toolhelp snapshot through ctypes: it lists image names
without opening the processes, so an elevated CANoe is still seen.
"""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

CANOE_IMAGE_NAMES = frozenset(
    {"canoetbe.exe", "canoe64.exe", "canoe32.exe", "canoe.exe", "canalyzer64.exe"}
)

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


def running_processes() -> list[tuple[int, str]]:
    """Return ``(pid, image_name)`` for every process. Windows only."""
    if sys.platform != "win32":
        return []
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or snap is None:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    out: list[tuple[int, str]] = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((int(entry.th32ProcessID), entry.szExeFile))
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return out


def canoe_processes() -> list[tuple[int, str]]:
    return [(pid, name) for pid, name in running_processes() if name.lower() in CANOE_IMAGE_NAMES]
