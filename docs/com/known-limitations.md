# CANoe 17 known limitations

What CANoe 17.6 does through COM that you might not expect, and what canoe17-mcp
does about it. Read this before using the server on a test bench. Each item
cites its evidence row in [api-evidence.md](api-evidence.md); "demo" means
observed on a PC without a licence or hardware, "documented" means CANoe help
only.

## Things that can lose work

| CANoe behaviour | What the server does |
|---|---|
| Opening another configuration while the current one has unsaved changes does **not** fail. CANoe silently discards the changes, although the help says the call fails (C2, demo). | Before every open or quit, the server reads `Modified` and refuses with `dirty_config` unless you choose `on_dirty="save"` or `"discard"`. `confirm=true` alone never discards. |
| Some changes are never marked unsaved. Activating or deactivating a node through COM leaves `Modified` false (C5, N2, demo). | The node preview warns about it. The `refuse` setting cannot protect such changes, so save before you open another configuration. |
| Saving to a new path makes the copy the active configuration (documented). | The save result reports the new active path. An existing destination file is backed up first (`.bak-<timestamp>`); if the backup fails, nothing is saved. |

## Things that need a licence

Without an application licence, `Measurement.Start` and `Configuration.Save`
fail with "Function is only available with valid application license." (M1,
C3, demo). Everything that depends on them is also unavailable:
save/save-copy, `on_dirty="save"`, measurement, test runs and reports,
diagnostic requests, Tester Present, CAPL calls, signal and system-variable
values, raw frames. The server reports `license_required` and marks the
session `licensed=false`. Configuration edits (nodes, databases, diagnostic
descriptions, Test Setup) work unlicensed but cannot be saved without one.

## Things the server deliberately does not offer

| Action | Why |
|---|---|
| Adding or removing buses | `Buses.Remove` removed nothing, by index or by name, and added buses read back as `Network` (B1, demo). |
| Writing the CAN baud rate | `Baudrate` writes read back inconsistent values (500000 -> 5000, 125000 -> 1000000) and are not marked unsaved (K2, demo). Use CANoe's own dialog. Reading it works (K1). |
| Starting Simulation Setup test nodes | They have no COM start method (T1). Only Test Setup modules can be started; listed as `tm-sim:` with `startable=false`. |
| Detaching a node's last bus | CANoe rejects it (N5, demo); the server refuses before sending anything. |
| Changing a database channel | Not exercised on a multi-channel bus yet; stays below the evidence floor (N4). |

## Identity and session surprises

| CANoe behaviour | What the server does |
|---|---|
| CANoe does not register in the running-object table; `GetActiveObject` fails even while it runs (A2). `Dispatch` attaches to the running instance (A3). | "Is CANoe running?" is a process check. Without a CANoe process, reads report `no_active_instance` and never launch; only an explicit open with `launch_if_absent` starts CANoe. |
| `OnOpen` can fire twice for one open, sometimes seconds apart (C6). | Every `OnOpen` makes earlier previews and IDs stale; an open completes 3 s after the last `OnOpen`. A very late duplicate can make the next call fail with `stale_session`: re-read and preview again. |
| Adding a second CDD whose ECU qualifier is taken renames it (`Door_1`); adding the same file twice creates true duplicates (D1, D6). | Same-file duplicates are refused (`already_exists`). IDs of same-named objects carry `@n` and become stale when their list changes. |
| Some members are missing from the 17.6 type library: `DiagDescription.Mode`, `OpenWindows`, `CloseWindows`, `Node.TestModule`, `TestEnvironment.TestModules` (D4, T1). | The backend uses late binding for them. |
| A test environment can only be added from an existing `.tse` file (T3). | The server checks the file exists first. |
| Launching CANoe takes about 12-37 s; opening takes 4-12 s (A4, C1, C7). | Opens run as operations. If the 30 s client wait ends first, poll `canoe_operation`; do not issue the open again. |

## Not verified at all yet

Everything that needs a licence or hardware: measurement events, test runs and
report files, UDS requests and responses, Tester Present, CAPL function calls,
signal values, frames on a real bus, and channel mapping to a Vector interface.
Run the bench profile of the integration tests on a licensed bench PC to verify
them (see `tests/integration/conftest.py`).
