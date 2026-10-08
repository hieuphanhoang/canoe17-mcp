# Licensed PC and physical bench checklist

This package is ready for controlled validation of its implemented actions.
It does not yet implement test execution/reports, UDS, Tester Present,
runtime values/CAPL calls or raw frames. Those remain separate acceptance
items. Read [known limitations](../com/known-limitations.md) first.
Report each item as pass, fail, skipped with reason, or deferred.
Installation/fake tests, licensed COM simulation and physical bench evidence
must remain distinct. Do not label a licensed-PC result bench-verified unless
its relevant hardware interaction was actually observed.

## Prepare with the operator

1. Record checkout commit, OS, Python bitness/version, uv/client versions,
   CANoe 17 build/edition and licence availability. Record the Vector
   interface model/driver, physical channel mapping, ECU software identity,
   bus protocol/nominal rate and power/termination conditions when hardware
   is involved. Redact customer identifiers before returning evidence.
2. Arrange a safe bench state and authority for each operation, especially
   measurement and any automatic transmitting nodes/tests. Starting
   measurement can transmit; inspect CAPL/sample behaviour before connecting
   an ECU. Keep an operator able to stop the bench.
3. Make **complete** working copies of the UDSBasic and Easy sample folders
   into an absolute dedicated sandbox, for example `C:/Bench/sample-copies`.
   Preserve dependencies and a baseline copy/hash. Do not use original
   Vector samples or customer projects. Save operator work before tests;
   never run a live suite while another client owns CANoe.
4. The broader edit/demo probes also expect
   `UDSBasic/ProbeTestSetup.tse` and `Easy/CANdb/easy.dbc`; use an existing
   valid copied `.tse` prepared by the operator. Missing prerequisites will
   skip the affected tests, which must be recorded as unverified.
5. Install with `uv sync --frozen --extra com`, configure narrow writable
   roots and a local audit destination, then run `--check`. Its pass proves
   installation prerequisites only. For manual licensed MCP validation,
   explicitly use `min_evidence="documented"` in temporary bench settings;
   restore `demo_verified` afterwards. The backend integration probes
   bypass the MCP catalogue/policy and do not test client confirmation.

## Offline baseline

From the checkout, with a writable disposable temp directory outside source:

```powershell
uv run ruff check .
uv run pyright
uv run pytest -m "not canoe" --basetemp "C:/Bench/review-temp/offline"
uv run --frozen --no-sync canoe17-mcp --check --config "C:/Bench/settings/read-only.toml"
```

Pytest removes/recreates its basetemp: point it only at a disposable test
subdirectory, using forward slashes. Preserve command output and exit codes.

## Licensed COM probes

Only after the operator authorizes live work on sample copies:

```powershell
$env:CANOE17_MCP_PROBE = '1'
$env:CANOE17_MCP_BENCH = '1'
$env:CANOE17_MCP_SANDBOX = 'C:/Bench/sample-copies'
uv run pytest tests/integration/test_bench_licensed.py -m canoe -rA --basetemp "C:/Bench/review-temp/licensed"
```

The three licensed probes must pass, with no licence skips/xfails:

- Save-copy: backup preserves prior destination bytes, active path switches
  to the copy, epoch is current and edits persist after reopening the copy.
- `on_dirty="save"`: current working config is backed up/saved before open,
  and reopening shows persisted edits.
- Measurement: start and stop are confirmed by events/state; an edit during
  measurement is refused. A timeout must not be treated as a successful stop.

These probes use fresh copies under `<sandbox>/bench-work/`. They do not need
Vector hardware or prove the DUT saw bus traffic. After inspecting and
archiving outputs, close those working configurations and have the operator
clean the generated `bench-work/` copies. Preserve the original copied samples.

To rerun the wider licence-free plus licensed integration suite:

```powershell
uv run pytest tests/integration -m canoe -rA --basetemp "C:/Bench/review-temp/integration"
Remove-Item Env:CANOE17_MCP_PROBE, Env:CANOE17_MCP_BENCH, Env:CANOE17_MCP_SANDBOX
```

Record all skips/xfails. Existing demo tests may xfail when a licence is absent;
that is not a licensed acceptance pass. The strict bench probes must fail
instead of masking a missing licence. The wider suite mutates/discards only
sample copies and can launch/open CANoe; use a dedicated operator-approved session.

## Client smoke and supported hardware observations

For each target client, use the [examples](../../clients/README.md) and
[usage skill](../../skills/canoe17/SKILL.md), one active MCP owner at a time.
Record client/model version and actual advertised tool/action list.

1. Read-only: status then summary/list attaches to the existing configuration;
   verify its path and unsaved changes stay intact. Confirm a mutation is
   refused. Demonstrate the second client receives `locked_by_other_server`
   after the first attaches; close the first cleanly and then switch.
2. Write mode on a working copy: preview an approved node/database/diagnostic
   description edit, confirm the same arguments, poll if pending, and verify
   the read-back. Check the corresponding audit operation/outcome record.
3. With temporary lower-floor licensed settings, preview/confirm save-copy,
   inspect its backup and new active path, reopen and verify persistence.
   Do not infer persistence solely from a reopen of an unsaved original.
4. For approved physical measurement, have the operator confirm Vector channel
   mapping in the existing Hardware Config/CANoe setup. Preview active nodes
   and automatic tests, compile, then start/stop and record event-confirmed
   operation states. Observe known permitted traffic in CANoe Trace plus
   the ECU/interface/channel context. Record what was observed rather than
   claiming frames or ECU responses from a measurement flag alone.
5. For local Qwen, use one tool call at a time. Verify it keeps preview
   arguments, polls IDs and handles stale/unknown outcomes without repeating
   mutations. If tool calling is unreliable, stop and record the failure.

On unexpected `outcome_unknown`, modal dialogs or client timeouts, stop
automation, retain IDs, poll/inspect state and reconcile with the operator.
Cancellation cannot prove the physical bench stopped. Never kill an
operator's CANoe or retry a mutating call blindly.

## Deferred physical acceptance

Return these as **deferred**, not pass or implied coverage:

- Test Setup CAPL module execution with verdicts, run-correlated fresh XML/HTML
  report and Write/log artifacts.
- Ad-hoc UDS through a CDD to the intended ECU, including decoded/raw response,
  timeout and negative-response cases; Tester Present start and cleanup.
- Runtime signal/system-variable access, allowlisted CAPL calls and raw frame
  send observed in Trace through the reviewed helper node.
- Hardware-proven database channel assignment on multiple channels. Bus edits
  and baud-rate writes remain deliberately unoffered; do not bypass with raw COM.

Gate 6 is incomplete until implemented required features have their reviewed
hardware evidence. Do not promote `com/evidence.toml` just because a PC has a
licence; propose per-operation evidence changes for independent review.

## Evidence to return

Return a local evidence folder or archive outside source with:

- commit/version/prerequisite record, settings with private paths redacted,
  chosen evidence floor and client/Qwen/Ollama identity;
- exact commands, environment toggles, exit codes and complete pytest summary
  (passes, failures, skips, xfails), plus self-check JSON;
- per-action results, operation IDs/epochs, pending/final/effect state and
  relevant audit slice; screenshots of CANoe path/dirty state/compile/Trace
  and physical channel context where needed;
- backup existence and byte comparison/persistence observations for save
  probes, distinguishing working files from original samples;
- pass/fail/skipped/deferred checklist with explicit evidence axis and
  unexpected COM errors, including pending/unknown operations reconciled;
- final measurement/test/traffic state and operator confirmation of cleanup.

Keep vendor binaries, customer `.cfg`/CDD/DBC/CAPL, licence details and ECU data
out of the public repository. Both agents review the returned results before
per-operation evidence promotion or any release/hardware claim.
