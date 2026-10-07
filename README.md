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

Tools are registered only for implemented first-slice actions satisfying the
configured `min_evidence`. Grouped action enums are narrowed accordingly.
State-store status and operation control remain available independently of that
floor. Runtime availability (connection, licence and measurement state) is
reported by `canoe_status`; a temporary block does not remove a tool.

The first slice includes status, configuration summary/open/save/quit, compile,
measurement, operation polling/cancellation, database listing/channel assignment,
diagnostic descriptions/windows and bounded Write-window reads/clear. Other
catalogue tools and later configuration edits are not registered yet.

Read-only mode is enabled by default and rejects mutations, including previews.
In write mode, call a mutation with `confirm = false` to inspect its preview,
then repeat the same arguments with `confirm = true`. Previews bind to their
session epoch. Re-read and preview again after `stale_session`. Opening a file
attaches to CANoe; `canoe_status` does not attach or launch. Launch and dirty-state
handling are explicit open arguments. Cancellation cannot undo a dispatched RPC
or establish that a measurement stopped. Poll returned operation IDs to inspect
completion, failure or an uncertain outcome; do not blindly retry uncertain calls.

Settings load from TOML with `CANOE17_MCP_*` environment overrides. Backend
selection uses `CANOE17_MCP_BACKEND_KIND=com|fake`; arrays, booleans and numbers
use JSON syntax. Backend step/dispatch deadlines and the evidence floor are in
the `[backend]` table. Empty allowed roots deny file mutations.

Validation without CANoe:

```powershell
uv run ruff check .
uv run pyright
uv run pytest -m "not canoe"
```

Live tests are opt-in. See `docs/com/` for evidence and licence limitations.
This milestone does not implement file audit logging, report parsing, hardware
validation, or the deferred catalogue extensions.
