# CANoe 17 MCP

A local stdio MCP server for CANoe 17 SP6 Test Bench Edition. It uses the
[official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk/tree/v1.x).
The COM backend needs Windows, 64-bit Python and CANoe 17. Install with `uv sync
--extra com`, copy `canoe17-mcp.example.toml`, and set absolute `allowed_roots`.

```powershell
uv run canoe17-mcp --config path/to/settings.toml
```

Use that command and arguments in your MCP client's stdio server configuration.
Stdout carries MCP JSON-RPC only; logs go to stderr. The process releases its
backend worker/session lock on normal exit; exiting the server does not quit CANoe.

`backend_kind = "com"` is the default. Set it explicitly in your configuration,
or select `--backend com` / `--backend fake`. There is no automatic fallback.
Fake mode never imports COM or starts CANoe. Its results carry `backend: fake`
and `evidence_axis: fake`; saves and backup names are in-memory simulations.
For a portable smoke test, set `backend_kind = "fake"`, list absolute synthetic
`.cfg` paths in `fake_config_paths`, allow their parent directories, and set
`read_only = false`. `fake_licensed = true` enables simulated licensed operations.
None of those settings provides real CANoe or hardware evidence.

Tools are registered only for implemented actions satisfying the
configured `min_evidence`. Grouped action enums are narrowed accordingly.
State-store status and operation control remain available independently of that
floor. Runtime availability (connection, licence and measurement state) is
reported by `canoe_status`; a temporary block does not remove a tool.

Handlers include status, configuration summary/open/save/quit, compile,
measurement, operation polling/cancellation, database edits, node edits,
Test Setup listing/environment/module edits, CAN controller reads,
diagnostic descriptions/windows and bounded Write-window reads/clear. At the
default real `demo_verified` floor, database channel assignment, save and
measurement actions remain withheld. Bus mutations and CAN bitrate writes
remain unimplemented and unregistered. Fake mode models the supported edits
in memory; synthetic test environments do not parse their `.tse` files.

Read-only mode is enabled by default and rejects mutations, including previews.
In write mode, call a mutation with `confirm = false` to inspect its preview,
then repeat the same arguments with `confirm = true`. Previews bind to their
session epoch. Re-read and preview again after `stale_session`. Configuration
summary, list and controller/Write-window reads attach to an already running
CANoe through the worker and session lock, preserving its unsaved configuration.
They never request a launch or open another file; attach failures propagate.
Status, measurement status and operation polling do not attach or launch. Launch and dirty-state
handling are explicit open arguments. Cancellation cannot undo a dispatched RPC
or establish that a measurement stopped. Poll returned operation IDs to inspect
completion, failure or an uncertain outcome; do not blindly retry uncertain calls.
Confirmed mutations wait up to `[backend].operation_default_timeout_s` (30 s by
default); if launch/open takes longer, poll `canoe_operation` with the returned
`operation_id` instead of issuing the mutation again.

Settings load from TOML with `CANOE17_MCP_*` environment overrides. Backend
selection uses `CANOE17_MCP_BACKEND_KIND=com|fake`; arrays, booleans and numbers
use JSON syntax. Backend step/dispatch deadlines and the evidence floor are in
the `[backend]` table. Empty allowed roots deny file mutations.

Confirmed mutation attempts are journaled before dispatch, followed by operation
state/outcome records, including refusals and errors. Read and confirmation-preview
calls do not create audit records. `audit_path` sets an absolute JSONL destination;
the default is `%LOCALAPPDATA%/canoe17-mcp/audit.jsonl` on Windows (otherwise
`~/.local/share/canoe17-mcp/audit.jsonl`). `CANOE17_MCP_AUDIT_PATH` overrides it
as a plain path string. The log destination is explicitly trusted server settings,
separate from allowed CANoe project roots. `audit_max_bytes` defaults to 1 MiB
per file and `audit_backup_count` to two numbered backups; rotation drops older
records. Their environment overrides use JSON numbers. A small `.lock` file
serializes appends and rotation across server processes.

Each record has UTC `time`, `tool`, redacted `params`, compact `result`, elapsed
`duration`, `call_id` and `backend`. Paths, object names/IDs, free text, returned
payloads and error messages are omitted or redacted. Operation IDs, state, epoch,
dispatch/effect flags and error codes remain available for correlation. Audit
failure before dispatch refuses the mutation. Outcome-write failure adds an
`audit_error` to the response while preserving its result and operation ID;
effects may already have occurred. Polling the operation retries outcome logging.
Pending states, including `outcome_unknown`, are recorded honestly; later states
are appended when the client polls. No final outcome is inferred on process exit.
Up to 128 outstanding calls are tracked per server; poll their terminal outcomes
before issuing more. Tracking is in memory and is not restored after restart.

Validation without CANoe:

```powershell
uv run ruff check .
uv run pyright
uv run pytest -m "not canoe"
```

Live tests are opt-in. See `docs/com/` for evidence and licence limitations.
This milestone does not implement report parsing, hardware
validation, or the deferred catalogue extensions.
