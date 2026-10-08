# CANoe 17 COM API evidence

What has been observed through the CANoe 17 COM interface, as opposed to what
the help says. Each row names its evidence level:

- **documented**: CANoe 17 SP6 help only.
- **demo_verified**: real COM calls against CANoe 17 on a PC without a Vector
  interface. Not proof of behaviour with hardware attached.
- **bench_verified**: real COM calls on a test bench with Vector hardware.

## Environment of the demo runs

| Item | Value |
|---|---|
| CANoe | Test Bench Edition 17.6.5, `Exec64\CANoeTBE.exe`, ProgID `CANoe.Application` (CurVer `CANoe.Application.1`) |
| Licence | **None.** Functions that need an application licence fail (see L1) |
| Hardware | No Vector interface, no ECU |
| Client | Python 3.12, pywin32, early-bound (`gencache`) and late-bound dispatch |
| Configurations | Copies of the `UDSBasic` and `Easy` sample configurations in a scratch folder; originals untouched |
| Date | 2026-10-07 |

## Results

| # | Item | Result | Level | Contract impact |
|---|---|---|---|---|
| A1 | `GetActiveObject("CANoe.Application")` with CANoe not running | Fails with `0x800401E3` (`MK_E_UNAVAILABLE`) | demo_verified | As expected |
| A2 | `GetActiveObject` while a CANoe started through COM is running | **Also fails with `0x800401E3`**. The running-object table is empty | demo_verified | ROT lookup cannot detect a running CANoe. See A3 |
| A3 | `Dispatch("CANoe.Application")` from a second process while CANoe runs | Attaches to the running instance in 0.03 s; same active configuration | demo_verified | Attach = process check, then `Dispatch`. `NO_ACTIVE_INSTANCE` comes from "no CANoe process", not from ROT |
| A5 | Attach-on-read: a new backend, not connected, calls a configuration read while CANoe runs a modified configuration | Attaches (no launch, no Open/Save), takes the session lock, read returns the unsaved state (an added diagnostic description) and `configuration_modified=true`; read epoch = session epoch after attach | demo_verified | S1 implemented: summary, lists, can_controller and Write-window reads attach; status/availability/operation polling never do |
| A4 | `Dispatch` with CANoe not running | Launches `CANoeTBE.exe`; call returns after about 37 s with no configuration loaded (`FullName == ""`) | demo_verified | Launch is a long blocking step; it must run as a queued operation with its own step deadline |
| C1 | `Application.Open(path, False, False)` on a clean configuration | Opens in 4-7 s; `Application.OnOpen(fullName)` event fires about 1.8 s after the call returns | demo_verified | `Open` is a long step; epoch bump on `OnOpen` works |
| C2 | `Application.Open(otherPath, False, False)` while `Configuration.Modified` is `True` | **Does not fail. Opens the other configuration and silently discards the changes.** The help says it fails | demo_verified | The backend must read `Modified` and refuse before calling `Open`. Never rely on CANoe to refuse |
| C6 | `Application.OnOpen` after `Open` in a CANoe that was just launched through COM | Fires **twice** for the one `Open`, 0.06 s apart. Reopening the already-open file also produced a late duplicate (more than 2 s) during a probe run | demo_verified | Every OnOpen bumps the epoch, ours or not; open_config completes 3 s after the last OnOpen and reports the epoch then in force. A very late duplicate can still make the next call STALE (re-read and retry) |
| C7 | `Application.Quit()` on a clean configuration | Works; CANoe exits. Relaunch plus `Open` then took 12.3 s (37 s on the first launch of the day, A4) | demo_verified | `quit` verified |
| C3 | `Configuration.Save(path, False)` and `Save()` | Fail: `0x80020009`, scode `0x8000FFFF`, "Function is only available with valid application license." | demo_verified (unlicensed) | Save, save-copy and `on_dirty="save"` need a licence |
| C4 | `CAPL.Compile()` then `CAPL.CompileResult` | Works unlicensed, 0.39 s on UDSBasic; `result == 0`, empty message and node | demo_verified | Compile is available without a licence |
| C5 | `Node.Active` toggled through COM | `Configuration.Modified` stays `False` | demo_verified | `Modified` does not cover every change. Dirty check is necessary but not sufficient |
| D1 | `DiagDescriptions.Add(network, cddPath)` with a CDD whose qualifier is already loaded | **Succeeds** and adds a second `Door`; sets `Modified`. The help says it fails | demo_verified | `diag:` IDs need the `@n` suffix too; the backend refuses a duplicate itself |
| D6 | `DiagDescriptions.Add` with a *different* CDD file whose ECU qualifier is already loaded | Succeeds; CANoe renames the new description's qualifier to `Door_1` | demo_verified | Only the same file added twice produces true duplicates (D1), which the backend refuses |
| D2 | `DiagDescriptions.Add` return value | An `IDiagDescription`; `Mode == 1` (send only / tester), `Node == ""` | demo_verified | Added descriptions start as tester descriptions |
| D3 | `DiagDescriptions.Remove(index)` | Works by 1-based index | demo_verified | Remove by resolved index, after re-reading the list |
| D4 | `DiagDescription.Mode`, `OpenWindows(1)`, `CloseWindows()` | Work only late-bound; missing from the early-bound 17.6 type library | demo_verified | Backend uses late binding for these members |
| D5 | `DiagDescription.Mode` values | 0 interpretation only, 1 send only (tester), 2 ECU simulation, 3 physical network request, 4 functional group | documented; 1 and 2 observed | `mode` enum gains `functional_group` |
| T1 | Simulation Setup node `Test 3` (UDSBasic) | `Node.TestModule == True` (late-bound). Simulation Setup test nodes have no COM start method; `TSTestModule` exists only under `TestSetup` | demo_verified + documented | Report such nodes as test modules that cannot be started through COM |
| T2 | `TestSetup.TestEnvironments` in UDSBasic | Empty | demo_verified | Test-run demo needs a Test Setup environment |
| M1 | `Measurement.Start()` | Fails: same licence error as C3 | demo_verified (unlicensed) | Measurement, test runs, UDS requests, CAPL calls, Tester Present and frame send cannot be demo-verified without a licence |
| N1 | `SimulationSetup.Nodes.Add(name)`, then `Node.FullName = <.can>`, `AttachBus(bus)`, `IsBusAttached`, `DetachBus`, `Nodes.Remove(index)` | All work unlicensed; Add sets `Modified` | demo_verified | node add/remove/attach/detach implemented |
| N2 | `Node.Active = False` through COM | Works (read back `False`); does not set `Modified` (C5) | demo_verified | node.set_active implemented; previews warn |
| N3 | `Bus.Databases.Add(<.dbc>)`, `Database.Channel`, `Databases.Remove(index)` | Add/Remove work unlicensed; new database gets channel 1. Setting `Channel` to a channel the bus does not have (2 on a 1-channel bus) fails with `0x80020009` "Exception occurred." | demo_verified | database add/remove implemented; channel range pre-checked |
| N4 | `Database.Channel` on an existing channel | Not exercised on a multi-channel bus yet | documented | set_channel stays below the floor until verified |
| N5 | `Node.DetachBus(bus)` when that bus is the node's only attached bus | Fails: `0x80020009`, scode `0x80004021`. Detaching one of two buses works (N1) | demo_verified | Backend refuses detaching the last bus before dispatch |
| B1 | `SimulationSetup.Buses.Add(name)` then `Buses.Remove(index)` / `Remove(name)` | Add returns a bus; Remove by index and by name returned no error but removed nothing, and added buses read back as `Network` | demo_verified | bus.add/remove **not implemented** |
| B2 | Bus type | No bus-type property in the 17 COM `Bus` object (only `AddWithType`) | documented | `BusInfo.bus_type = "unknown"` |
| K1 | `Bus.Channels(n).Controller.Baudrate` read | `500000.0` on UDSBasic CAN (bit/s) | demo_verified | can_controller read implemented |
| K2 | `CANController.Baudrate = v` | Inconsistent: set 500000 -> reads 5000; 125000/250000/83333 -> reads 1000000; does not set `Modified`; reopen restores 500000 | demo_verified | can_controller.set_bitrate **not implemented** (would need `CANSetConfig` with bit timing) |
| T3 | `TestSetup.TestEnvironments.Add(<.tse>)` | Needs an existing file; a new path fails "File not Found!". With a copied sample `.tse` it loads, including its modules (paths resolved relative to the `.tse`) | demo_verified | add_environment pre-checks the file exists |
| T4 | `TestEnvironment.TestModules.Add(<.can>)`, `TSTestModule.Enabled` | Module appended at the end (named `Test 4`); `Enabled` settable; `TestEnvironments.Remove(index, False)` works | demo_verified | add_module and set_enabled implemented |
| T5 | `TSTestModule.Start()` with measurement stopped | Returns without error and without a licence error; effect not observable without a measurement | demo_verified | Test runs stay licence-gated (M1) |
| W1 | `UI.Write.Text` read, `UI.Write.Clear()` | Both work unlicensed; after Clear the text reads back empty | demo_verified | write_window read/clear verified |
| S1 | The stdio server (`canoe17-mcp --backend com`) driven as an MCP client | Gate-3 demo passed: see `demo-2026-10-08.md` | demo_verified | First real end-to-end run |
| U1 | `Networks("CAN").Devices` in UDSBasic | `Tester`, `TestModule`, `SimDiagECU`, `Door` | demo_verified | Diagnostic target for the raw request spike is `Door` |

## Licence error

| Field | Value |
|---|---|
| HRESULT | `0x80020009` (`DISP_E_EXCEPTION`) |
| EXCEPINFO source | e.g. `Measurement::Start` |
| EXCEPINFO description | `Function is only available with valid application license.` |
| EXCEPINFO scode | `0x8000FFFF` (`E_UNEXPECTED`) |

The scode is generic, so the backend classifies by the description text and
reports `ErrorCode.LICENSE_REQUIRED` (keeping the HRESULT), sets
`SessionStatus.licensed = False`, and availability reports
`BlockReason.NO_LICENSE`. This positive identification takes precedence over
the general rule that a dispatched failure is `canoe_rejected`.

## Not yet verified

Items that need a licensed CANoe (demo PC with licence, or the bench):
measurement start/stop events, `Save` to a copy and the `OnOpen` it triggers,
`on_dirty="save"`, test module start/stop and report events, raw and symbolic
UDS requests (`CreateRequestFromStream`, `Pending`, `Responses`), Tester
Present, `CAPL.GetFunction` in `OnInit`, signal/system-variable values and the
CAN controller baud rate.
