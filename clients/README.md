# Client examples

These are examples, not registrations. Adapt `C:/Bench/canoe17-mcp`,
`C:/Bench/settings` and `C:/Bench/projects` to absolute paths on the bench PC.
Copy [read-only.toml](read-only.toml) or [write.toml](write.toml) to the settings
directory. Also update path strings inside the client examples, including the
write-mode `CANOE17_MCP_ALLOWED_ROOTS` JSON array. Keep production projects
outside the writable roots. Install first as described in the [README](../README.md#2-install).

Use one variant per client, under the same server name `canoe17`. Start with
read-only: previews are mutations too and will be refused. Client permissions
do not override server safety settings. Environment settings override TOML;
these examples explicitly pin read-only/write mode and the lock key.

All clients must share `[backend].lock_key = "default"` for the same CANoe
instance. The server holds the lock after attachment until shutdown/quit;
a second server receives `locked_by_other_server` (`LOCKED_BY_OTHER_SERVER`
in the Python enum). Status/discovery alone do not prove ownership. Close the
first client's MCP server before switching clients. Do not choose different
keys to bypass the lock. Shutdown releases the lock without quitting CANoe.

## Claude Code

Merge `claude-code/read-only.mcp.json` into the chosen project's `.mcp.json`;
preserve other server entries. To enable approved writes, use
`claude-code/write.mcp.json` instead. Alternatively, run these **operator
registration commands** from that project's directory (choose one):

```powershell
claude mcp add --scope project --env CANOE17_MCP_READ_ONLY=true --env CANOE17_MCP_LOCK_KEY=default --transport stdio canoe17 -- uv --directory "C:/Bench/canoe17-mcp" run --frozen --no-sync canoe17-mcp --config "C:/Bench/settings/read-only.toml"
claude mcp add --scope project --env CANOE17_MCP_READ_ONLY=false --env 'CANOE17_MCP_ALLOWED_ROOTS=["C:/Bench/projects"]' --env CANOE17_MCP_LOCK_KEY=default --transport stdio canoe17 -- uv --directory "C:/Bench/canoe17-mcp" run --frozen --no-sync canoe17-mcp --config "C:/Bench/settings/write.toml"
```

Check `claude mcp list` and `/mcp`. Project registrations may require trust
approval in Claude Code. Syntax follows the [official Claude Code MCP docs](https://code.claude.com/docs/en/mcp).

## Codex

Merge the table from `codex/read-only.config.toml` into the operator's
`~/.codex/config.toml`, or use `codex/write.config.toml` for approved writes.
Alternatively, choose one **operator registration command**:

```powershell
codex mcp add canoe17 --env CANOE17_MCP_READ_ONLY=true --env CANOE17_MCP_LOCK_KEY=default -- uv --directory "C:/Bench/canoe17-mcp" run --frozen --no-sync canoe17-mcp --config "C:/Bench/settings/read-only.toml"
codex mcp add canoe17 --env CANOE17_MCP_READ_ONLY=false --env 'CANOE17_MCP_ALLOWED_ROOTS=["C:/Bench/projects"]' --env CANOE17_MCP_LOCK_KEY=default -- uv --directory "C:/Bench/canoe17-mcp" run --frozen --no-sync canoe17-mcp --config "C:/Bench/settings/write.toml"
```

Check `codex mcp list` and `/mcp`. CLI and IDE use the shared configuration.
The table examples include 30 s startup and 60 s tool timeouts; add these after
CLI registration if needed. Syntax follows the [official Codex MCP docs](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).

## opencode with local Qwen

Merge `opencode/read-only.opencode.json` into the chosen project's
`opencode.json`; use the write variant only for approved work. These examples
target the `provider` / `mcp.<name>` configuration documented at
[opencode MCP](https://opencode.ai/docs/mcp-servers/) and
[opencode Ollama providers](https://opencode.ai/docs/providers/#ollama).
OpenCode v2 uses a different configuration (`providers`, `mcp.servers`);
do not copy this layout into a v2 client without translating it using its docs.

An existing local Ollama server at `127.0.0.1:11434` and a locally installed
`qwen3:8b` tag are prerequisites, not installed by these files. Replace both
the model ID and provider model key if using another local Qwen tag.
Verify the selected model can emit tool calls. If it cannot, stop rather than
turning its prose into automatic mutations. The 8B example is not a bench-tested
model recommendation. Increase Ollama's context to 16k-32k if tool calls fail,
following the provider docs; this package does not change model settings.

Give every agent the [canoe17 skill](../skills/canoe17/SKILL.md) before write
work. For a small model, ask for one read or one preview at a time. The
files have been syntax/policy checked; actual client connections and local
model tool use need operator smoke testing.
