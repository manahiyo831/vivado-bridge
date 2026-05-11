# build operations

`from operations import build`

Drives synthesis and implementation on the project's **active** runs.
Active = whatever Vivado returns from `current_run -synthesis` /
`current_run -implementation`. For a fresh project that's `synth_1` /
`impl_1`; if you've cloned strategies, it's whichever you marked
active. The operations never invent run names.

If you need to drive a non-active run, use `client.exec_tcl(...)` directly.

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

### summary

One-shot snapshot of build state. Use this as the first call in a
session: it tells you whether there's a project, which runs are
active, what their statuses are, whether a bitstream is on disk, and
whether you can program now.

```python
build.summary(client)
# {
#   'success': True,
#   'synth_run': 'synth_1', 'impl_run': 'impl_1',
#   'synth_status': 'synth_design Complete!', 'synth_complete': True, 'synth_failed': False,
#   'impl_status': 'write_bitstream Complete!', 'impl_complete': True, 'impl_failed': False,
#   'bit_path': '.../blink.bit', 'ltx_path': '.../blink.ltx',
#   'bit_exists': True, 'ltx_exists': True,
#   'wns': 0.231, 'tns': 0.0, 'met_timing': True,
#   'timing_report_path': '.../impl_1/blink_timing_summary_routed.rpt',
#   'ready_to_program': True,
# }
```

`ready_to_program` is `impl_complete and not impl_failed and bit_exists`.

`met_timing` is a separate axis from `ready_to_program` -- a build can
be "ready to program" (bitstream exists) and still have negative slack.
Vivado happily generates a bitstream for a timing-failed design; it's
the design that won't run reliably at the target clock, not the build
that's broken. Branch on `met_timing` explicitly when timing matters:

```python
s = build.summary(c)
if s["ready_to_program"] and s["met_timing"] is True:
    hardware.program_device(c)
elif s["met_timing"] is False:
    print(f"timing failed, WNS={s['wns']:.3f}ns -- inspect {s['timing_report_path']}")
```

Three-way truth value, on purpose:
- `True`  -- WNS ≥ 0 and TNS ≥ 0
- `False` -- numbers parsed cleanly and WNS or TNS is negative
- `None`  -- the routed timing summary couldn't be located or parsed
            yet (e.g. impl hasn't finished, or Vivado changed the
            report layout). Don't paper over this with `if not met_timing`
            -- "missing" and "failed" mean different things.

### get_active_runs

Just the run names. Lighter than `summary()` -- doesn't query
statuses or scan the filesystem.

```python
build.get_active_runs(client)
# {'success': True, 'synth_run': 'synth_1', 'impl_run': 'impl_1', ...}
```

### get_run_status

Read STATUS / PROGRESS / NEEDS_REFRESH on a specific run. Pass exactly
one of:

- `kind="synthesis"` or `kind="implementation"` -- resolves via
  `current_run -synthesis` / `-implementation` (typically `synth_1` /
  `impl_1`).
- `run="<name>"` -- any run in the project. This is what you use for
  per-IP OOC runs (e.g. `vio_0_synth_1`), which are not exposed by
  `current_run`.

```python
build.get_run_status(client, kind="implementation")
# {
#   'success': True, 'kind': 'implementation', 'run': 'impl_1',
#   'status': 'write_bitstream Complete!', 'progress': '100%',
#   'needs_refresh': '0', 'is_complete': True, 'is_failed': False,
# }

build.get_run_status(client, run="vio_0_synth_1")
# {'success': True, 'kind': None, 'run': 'vio_0_synth_1', ...}
```

The `kind` field in the response is the label when resolved by `kind=`
and `None` when resolved by `run=` -- the op deliberately doesn't try
to classify arbitrary IP / OOC runs.

Cheap; safe to poll. `is_complete` / `is_failed` are derived booleans
so callers can branch without string matching.

#### Failure modes

| `error_kind` | When |
|---|---|
| `bad_arg` | Both or neither of `kind=` / `run=` were given, or `kind` was not `"synthesis"` / `"implementation"`. |
| `not_found` | The active run for `kind` is empty (no project open?), or `run=<name>` does not exist in the project. |

### wait_for_run

Block (with polling) until a run completes, fails, or times out. Same
`kind=` / `run=` resolution as `get_run_status`.

```python
build.wait_for_run(client, kind="implementation", timeout=1800, poll=30)
build.wait_for_run(client, run="vio_0_synth_1", poll=2)
```

Why polling, not `wait_on_run`? `wait_on_run` blocks Vivado's main
thread, which would also block the bridge for the entire duration of
the build. Polling keeps the bridge responsive.

Set `log=False` to silence the per-progress-tick prints.

#### Failure modes

| `error_kind` | When |
|---|---|
| `bad_arg` | See `get_run_status`. |
| `not_found` | See `get_run_status`. |
| `run_failed` | Vivado reported the run errored / failed. |
| `timeout` | The run did not complete within `timeout` seconds. |

### synthesize

```python
build.synthesize(
    client,
    jobs=8, timeout=1800,
    reset=True, wait=True,
    auto_synth_ips=True,   # mirrors the GUI; see below
)
```

Runs `reset_run`, then `launch_runs`, then (if `wait=True`) waits for
completion. On `wait=True` the result also carries a `diagnostics`
block (see below) and lifts critical messages into `warnings`.

#### Auto OOC synthesis of IPs (GUI parity)

`auto_synth_ips=True` (the default) makes the wrapper match what the
Vivado GUI does on "Run Synthesis". Before the parent `synth_1` is
launched, every IP in the project that:

- has `GENERATE_SYNTH_CHECKPOINT=true` (the create_ip default), and
- has no `Complete` OOC synth run (or no run object at all),

is synthesized first via `synthesize_ip` (see below). Each auto-synth
adds a one-line `[bridge]` note to `warnings` so the caller can see
which IPs got built ahead of time and how long each took.

Without this step, an AI driving Vivado from Tcl typically hits one
of two failure modes that the GUI never exposes:

  1. `[Common 17-162] Invalid option value` from
     `launch_runs <ip>_synth_1` — the run object doesn't exist
     because `create_ip_run` was never called.
  2. The parent `synth_1` runs, the IP is treated as a black box,
     and the `.bit` is generated with the IP optimised away. Most
     visible later as an empty `.ltx` and a missing VIO/ILA core
     at runtime.

`synthesize_ip` handles `create_ip_run` and the polling correctly,
so calling `synthesize` is sufficient. Set `auto_synth_ips=False`
only if you want to reproduce the bare `launch_runs synth_1`
behaviour (e.g. when debugging the IP synth flow itself).

#### Top-module change detection

If you rename a module in HDL but forget to update the project's
`top` setting, Vivado will silently pick a different module as the
top and synthesise *that* design successfully. The wrapper compares
the project's `top` property before and after the run; if it changed,
the result includes a `warnings` entry plus `top_before` and
`top_after` fields. The synth itself still reports `success=True`
(Vivado's view), but you'll know the design that was synthesised
isn't the one you thought.

### synthesize_ip

```python
build.synthesize_ip(
    client,
    ip="vio_0",            # IP instance name
    jobs=4, timeout=600,
    reset=True,
)
```

Out-of-context synthesize a single IP and wait for completion. Used
internally by `synthesize(auto_synth_ips=True)` and exposed here for
callers that want to drive the OOC flow manually.

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

### implement

```python
build.implement(client, jobs=8, timeout=3600,
                reset=True, wait=True, generate_bitstream=True)
```

`generate_bitstream=True` runs the implementation through
`write_bitstream` (default; you almost always want this -- it matches
the GUI's "Generate Bitstream" click). `generate_bitstream=False`
stops at `route_design`, e.g. when you only need timing reports.

Same diagnostic-attaching behaviour as `synthesize`.

#### Pre-flight: upstream synth check

Before launching, `implement` checks whether the active synthesis run
is in a failed state. If it is, the call returns
`error_kind="upstream_failed"` (not `tcl_error`) with a message that
points the user at `synthesize(reset=True)`. Vivado's own error here
is a Tcl stack trace mentioning `[Common 17-70]`, which is hostile to
read; the wrapper turns it into a guided message.

#### `diagnostics` block

When `synthesize` / `implement` finish (with `wait=True`), the result
includes:

```python
'diagnostics': {
    'available': True,
    'error_count': 0,
    'critical_warning_count': 0,
    'warning_count': 8,
    'first_errors': [],                 # up to 5 lines, max 250 chars each
    'first_critical_warnings': [],
    'first_warnings': ['WARNING: ...', 'WARNING: ...'],
    'log_path': '.../runme.log',
}
```

Counts plus a short preview (default 5 lines per severity, each
truncated to 250 chars). The preview is enough to see *what* went
wrong; the full list is in `log_path` (read or grep with your
host-side tools).

If any critical issues are present, the first line of each severity is
also surfaced in the top-level `warnings` list, so you don't have to
look inside `diagnostics` to notice.

### get_run_log_path

Resolve `runme.log` for the active run.

```python
build.get_run_log_path(client, kind="synthesis")
# {'success': True, 'run': 'synth_1',
#  'log_path': 'D:/.../synth_1/runme.log', 'log_exists': True, ...}
```

The log can be megabytes. **This operation does not read it.** Use the
returned `log_path` with your own host-side tools (Read / Grep) when
you need the contents.

### get_run_diagnostics

Count and preview ERROR / CRITICAL WARNING / WARNING lines in
`runme.log`.

```python
build.get_run_diagnostics(client, kind="implementation", sample_size=5)
# {'success': True, 'run': 'impl_1',
#  'log_path': '.../runme.log', 'log_exists': True,
#  'error_count': 0, 'critical_warning_count': 0, 'warning_count': 8,
#  'first_errors': [], 'first_critical_warnings': [],
#  'first_warnings': ['WARNING: ...', 'WARNING: ...']}
```

Returns counts plus the first few lines of each severity (default
five). For cascading errors there can be hundreds of entries; the
preview keeps the response tight while still letting you see what
matters. For full detail, read / grep `log_path` host-side.

Severity is detected by line prefix (`ERROR:`, `CRITICAL WARNING:`,
`WARNING:`), so an `INFO:` line that mentions the word "WARNING" in
its message body is *not* miscounted.

### find_bitstream

Locate the freshest `.bit` and (if any) `.ltx` for the active impl run.

```python
build.find_bitstream(client)
# {'success': True, 'impl_run': 'impl_1',
#  'bit_path': '.../blink.bit', 'ltx_path': '.../blink.ltx',
#  'bit_exists': True, 'ltx_exists': True, ...}
```

`hardware.program_device` calls this internally when its `bit_path` /
`ltx_path` arguments are None.

### open_synth

Opens the active synth run's netlist via `open_run`, so subsequent Tcl
can operate on the open synthesized design (e.g. `create_debug_core`,
`report_*`, debug-XDC edits). No-op if a design is already open. Pair
with [`close_design`](#close_design) before the next
`launch_runs synth_1` so the in-memory design does not collide with a
fresh synthesis.

```python
build.open_synth(client, run=None)
# Opens the active synth run (e.g. synth_1). Pass `run=` to target a
# specific run name. Returns `{'success': True, 'run': 'synth_1', ...}`.
```

### close_design

Closes whatever design is currently open (`close_design`). Use this
after you are done inspecting / editing the netlist that
[`open_synth`](#open_synth) brought up, and before launching a new
synth run -- otherwise Vivado refuses with "design still open" errors.

```python
build.close_design(client)
# Returns `{'success': True, ...}`. No-op if no design is open.
```

## Notes

- The operations do **not** add runs, switch active runs, or modify run
  strategies. They only act on whatever's already current.
- `synthesize()` and `implement()` will both call `reset_run` by
  default. Pass `reset=False` if the run hasn't been completed yet
  (e.g. you're resuming after a crash) -- otherwise Vivado will
  refuse with "out-of-date" errors.
- These operations don't pre-flight "is a project open?" -- if it
  isn't, Vivado returns a clear error which the wrapper passes through
  as a `tcl_error`.
