"""Classify pywin32 COM errors into contract errors.

Kept free of pywin32 imports so it can be unit-tested anywhere: callers pass
the ``hresult`` and ``excepinfo`` fields of a ``pywintypes.com_error``.
"""

from __future__ import annotations

from canoe17_mcp.contracts import ErrorCode, ErrorInfo, Pairs

MK_E_UNAVAILABLE = 0x800401E3
DISP_E_EXCEPTION = 0x80020009
E_ACCESSDENIED = 0x80070005
RPC_E_SERVERCALL_RETRYLATER = 0x8001010A
RPC_E_CALL_REJECTED = 0x80010001
RPC_E_DISCONNECTED = 0x80010108
RPC_S_SERVER_UNAVAILABLE = 0x800706BA
CO_E_SERVER_EXEC_FAILURE = 0x80080005

LICENCE_TEXT = "valid application license"
"""Description CANoe 17 returns for licence-gated functions (api-evidence C3/M1)."""

_DISCONNECTED = {RPC_E_DISCONNECTED, RPC_S_SERVER_UNAVAILABLE}
_BUSY = {RPC_E_SERVERCALL_RETRYLATER, RPC_E_CALL_REJECTED}


def u32(value: int | None) -> int | None:
    return None if value is None else value & 0xFFFFFFFF


def is_licence_error(description: str | None) -> bool:
    return bool(description) and LICENCE_TEXT in description.lower()


def classify(
    hresult: int,
    excepinfo: tuple | None,
    *,
    action: str,
    details: Pairs = (),
) -> ErrorInfo:
    """Map a COM failure of a *dispatched* call to an ErrorInfo.

    Never returns DIRTY_CONFIG: that code comes only from the pre-dispatch
    Modified check (contract rule 4).
    """
    hr = u32(hresult) or 0
    source = description = None
    scode: int | None = None
    if excepinfo:
        source = excepinfo[1]
        description = excepinfo[2]
        scode = u32(excepinfo[5]) if len(excepinfo) > 5 else None
    extra: Pairs = (("action", action),)
    if source:
        extra += (("source", str(source)),)
    if scode:
        extra += (("scode", f"0x{scode:08X}"),)
    extra += details

    if is_licence_error(description):
        return ErrorInfo(
            ErrorCode.LICENSE_REQUIRED,
            f"{action}: CANoe reports that this function needs a valid application licence.",
            retryable=False,
            hresult=hr,
            details=extra,
        )
    if hr in _DISCONNECTED:
        return ErrorInfo(
            ErrorCode.NOT_CONNECTED,
            f"{action}: lost the connection to CANoe.",
            retryable=False,
            hresult=hr,
            details=extra,
        )
    if hr in _BUSY:
        # The call was rejected by the COM server's message filter: CANoe did
        # not run it. Safe to report as rejected-but-retryable.
        return ErrorInfo(
            ErrorCode.CANOE_REJECTED,
            f"{action}: CANoe was busy and rejected the call.",
            retryable=True,
            hresult=hr,
            details=extra,
        )
    text = description or f"COM error 0x{hr:08X}"
    return ErrorInfo(
        ErrorCode.CANOE_REJECTED,
        f"{action}: {text}",
        retryable=False,
        hresult=hr,
        details=extra,
    )


def attach_error(hresult: int, excepinfo: tuple | None) -> ErrorInfo:
    """Classify a failure while attaching. Never triggers a launch."""
    hr = u32(hresult) or 0
    reason = "access denied (elevation mismatch?)" if hr == E_ACCESSDENIED else "attach failed"
    desc = excepinfo[2] if excepinfo and len(excepinfo) > 2 and excepinfo[2] else ""
    return ErrorInfo(
        ErrorCode.ATTACH_FAILED,
        f"Could not attach to the running CANoe: {reason}. {desc}".strip(),
        retryable=False,
        hresult=hr,
    )
