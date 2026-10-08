---
name: canoe17
description: Use the canoe17 MCP tools to inspect or edit a Vector CANoe 17 configuration, compile CAPL, or handle supported save and measurement operations on a local bench. Covers confirmation, stale sessions, operation polling and evidence limits.
---

# Use CANoe 17 safely

Work one step at a time. Use the tools actually advertised by this server.
Read `canoe_status` first: check backend, connection, configuration path,
dirty state, measurement and capabilities. A fake result proves only a
simulation. Missing tools/actions are unavailable; do not invent a command
or use arbitrary COM or text edits to the proprietary `.cfg` to bypass it.

## Inspect before opening

`canoe_status`, measurement status and operation polling never attach or launch.
An unattached status is not proof CANoe is absent. Use
`canoe_get_config_summary` or a configuration list/read to attach to an already
running CANoe without opening another file. Reads preserve unsaved changes.
`no_active_instance` means an operator must start CANoe or explicitly authorize
open with `launch_if_absent=true` when server launch settings permit it.
An attachment error must be resolved, not followed by a blind launch.

Opening a configuration is a mutation. Choose an absolute `.cfg` in approved
roots. Default to `on_dirty="refuse"` and `launch_if_absent=false`.
`confirm=true` does not authorize discard. Use `on_dirty="save"` or `"discard"`
only when the user requested that handling. Save requires a licence; node
activation can change without setting Modified, so Modified=false is not a
complete guarantee that edits will survive reopening.

## Preview, then confirm

1. Read the relevant list/summary and take current IDs from that response.
2. Call the requested mutation with `confirm=false`.
3. Read `needs_confirmation`, affected paths, warnings and blockers. Follow
   the user's authorization and client approval policy before proceeding.
4. Repeat the **same tool and arguments**, changing only `confirm` to `true`.
5. Inspect the result and poll its operation ID if it is still pending.

Example for an approved existing working copy (substitute its actual path):

```json
{"path":"C:/Bench/projects/demo/demo.cfg","on_dirty":"refuse","launch_if_absent":false,"confirm":false}
```

Send that to `canoe_open_config`. After reviewing the preview and receiving
any required approval, send the same object with `"confirm":true`.
Do not change the path, dirty policy or IDs between preview and confirmation.
In server read-only mode even previews are refused: do not request a settings
change unless write work is part of the user's task.

## Recover without duplicate effects

- `stale_session`: an open/reopen or list change invalidated IDs/previews.
  Read again, obtain fresh IDs and preview again before confirming.
- Pending operation: retain `operation_id` and call `canoe_operation` with
  `{"action":"status","operation_id":"<returned ID>"}`. Pause briefly
  between polls; never repeat the originating mutation to wait for it.
- `outcome_unknown` or a client timeout: do not retry the mutation blindly.
  Poll a known ID and inspect fresh state. If no ID arrived, ask the operator
  to reconcile state before another mutation. Cancellation only requests a
  stop and cannot undo a dispatched RPC or prove measurement stopped.
- `locked_by_other_server`: close the other client's server cleanly or wait.
  Never change `lock_key` to obtain concurrent ownership.
- `license_required`: stop that path. Fake licence settings do not license
  CANoe. Never lower the evidence floor merely to make a missing action appear.
- `audit_error`: effects may already have occurred. Keep the operation ID,
  poll it to retry outcome logging, and resolve the log destination problem.

## Respect the current implementation

The default `demo_verified` floor withholds save, measurement start/stop and
database channel assignment. A licensed bench profile may explicitly use
`min_evidence="documented"` to validate supported lower-evidence operations;
success is evidence to record, not a reason to label all features verified.
Compile can fail; report its actual diagnostics and do not start measurement
after errors. Before any measurement, inspect the preview's active nodes,
automatic tests and potential bus traffic with the operator.

Test Setup listing/editing is supported; test execution, report parsing,
UDS requests, Tester Present, runtime values, CAPL calls, raw-frame helpers,
bus edits and baud-rate writes are deferred. A Diagnostic Console opening is
not proof a UDS request succeeded. Reopening is not proof unsaved changes were
saved. Use [known limitations](../../docs/com/known-limitations.md) for COM
behaviour and the [bench checklist](../../docs/validation/bench-checklist.md)
for licensed/hardware evidence. When copying this skill alone into a client's
skill directory, resolve those references from the installed server checkout.
