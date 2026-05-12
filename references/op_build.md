# build operations

All operations are invoked via the `vivado_op.py` JSON dispatcher.
See [SKILL.md](../SKILL.md) for the invocation pattern.

Drives synthesis and implementation on the project's **active** runs.
Active = whatever Vivado returns from `current_run -synthesis` /
`current_run -implementation`. For a fresh project that's `synth_1` /
`impl_1`; if you've cloned strategies, it's whichever you marked
active. The operations never invent run names.

If you need to drive a non-active run, drop down to raw Tcl via the
`exec_tcl.py` escape hatch -- see [SKILL.md](../SKILL.md) for when
that's appropriate.

## Common shape

All operations return a dict with at least:

| Field | Meaning |
|---|---|
| `success` (bool) | Did the operation finish without errors? |
| `error_kind` (str or None) | Set on failure: `"not_found"`, `"run_failed"`, `"upstream_failed"`, `"timeout"`, `"tcl_error"`, `"bad_arg"`, ... |
| `message` (str) | One-line summary, safe to print. |
| `warnings` (list of str) | Non-fatal notes. |

On `tcl_error` failures, the result also carries `error_info` (Vivado's
Tcl stack trace) and `error_code` so you can diagnose what Vivado
specifically refused.

Operation-specific fields are listed below.

## Operations

### build.summary

One-shot snapshot of build state. Use this as the first call in a
session: it tells you whether there's a project, which runs are
active, what their statuses are, whether a bitstream is on disk, and
whether you can program now.

**Request:**
```json
{"op": "build.summary", "params": {}}
```

**Response:**
```json
{
  "success": true,
  "synth_run": "synth_1", "impl_run": "impl_1",
  "synth_status": "synth_design Complete!", "synth_complete": true, "synth_failed": false,
  "impl_status": "write_bitstream Complete!", "impl_complete": true, "impl_failed": false,
  "bit_path": ".../blink.bit", "ltx_path": ".../blink.ltx",
  "bit_exists": true, "ltx_exists": true,
  "wns": 0.231, "tns": 0.0, "met_timing": true,
  "timing_report_path": ".../impl_1/blink_timing_summary_routed.rpt",
  "ready_to_program": true
}
```

`ready_to_program` is `impl_complete and not impl_failed and bit_exists`.

`met_timing` is a separate axis from `ready_to_program` -- a build can
be "ready to program" (bitstream exists) and still have negative slack.
Vivado happily generates a bitstream for a timing-failed design; it's
the design that won't run reliably at the target clock, not the build
that's broken. Branch on `met_timing` explicitly when timing matters:

```bash
# Snapshot build state, then decide whether to program
echo '{"op":"build.summary","params":{}}' | python vivado_op.py
# Inspect the response: if ready_to_program is true and met_timing is true,
# call hardware.program_device. If met_timing is false, point the user at
# the timing_report_path before programming.
echo '{"op":"hardware.program_device","params":{}}' | python vivado_op.py
```

Three-way truth value, on purpose:
- `true`  -- WNS >= 0 and TNS >= 0
- `false` -- numbers parsed cleanly and WNS or TNS is negative
- `null`  -- the routed timing summary couldn't be located or parsed
            yet (e.g. impl hasn't finished, or Vivado changed the
            report layout). Don't paper over this with "if not met_timing"
            -- "missing" and "failed" mean different things.

### build.get_active_runs

Just the run names. Lighter than `build.summary` -- doesn't query
statuses or scan the filesystem.

**Request:**
```json
{"op": "build.get_active_runs", "params": {}}
```

**Response:**
```json
{"success": true, "synth_run": "synth_1", "impl_run": "impl_1"}
```

### build.get_run_status

Read STATUS / PROGRESS / NEEDS_REFRESH on a specific run. Pass exactly
one of:

- `kind="synthesis"` or `kind="implementation"` -- resolves via
  `current_run -synthesis` / `-implementation` (typically `synth_1` /
  `impl_1`).
- `run="<name>"` -- any run in the project. This is what you use for
  per-IP OOC runs (e.g. `vio_0_synth_1`), which are not exposed by
  `current_run`.

**Request:**
```json
{"op": "build.get_run_status", "params": {"kind": "implementation"}}
```

**Response:**
```json
{
  "success": true, "kind": "implementation", "run": "impl_1",
  "status": "write_bitstream Complete!", "progress": "100%",
  "needs_refresh": "0", "is_complete": true, "is_failed": false
}
```

For a per-IP OOC run, query by name:

**Request:**
```json
{"op": "build.get_run_status", "params": {"run": "vio_0_synth_1"}}
```

**Response:**
```json
{"success": true, "kind": null, "run": "vio_0_synth_1", "status": "synth_design Complete!", "is_complete": true, "is_failed": false}
```

The `kind` field in the response is the label when resolved by `kind=`
and `null` when resolved by `run=` -- the op deliberately doesn't try
to classify arbitrary IP / OOC runs.

Cheap; safe to poll. `is_complete` / `is_failed` are derived booleans
so callers can branch without string matching.

#### Failure modes

| `error_kind` | When |
|---|---|
| `bad_arg` | Both or neither of `kind` / `run` were given, or `kind` was not `"synthesis"` / `"implementation"`. |
| `not_found` | The active run for `kind` is empty (no project open?), or `run=<name>` does not exist in the project. |

### build.wait_for_run

Block (with polling) until a run completes, fails, or times out. Same
`kind` / `run` resolution as `build.get_run_status`.

**Request:**
```json
{"op": "build.wait_for_run", "params": {"kind": "implementation", "timeout": 1800, "poll": 30}}
```

**Response:**
```json
{"success": true, "kind": "implementation", "run": "impl_1", "status": "write_bitstream Complete!", "is_complete": true, "is_failed": false}
```

For an OOC IP run with a faster poll:

**Request:**
```json
{"op": "build.wait_for_run", "params": {"run": "vio_0_synth_1", "poll": 2}}
```

**Long-running**: this can take minutes to hours. The dispatcher will
sit blocked for the full duration of the wait, so for very long runs
prefer launching with `build.synthesize` / `build.implement` using
`wait: false`, then poll `build.get_run_status` from the agent at
e.g. 30s / 60s / 5min intervals.

Why polling, not `wait_on_run`? `wait_on_run` blocks Vivado's main
thread, which would also block the bridge for the entire duration of
the build. Polling keeps the bridge responsive.

Set `log: false` to silence the per-progress-tick prints.

#### Failure modes

| `error_kind` | When |
|---|---|
| `bad_arg` | See `build.get_run_status`. |
| `not_found` | See `build.get_run_status`. |
| `run_failed` | Vivado reported the run errored / failed. |
| `timeout` | The run did not complete within `timeout` seconds. |

### build.synthesize

**Request:**
```json
{"op": "build.synthesize", "params": {"jobs": 8, "reset": true, "wait": true, "auto_synth_ips": true}}
```

**Response:**
```json
{
  "success": true,
  "status": "synth_design Complete!",
  "diagnostics": {"available": true, "error_count": 0, "critical_warning_count": 0, "warning_count": 8, "first_errors": [], "first_critical_warnings": [], "first_warnings": ["WARNING: ..."], "log_path": ".../runme.log"},
  "warnings": []
}
```

Runs `reset_run`, then `launch_runs`, then (if `wait: true`) waits for
completion. On `wait: true` the result also carries a `diagnostics`
block (see below) and lifts critical messages into `warnings`.

**Long-running**: this can take minutes to tens of minutes. Pass
`wait: false` in params to return immediately after launch, then poll
status with `build.get_run_status` until `is_complete: true`. The
agent should schedule periodic checks (e.g. 30s, 60s, 5min intervals)
rather than blocking in a single dispatcher call.

#### Auto OOC synthesis of IPs (GUI parity)

`auto_synth_ips: true` (the default) makes the wrapper match what the
Vivado GUI does on "Run Synthesis". Before the parent `synth_1` is
launched, every IP in the project that:

- has `GENERATE_SYNTH_CHECKPOINT=true` (the create_ip default), and
- has no `Complete` OOC synth run (or no run object at all),

is synthesized first via `build.synthesize_ip` (see below). Each
auto-synth adds a one-line `[bridge]` note to `warnings` so the caller
can see which IPs got built ahead of time and how long each took.

Without this step, an AI driving Vivado from Tcl typically hits one
of two failure modes that the GUI never exposes:

  1. `[Common 17-162] Invalid option value` from
     `launch_runs <ip>_synth_1` -- the run object doesn't exist
     because `create_ip_run` was never called.
  2. The parent `synth_1` runs, the IP is treated as a black box,
     and the `.bit` is generated with the IP optimised away. Most
     visible later as an empty `.ltx` and a missing VIO/ILA core
     at runtime.

`build.synthesize_ip` handles `create_ip_run` and the polling
correctly, so calling `build.synthesize` is sufficient. Set
`auto_synth_ips: false` only if you want to reproduce the bare
`launch_runs synth_1` behaviour (e.g. when debugging the IP synth
flow itself).

#### Top-module change detection

If you rename a module in HDL but forget to update the project's
`top` setting, Vivado will silently pick a different module as the
top and synthesise *that* design successfully. The wrapper compares
the project's `top` property before and after the run; if it changed,
the result includes a `warnings` entry plus `top_before` and
`top_after` fields. The synth itself still reports `success=true`
(Vivado's view), but you'll know the design that was synthesised
isn't the one you thought.

### build.synthesize_ip

**Request:**
```json
{"op": "build.synthesize_ip", "params": {"ip": "vio_0", "jobs": 4, "timeout": 600, "reset": true}}
```

**Response:**
```json
{"success": true, "ip": "vio_0", "run": "vio_0_synth_1", "status": "synth_design Complete!", "elapsed_s": 47.2}
```

Out-of-context synthesize a single IP and wait for completion. Used
internally by `build.synthesize` with `auto_synth_ips: true` and
exposed here for callers that want to drive the OOC flow manually.

**Long-running**: typically tens of seconds to a few minutes per IP.
Same blocking semantics as `build.synthesize`.

Returns the usual ok/fail dict plus:
  - `ip` -- the IP instance name.
  - `run` -- the OOC synth run name (e.g. `vio_0_synth_1`).
  - `status` -- the final run STATUS string from Vivado.
  - `elapsed_s` -- wall-clock seconds.

Failure modes:
  - `tcl_error` -- propagated from `create_ip_run` / `reset_run` /
    `launch_runs` / `get_property STATUS`.
  - `run_failed` -- run finished with `Aborted` or `ERROR` status.
  - `timeout` -- did not finish within `timeout` seconds.

### build.implement

**Request:**
```json
{"op": "build.implement", "params": {"jobs": 8, "timeout": 3600, "reset": true, "wait": true, "generate_bitstream": true}}
```

**Response:**
```json
{
  "success": true,
  "status": "write_bitstream Complete!",
  "diagnostics": {"available": true, "error_count": 0, "critical_warning_count": 0, "warning_count": 3, "first_warnings": ["WARNING: ..."], "log_path": ".../runme.log"},
  "warnings": []
}
```

`generate_bitstream: true` runs the implementation through
`write_bitstream` (default; you almost always want this -- it matches
the GUI's "Generate Bitstream" click). `generate_bitstream: false`
stops at `route_design`, e.g. when you only need timing reports.

Same diagnostic-attaching behaviour as `build.synthesize`.

**Long-running**: implementation typically takes longer than synth
(minutes to an hour on larger designs). Pass `wait: false` and poll
`build.get_run_status` for `kind: "implementation"` at appropriate
intervals (e.g. 60s / 5min) instead of blocking the dispatcher.

#### Pre-flight: upstream synth check

Before launching, `build.implement` checks whether the active
synthesis run is in a failed state. If it is, the call returns
`error_kind="upstream_failed"` (not `tcl_error`) with a message that
points the user at `build.synthesize` with `reset: true`. Vivado's
own error here is a Tcl stack trace mentioning `[Common 17-70]`,
which is hostile to read; the wrapper turns it into a guided message.

#### `diagnostics` block

When `build.synthesize` / `build.implement` finish (with `wait: true`),
the result includes:

```json
"diagnostics": {
    "available": true,
    "error_count": 0,
    "critical_warning_count": 0,
    "warning_count": 8,
    "first_errors": [],
    "first_critical_warnings": [],
    "first_warnings": ["WARNING: ...", "WARNING: ..."],
    "log_path": ".../runme.log"
}
```

`first_errors`, `first_critical_warnings`, and `first_warnings` are
each up to 5 lines, with each line truncated to 250 chars. The preview
is enough to see *what* went wrong; the full list is in `log_path`
(read or grep with your host-side tools).

If any critical issues are present, the first line of each severity is
also surfaced in the top-level `warnings` list, so you don't have to
look inside `diagnostics` to notice.

### build.get_run_log_path

Resolve `runme.log` for the active run.

**Request:**
```json
{"op": "build.get_run_log_path", "params": {"kind": "synthesis"}}
```

**Response:**
```json
{"success": true, "run": "synth_1", "log_path": "D:/.../synth_1/runme.log", "log_exists": true}
```

The log can be megabytes. **This operation does not read it.** Use the
returned `log_path` with your own host-side tools (Read / Grep) when
you need the contents.

### build.get_run_diagnostics

Count and preview ERROR / CRITICAL WARNING / WARNING lines in
`runme.log`.

**Request:**
```json
{"op": "build.get_run_diagnostics", "params": {"kind": "implementation", "sample_size": 5}}
```

**Response:**
```json
{"success": true, "run": "impl_1",
 "log_path": ".../runme.log", "log_exists": true,
 "error_count": 0, "critical_warning_count": 0, "warning_count": 8,
 "first_errors": [], "first_critical_warnings": [],
 "first_warnings": ["WARNING: ...", "WARNING: ..."]}
```

Returns counts plus the first few lines of each severity (default
five). For cascading errors there can be hundreds of entries; the
preview keeps the response tight while still letting you see what
matters. For full detail, read / grep `log_path` host-side.

Severity is detected by line prefix (`ERROR:`, `CRITICAL WARNING:`,
`WARNING:`), so an `INFO:` line that mentions the word "WARNING" in
its message body is *not* miscounted.

### build.find_bitstream

Locate the freshest `.bit` and (if any) `.ltx` for the active impl run.

**Request:**
```json
{"op": "build.find_bitstream", "params": {}}
```

**Response:**
```json
{"success": true, "impl_run": "impl_1",
 "bit_path": ".../blink.bit", "ltx_path": ".../blink.ltx",
 "bit_exists": true, "ltx_exists": true}
```

`hardware.program_device` calls this internally when its `bit_path` /
`ltx_path` arguments are null.

### build.open_synth

Open the active synth run as a netlist design. Required when you want
to call `create_debug_core`, `report_*`, or any other Tcl that
operates on the open netlist. No-op if already open.

**Request:**
```json
{"op": "build.open_synth", "params": {}}
```

**Response:**
```json
{"success": true, "run": "synth_1"}
```

To open a non-active run, pass `run: "<name>"` in params.

### build.close_design

Close the currently open design (`close_design`). Pair with
`build.open_synth` so the next `launch_runs synth_1` does not collide
with an already-open design.

**Request:**
```json
{"op": "build.close_design", "params": {}}
```

**Response:**
```json
{"success": true}
```

## Notes

- The operations do **not** add runs, switch active runs, or modify run
  strategies. They only act on whatever's already current.
- `build.synthesize` and `build.implement` will both call `reset_run`
  by default. Pass `reset: false` if the run hasn't been completed yet
  (e.g. you're resuming after a crash) -- otherwise Vivado will
  refuse with "out-of-date" errors.
- These operations don't pre-flight "is a project open?" -- if it
  isn't, Vivado returns a clear error which the wrapper passes through
  as a `tcl_error`.
