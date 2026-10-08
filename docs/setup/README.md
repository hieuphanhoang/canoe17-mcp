# Install and configure

Run the server and your MCP client on the same Windows bench PC. The COM
backend targets CANoe 17 SP6 Test Bench Edition, 64-bit Python 3.12 or later,
and pywin32. CANoe 20's MCP proxy is not used. A valid CANoe application licence
is needed for save and measurement; installation alone does not provide one.
Vector hardware and an ECU are needed for physical bench evidence.

## Install the locked environment

Use an existing uv installation. From the checkout:

```powershell
uv sync --frozen --extra com
uv run --frozen --no-sync canoe17-mcp --help
```

For an operator environment without development tools, use
`uv sync --frozen --extra com --no-dev`. For the unit/static checks below,
keep the default development group. No global Python packages or CANoe drivers
are installed by this package. The lock pins dependencies; `--frozen` avoids
rewriting it. If Python is 32-bit, recreate this checkout's venv with an
approved 64-bit interpreter before installing. Do not copy someone else's venv.

## Choose settings before registration

Copy [the settings template](../../canoe17-mcp.example.toml) outside the checkout,
or start from [clients/read-only.toml](../../clients/read-only.toml). Use
absolute paths. The example `C:/Bench/...` paths must be replaced; they are
not discovered or created as sample projects. Keep local settings, original
Vector samples and customer projects outside published source.

- `backend_kind="com"`: real Windows backend; `fake` is an explicitly selected
  in-memory simulation. COM failures never fall back to fake.
- `read_only=true`: default. Mutations and their previews are refused. For
  approved edits, use `false` with `allowed_roots` containing only complete
  disposable project copies. Empty roots deny file mutations. Read-only
  inspection can attach to the operator's current project without allowing writes.
- `[backend].lock_key="default"`: use the same key in every client controlling
  this CANoe instance. Do not use a separate key per agent.
- `allow_launch=false` in the client templates prevents accidental launch.
  Enable it only when needed; an open still needs `launch_if_absent=true`.
- `min_evidence="demo_verified"`: default catalogue floor. Save, measurement
  start/stop and database channel assignment are below it. Lowering to
  `documented` is an explicit operator choice for controlled validation on
  working copies; it neither licenses CANoe nor verifies those functions.

TOML reads `[backend]` keys from that table. Environment overrides use
`CANOE17_MCP_<UPPERCASE_FIELD>` (no `BACKEND_` prefix for backend fields).
Paths such as `AUDIT_PATH`, `LOCK_KEY`, `MIN_EVIDENCE` and `BACKEND_KIND` are
plain strings; booleans, numbers and arrays use JSON syntax:

```powershell
$env:CANOE17_MCP_READ_ONLY = 'true'
$env:CANOE17_MCP_ALLOWED_ROOTS = '["C:/Bench/projects"]'
$env:CANOE17_MCP_LOCK_KEY = 'default'
```

Review inherited `CANOE17_MCP_*` variables before troubleshooting: environment
wins over TOML. Client examples also set explicit mode/lock values.

## Check installation without starting CANoe

```powershell
uv run --frozen --no-sync canoe17-mcp --check --config "C:/Bench/settings/read-only.toml"
```

`--check` prints one JSON report to stdout and exits 0 when every required
check passes, or 1 on failure. It loads/validates settings, checks Windows
and 64-bit Python, imports pythoncom/pywintypes/win32com.client, and reads
`CANoe.Application`'s CLSID and LocalServer32 registration. It never creates
a backend, acquires the CANoe session lock, connects or activates COM.
Fake mode skips COM prerequisites and makes no real-readiness claim.

The audit check creates missing parent directories and writes/deletes a small
temporary sibling. It opens an existing audit/lock file for write access
without changing its contents. It does not create audit records or change
registration. A pass cannot guarantee future rotation, free disk space or
lock contention. It also cannot verify CANoe version/running state,
elevation compatibility, application licence or hardware mapping.

## Audit storage

Set `audit_path` to an absolute writable destination. By default it is
`%LOCALAPPDATA%/canoe17-mcp/audit.jsonl`. The path is trusted server settings,
separate from allowed project roots. Confirmed mutations are journaled before
dispatch; reads/previews do not create records. `audit_max_bytes` defaults to
1 MiB per file, and `audit_backup_count` defaults to two rotated files.
An adjacent `.lock` serializes append/rotation across processes.

Audit records include UTC time, tool, redacted parameters, operation IDs,
epoch, effects/outcome, backend and allowed configuration path. They contain
configuration metadata: keep them local and review before sharing. Rotation
discards old records. Archive the relevant slice for bench evidence before
it rotates. Failed pre-dispatch journaling blocks a mutation; `audit_error`
after dispatch means effects may exist and must be reconciled/polled.
Pending-operation tracking is bounded and in memory, not restored on restart.

## Register and inspect

Use the [client examples](../../clients/README.md) for Claude Code, Codex or
opencode/Qwen; registration is an operator action. Give the agent the
[canoe17 skill](../../skills/canoe17/SKILL.md). Start in read-only mode with a
known disposable configuration open in CANoe. Inspect status, then summary
or list. Status alone does not attach; configuration reads attach only to an
already running instance and preserve its current configuration/unsaved work.
Keep client tool timeouts longer than the server's 30 s default wait.
When an operation returns pending, poll its ID instead of repeating it.

Normal stdio server mode writes only MCP JSON-RPC to stdout; diagnostics go
to stderr. Exiting the server releases its worker/lock and leaves CANoe open.
Do not set `--check` in a registered client's command: it exits instead of
serving MCP.

## Troubleshooting

- **pywin32 import or DLL error:** confirm the same venv/interpreter is used by
  the client, run `uv sync --frozen --extra com`, and check Python is 64-bit.
  Use the client's PATH or an absolute uv executable path if `uv` is missing.
- **ProgID missing/wrong version:** `--check` reports the registered local
  server command. Use the approved CANoe 17 installation/repair procedure.
  This package never re-registers COM; runtime attachment validates version.
- **Elevation mismatch/access denied:** run CANoe and the client under the
  same account/elevation. A registry pass does not prove attachment works.
  Do not launch a second instance as an attachment-error workaround.
- **CANoe not running:** `no_active_instance` on a configuration read is
  expected. Have the operator start CANoe, or authorize explicit open/launch.
  An unattached status alone does not prove CANoe is absent.
- **Lock held:** another MCP server owns the session. Shut it down cleanly
  before switching clients. Do not delete mutex state or change lock keys.
- **Licence required/action absent:** verify the CANoe application licence
  with the operator. The default evidence floor may hide save/measurement.
  Fake licence settings and lowered evidence floors cannot enable a licence.
- **Read-only/path denied:** verify explicit server mode and approved roots.
  Do not broaden roots to the entire drive to resolve a refusal.
- **Stale session:** re-read current IDs/configuration and preview again.
- **Pending/client timeout/outcome unknown:** poll a known operation ID and
  inspect fresh state. A timeout/cancel does not establish the bench stopped.
  Do not kill CANoe or reissue a mutation to recover automatically.
- **Audit destination failure:** check directory/file permissions and disk
  space. Resolve journaling before more writes; retain operation IDs for
  any call that may already have changed state.

Read [known COM limitations](../com/known-limitations.md) before write work.
Use the [bench checklist](../validation/bench-checklist.md) for validation and
evidence return; client syntax checks do not establish a client smoke pass.
