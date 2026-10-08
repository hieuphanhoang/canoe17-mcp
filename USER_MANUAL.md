# canoe17-mcp user manual

canoe17-mcp lets an AI assistant (Claude Code, Codex, or opencode with a local
model) work with **Vector CANoe 17** on the same Windows PC. The assistant can
inspect a configuration, edit nodes, databases, diagnostic descriptions and the
test setup, compile CAPL, and, on a licensed PC, save and run measurements.
Every change is shown to you first and needs your confirmation.

It is an [MCP](https://modelcontextprotocol.io) server: your AI client starts
it as a local program and talks to it. It drives CANoe through CANoe's COM
interface. It never edits `.cfg` files as text, and never runs arbitrary
commands.

Contents:

1. [What you need](#1-what-you-need)
2. [Install](#2-install)
3. [Configure](#3-configure)
4. [Check the installation](#4-check-the-installation)
5. [Connect your AI client](#5-connect-your-ai-client)
6. [First session](#6-first-session)
7. [How changes work](#7-how-changes-work)
8. [Tools](#8-tools)
9. [Common tasks](#9-common-tasks)
10. [Messages and what to do](#10-messages-and-what-to-do)
11. [Limits](#11-limits)
12. [Update, move, remove](#12-update-move-remove)

---

## 1. What you need

| Item | Requirement |
|---|---|
| Operating system | Windows |
| CANoe | CANoe 17, installed and registered (developed and tested on 17.6.5 Test Bench Edition) |
| Python | 64-bit Python 3.12 or newer |
| uv | The [uv](https://docs.astral.sh/uv/) package manager |
| AI client | Claude Code, Codex, or opencode, running on the **same PC** as CANoe |
| CANoe licence | Needed for saving and for measurements. Without one, you can still inspect and edit, but not save |
| Vector hardware and an ECU | Only for real bus traffic. Not needed for configuration work |

Run CANoe and your AI client as the **same Windows user, with the same
elevation**: both normal, or both "Run as administrator". If they differ, the
server cannot connect to CANoe.

## 2. Install

Copy or clone the `canoe17-mcp` folder to the PC, for example
`C:\Tools\canoe17-mcp`. Do not copy a `.venv` folder from another PC. Then, in
that folder:

```powershell
uv sync --frozen --extra com --no-dev
```

This creates a private Python environment inside the folder with the exact,
locked package versions, including `pywin32` for COM. Nothing is installed
system-wide. Leave out `--no-dev` if you also want to run the tests.

## 3. Configure

Create a settings file **outside** the `canoe17-mcp` folder, for example
`C:\Tools\canoe17-settings\canoe17.toml`. Start from one of the examples:

- `clients/read-only.toml`: inspect only, no changes possible. Use this first.
- `clients/write.toml`: changes allowed inside the folders you list.

Example for write mode:

```toml
backend_kind = "com"          # real CANoe; "fake" is a simulation for testing only
read_only = false             # true = no changes at all (the default)
allowed_roots = ["C:/Bench/projects"]   # changes only to files inside these folders
# audit_path = "C:/Bench/logs/canoe17-mcp/audit.jsonl"   # optional, see section 7

[backend]
lock_key = "default"          # keep "default"; see "One owner at a time" below
allow_launch = false          # true lets the server start CANoe when asked to open a file
min_evidence = "demo_verified"
```

| Setting | Meaning |
|---|---|
| `read_only` | `true` refuses every change, including previews. Default `true`. |
| `allowed_roots` | Absolute folders. Opening, saving and adding files only works inside them. Empty means no file changes. Use folders with **working copies** of projects, never your only copy. |
| `backend_kind` | `com` uses the real CANoe. `fake` is an in-memory simulation that never touches CANoe; its results are marked `fake`. There is no automatic switch between them. |
| `audit_path` | Where the change log goes. Default `%LOCALAPPDATA%\canoe17-mcp\audit.jsonl`. |
| `lock_key` | Name of the lock that lets only one server control CANoe. Keep the same value everywhere. |
| `allow_launch` | Whether an open request may start CANoe if it is not running. |
| `min_evidence` | How proven an action must be before the server offers it (section 11). Leave at `demo_verified`. |

Any setting can also be given as an environment variable,
`CANOE17_MCP_<NAME>` (for example `CANOE17_MCP_READ_ONLY=true`). Environment
variables win over the file.

## 4. Check the installation

```powershell
uv run --frozen --no-sync canoe17-mcp --check --config C:\Tools\canoe17-settings\canoe17.toml
```

The check prints a JSON report and ends with `"ok": true` when everything is
in place. It checks the settings file, Windows, 64-bit Python, pywin32, that
CANoe is registered, and that the audit log folder is writable. It **never
starts or connects to CANoe**, so it cannot check the licence, the CANoe
version or the elevation match.

## 5. Connect your AI client

Ready-made files are in `clients/`. Replace the example paths
(`C:/Bench/...`) with yours. They all start the server like this:

```text
uv --directory <canoe17-mcp folder> run --frozen --no-sync canoe17-mcp --config <settings.toml>
```

**Claude Code.** Either copy `clients/claude-code/read-only.mcp.json` as
`.mcp.json` into the project folder you work in, or run:

```powershell
claude mcp add canoe17 --scope project -- uv --directory C:\Tools\canoe17-mcp run --frozen --no-sync canoe17-mcp --config C:\Tools\canoe17-settings\canoe17.toml
```

Start a new Claude Code session in that folder and approve `canoe17` when
asked. `/mcp` shows whether it is connected.

**Codex.** Add the `[mcp_servers.canoe17]` block from
`clients/codex/read-only.config.toml` to `%USERPROFILE%\.codex\config.toml`,
or use `codex mcp add`.

**opencode with a local model.** Merge `clients/opencode/read-only.opencode.json`
into your opencode configuration. Small local models follow the rules better
with the skill below.

**Give the assistant the skill.** `skills/canoe17/SKILL.md` teaches an
assistant the rules in section 7. Install it as a skill in your client, or
paste it into the assistant's instructions.

**One owner at a time.** Only one server can control CANoe at a time. If
Claude Code and Codex both have canoe17, whichever connects to CANoe second
gets "locked by another server" until the first one exits. Do not give the
servers different `lock_key` values to work around this; that would let two
assistants change CANoe at once.

## 6. First session

1. Start CANoe and open a **copy** of a project, or one of Vector's sample
   configurations copied into an allowed folder.
2. Start your AI client and check the server is connected.
3. Ask, for example: *"Show the CANoe status, then summarize the open
   configuration."*

`canoe_status` only reports what the server already knows. It never connects
and never starts CANoe, so at first it says `connected: false`. Reading the
configuration summary (or any list) then connects to the running CANoe. That
read does not open, save or change anything, and unsaved changes in CANoe stay
as they are. If CANoe is not running, you get `no_active_instance`. The server
does not start CANoe for a read.

## 7. How changes work

**Preview, then confirm.** Every change is a two-step call:

1. The assistant calls the tool with `confirm: false`. Nothing changes; the
   server returns a **preview**: which file or object is affected, what will
   be overwritten, whether unsaved work would be lost, whether CANoe would be
   started, which nodes may transmit when a measurement starts, and anything
   that blocks the change.
2. If you agree, the assistant repeats the **exact same call** with
   `confirm: true`. Only then does anything happen.

Ask the assistant to show you the preview before it confirms. The server
does not enforce that a person saw it: a `confirm: true` call is checked
against a fresh preview (anything blocking still stops it) and then carried
out. So the preview you see must be for exactly the arguments that are
confirmed. The `canoe17` skill tells the assistant to work this way, and your
AI client's own approval prompts add a second check.

**Unsaved work.** Opening another configuration or quitting CANoe asks what to
do with unsaved changes (`on_dirty`):

| `on_dirty` | Effect |
|---|---|
| `refuse` (default) | Refused if there are unsaved changes. |
| `save` | Saves first, after making a backup of the file. Needs a licence. |
| `discard` | Throws the unsaved changes away. |

`confirm: true` alone never discards anything. Some changes are not marked as
unsaved by CANoe at all (activating or deactivating a node), so `refuse`
cannot protect them; save before switching configurations.

**Backups.** Every save that overwrites a file first copies it to
`<file>.bak-<date-time>`. If that copy fails, nothing is saved.

**Session changes.** When the configuration open in CANoe changes, because you
opened another one in the CANoe window or the assistant reopened one, all
earlier previews and object IDs become invalid. The next change then fails
with `stale_session`. The assistant should read the configuration again,
preview again, and confirm again. This protects you from a change meant for
one configuration landing in another.

**Long operations.** Opening, compiling and starting CANoe can take a while.
The server waits up to 30 s. If an action is still running after that, it
returns an `operation_id`, and the assistant checks on it with
`canoe_operation`. It must **not** send the change again. If a call to CANoe
does not return in time, the result is `outcome_unknown`: the change may or
may not have happened. The server then accepts no new changes until that
operation is resolved; reads still work.

**Audit log.** Every confirmed change is written to the audit log
(`audit_path`): first the request, then the result. A record keeps the tool,
the object IDs, bus names, file paths inside your allowed folders and the
configuration that was open. Text the assistant made up (for example a new
node's name), data payloads and error messages are replaced by `[redacted]`.
If the request cannot be written to the log, the change is refused. The log is
limited to 1 MiB per file with two older files kept; older records are
dropped. Keep it local; it describes your projects.

## 8. Tools

The server offers only the tools and actions that are implemented **and**
proven on a real CANoe (section 11). With the default settings these are:

| Tool | Actions | What it does |
|---|---|---|
| `canoe_status` | read | Connection, CANoe version, open configuration, unsaved changes, measurement state, licence state, read-only mode, allowed folders, and which actions are available right now. Never connects. |
| `canoe_get_config_summary` | read | Buses, databases, nodes, diagnostic descriptions and test setup. `section` limits it: `all`, `networks`, `databases`, `nodes`, `diagnostics`, `tests`, `logging`, `panels`. |
| `canoe_open_config` | open | Opens a `.cfg` inside the allowed folders (`path`, `on_dirty`, `launch_if_absent`). |
| `canoe_quit` | quit | Closes CANoe (`on_dirty`). |
| `canoe_compile` | compile | Compiles all CAPL nodes; returns success and the first error. |
| `canoe_node` | list, add, remove, set_active, attach_bus, detach_bus | Simulation nodes. `add` takes `name`, `bus` and an optional CAPL file (`capl_path`). |
| `canoe_database` | list, add, remove | Databases on a bus (`path`, `bus`, `channel`). |
| `canoe_diag_description` | list, add, remove, open_windows, close_windows | Diagnostic descriptions (CDD, ODX/PDX) and the Diagnostic Console. `qualifier` takes the `diag:` ID from `list`. |
| `canoe_test_setup` | list, add_environment, add_module, set_enabled | Test environments (from an existing `.tse`) and test modules. |
| `canoe_can_controller` | read | The CAN baud rate of a bus channel. |
| `canoe_write_window` | read, clear | The text in CANoe's Write window. |
| `canoe_measurement` | status | Measurement state. `start` and `stop` appear after they are verified on a licensed PC. |
| `canoe_operation` | status, cancel | Checks on or cancels a long operation by `operation_id`. Cancelling cannot undo something CANoe already did. |

**Object IDs.** Objects are named by IDs the server returns in lists and
summaries, such as `node:Tester`, `db:easy`, `diag:Door`, `env:Tests`,
`tm:Tests/MyModule`. Same-named objects get `@1`, `@2`. Always take IDs from a
fresh list; they become invalid when the configuration changes.

**Defined but not offered yet:** `canoe_save_config`, measurement start/stop
and database channel changes (waiting for licensed verification). Bus
add/remove and baud-rate changes are withheld because CANoe 17 does not
perform them correctly. Test runs, diagnostic requests, Tester Present, signal
and system-variable values, CAPL calls and CAN frames are not built yet.

## 9. Common tasks

Ask in plain language; the assistant picks the tools. Examples:

- **Inspect:** "What configuration is open in CANoe, and does it have unsaved
  changes? List the nodes and diagnostic descriptions."
- **Open a project copy:** "Open C:/Bench/projects/door/door.cfg. If there are
  unsaved changes, stop and tell me." (`on_dirty = refuse`)
- **Add a diagnostic description:** "Add C:/Bench/projects/door/Cdd/Door.cdd to
  network CAN and open the Diagnostic Console." If a description with the
  same ECU name exists, CANoe names the new one `Door_1`.
- **Add a node:** "Add a node EngineSim on bus CAN with
  C:/Bench/projects/door/Nodes/EngineSim.can, then compile."
- **Add a test environment:** "Add the test environment
  C:/Bench/projects/door/Tests.tse and disable module X." The `.tse` file must
  already exist.
- **Throw away experiments:** "Reopen the configuration and discard the
  changes."

Edits stay in CANoe's memory until the configuration is saved. Saving needs a
licence; without one, save in the CANoe window yourself if you want to keep a
change.

## 10. Messages and what to do

Errors come back as `"error": {"code": ..., "message": ...}`.

| Code | Meaning | What to do |
|---|---|---|
| `no_active_instance` | CANoe is not running. | Start CANoe, or open a file with `launch_if_absent` if launching is allowed. |
| `attach_failed` | CANoe runs but the server cannot connect. | Usually an elevation mismatch (section 1). Run both the same way. |
| `locked_by_other_server` | Another canoe17 server controls CANoe. | Close the other client's session first. |
| `dirty_config` | Unsaved changes would be lost. | Decide: save (licence), discard, or keep working. |
| `stale_session` | The configuration changed since the preview or list. | Read again, preview again, confirm again. |
| `license_required` | CANoe needs a licence for this. | Use a licensed PC, or do it in CANoe yourself. |
| `measurement_running` / `measurement_not_running` | Not possible in the current measurement state. | Stop or start the measurement first. |
| `read_only_mode` | The server is in read-only mode. | Change `read_only` in the settings if changes are intended. |
| `path_not_allowed` | The file is outside `allowed_roots`. | Use a copy inside an allowed folder, or add that folder. Do not allow a whole drive. |
| `not_found` / `ambiguous_id` / `already_exists` | Wrong or duplicate ID or file. | List again and use the exact ID. |
| `busy`, `deadline_exceeded` | CANoe was busy; **nothing was sent**. | Try again. |
| `outcome_unknown` | Sent, but the result is unknown. | Check with `canoe_operation`; look at CANoe; do not just repeat. |
| `capability_unavailable` | Not offered (not built, not proven, or blocked right now). | See the `availability` list in `canoe_status`. |
| `canoe_rejected` | CANoe refused the call. | Read the message; check the file or object in CANoe. |

The installation checks and fixes for common problems are in
`docs/setup/README.md`.

## 11. Limits

- **Licence.** Without a CANoe licence: no saving, no measurements, so no test
  runs or bus activity. Inspecting and editing work.
- **How proven an action is.** Every action has an evidence level:
  `documented` (CANoe help only), `demo_verified` (tested on a real CANoe
  without hardware) or `bench_verified` (tested with hardware). The server
  offers only actions at or above `min_evidence`. Save and measurement are
  still `documented` because they could not be tested without a licence, so
  they are hidden by default. Lowering `min_evidence` to `documented` shows
  them; do that only on a test setup with project copies.
- **CANoe 17 behaviours to know** (details in `docs/com/known-limitations.md`):
  - opening another configuration silently discards unsaved changes in CANoe itself; the server prevents this;
  - node activation is not tracked as unsaved;
  - removing buses and writing the baud rate do not work correctly through COM;
  - test nodes in the Simulation Setup cannot be started through COM;
  - CANoe can report one open twice, which sometimes causes a `stale_session` right after an open.
- **Not proven on a bench yet:** nothing in this version has been checked
  with Vector hardware. `docs/validation/bench-checklist.md` describes that check.

## 12. Update, move, remove

- **Update:** replace the `canoe17-mcp` folder with the new version (or pull
  it), then run `uv sync --frozen --extra com --no-dev` again and the check
  from section 4.
- **Another PC:** copy the folder without `.venv`, then follow sections 2-5
  there. Settings files contain that PC's paths; create them per PC.
- **Remove:** remove the `canoe17` entry from your AI client, then delete the
  `canoe17-mcp` folder, your settings file and, if you no longer need it, the
  audit log folder (`%LOCALAPPDATA%\canoe17-mcp`). Nothing else is installed.
