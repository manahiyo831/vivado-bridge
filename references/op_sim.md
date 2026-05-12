# sim operations

All operations are invoked via the `vivado_op.py` JSON dispatcher.
See [SKILL.md](../SKILL.md) for the invocation pattern.

Verbs for driving Vivado's behavioural simulator (xsim) via
the bridge. The matching design / pitfall / testbench-rules guide is
[using_simulation.md](using_simulation.md) -- read both.

## Common shape

All operations return a dict with `success`, `error_kind`, `message`,
`warnings`. On `tcl_error` failures the result also carries
`error_info` and `error_code`. Operation-specific fields are listed
below per operation.

`warnings` always carries the new lines Vivado wrote to its Tcl
Console between calls -- including testbench `$display` /
`RESULT:` / `$finish called at time ...` plus xvlog / xelab
WARNING / ERROR. See SKILL.md §9 for the full filter rules.

## Operations

### sim.get_sim_status

Read whether a simulation is currently open and at what sim time.

Request:

```json
{"op": "sim.get_sim_status", "params": {}}
```

Response:

```json
{
  "success": true,
  "open": true,
  "sim": "simulation_4",
  "current_time": "2696 ns",
  "message": "simulation_4 open at 2696 ns"
}
```

`current_sim` (Vivado Tcl) returns the open sim name or empty when
no sim is open. We pass both back as-is so the caller can decide
what to do.

Failure modes:

  - `tcl_error` -- the underlying `current_sim -quiet` call itself
    failed. This is rare; if it happens the bridge or Vivado is in
    an unusual state (read SKILL.md §"Connect first, then work").

### sim.close_sim

Close the active simulation. `force=true` mirrors `close_sim
-force`, which is the right default when you want to recover after
a stuck `run`.

Request:

```json
{"op": "sim.close_sim", "params": {"force": true}}
```

Response:

```json
{"success": true, "message": "simulation closed"}
```

Op-specific fields: none.

Failure modes:

  - `tcl_error` -- forwarded from xsim (e.g. there was no sim to
    close, depending on Vivado version).

### sim.run (the main entry point)

Single-shot launch + run for at most `sim_time_us` µs. Returns
when xsim either reaches `$finish` (early) or consumes the full
window. There is no internal chunk loop.

Request:

```json
{"op": "sim.run", "params": {
  "sim_time_us": 200,
  "top": "tb_simple_counter",
  "timeout": 60.0,
  "reuse": false,
  "restart": false
}}
```

Parameters:

- `sim_time_us` (required) -- max sim time in µs.
- `top` -- sim_1 top to set before launching. Omit to keep the
  currently-set top.
- `timeout` (default `60.0`) -- per-call exec_tcl deadline in
  wall-clock seconds.
- `reuse` (default `false`) -- continue running against an
  already-open sim. Mutually exclusive with `restart`.
- `restart` (default `false`) -- `close_sim -force` then launch
  fresh. Mutually exclusive with `reuse`.

Response (success):

```json
{
  "success": true,
  "sim": "simulation_1",
  "before_time": "1 us",
  "current_time": "201 us",
  "finished": true,
  "elapsed_s": 3.4
}
```

Field meanings:

  - `success` -- `true` iff xsim ran without erroring (this is NOT
    "the testbench passed"; check `warnings` for `RESULT:` lines).
  - `sim` -- current_sim string (`"simulation_1"` / `"sim_1"`).
  - `before_time` -- sim time just *after* launch_simulation
    (typically not 0; xsim parks at ~1 us after launch). See
    using_simulation.md §"Reading before_time / current_time".
  - `current_time` -- sim time after the run window.
  - `finished` -- `true` iff the testbench `$finish`ed before the
    full `sim_time_us` was consumed.
  - `elapsed_s` -- wall-clock seconds the call took.

Pre-flight check: if a simulation is already open and
`current_time > 0`, `run` fails with `error_kind="sim_already_running"`
unless the caller passed `reuse=true` (continue running against
the existing sim) or `restart=true` (close_sim -force then launch
fresh). This exists because the bridge cannot tell whether the
open sim was started by the user from the Tcl Console, by an
earlier `sim.run` that errored out, or by something else --
silently piling on is exactly how runaway sims happen.

Cap-without-finish hint: when `finished=false`, `warnings[0]` is
prepended with a `[bridge]` line summarising the common causes
(wedged on wait/event, silent-park, sim_time_us too short).

Polling pattern: `sim.run` is blocking and returns only when xsim
either `$finish`es or the cap fires. For very long simulations,
keep `sim_time_us` modest and call `sim.run` repeatedly with
`reuse=true` -- composition lives in the caller, not inside the
helper. Each return gives you a chance to inspect `current_time`
/ `warnings` and decide whether to continue.

Failure modes:

  - `client_error` -- `reuse=true` and `restart=true` were both
    passed, or `sim_time_us <= 0`, or `top` set together with
    `reuse=true` (top can only be set at launch time; the open
    sim's top is fixed).
  - `sim_already_running` -- pre-flight check refused. The
    `sim` and `current_time` fields tell you what is already
    open. Choose `reuse=true` or `restart=true` to override.
  - `tcl_error` -- propagated from `set_property top`,
    `launch_simulation`, or `run <us> us`. The actionable lines
    (e.g. `[VRFC ...]` for a syntax error in the DUT, `[USF-XSim-62]`
    for a failed compile step) appear in `warnings`.

Composing "run for another N µs": call `sim.run` again with
`sim_time_us=N` and `reuse=true` -- no internal loop required, the
caller composes.

### sim.summary

Brief textual summary of the most recent `simulate.log`. Counts
ERROR / Fatal / `$finish` markers and any `*** ALL PASS ***`-style
PASS markers. Useful as a sanity cross-check, but rarely needed
now that `sim.run`'s `warnings` already carries the full Tcl
Console transcript.

Request:

```json
{"op": "sim.summary", "params": {"sample_size": 5}}
```

Response:

```json
{
  "success": true,
  "log_path": ".../simulate.log",
  "errors": [],
  "fatals": [],
  "finishes": ["$finish called at time : 200 us"],
  "pass_markers": 1,
  "message": "errors=0, fatals=0, finishes=1, pass_markers=1"
}
```

Failure modes:

  - `parse_failed` -- could not locate or read `simulate.log`.
    The `log_path` field carries the path it tried (or `null` if
    even that couldn't be resolved).

## Typical flow

```bash
python vivado_op.py '{"op":"sim.run","params":{"top":"tb_simple_counter","sim_time_us":200,"restart":true}}'
```

On success, the response's `finished` field tells you whether the
testbench `$finish`ed before the cap fired. `warnings` carries the
xvlog/xelab/runtime lines -- scan it for `RESULT:` to recover the
testbench's own pass/fail report. On failure, `message` is the
one-line summary and the first few `warnings` lines hold the
actionable Vivado lines (xvlog/xelab errors etc.).

## See also

  - [using_simulation.md](using_simulation.md) -- testbench design
    rules, recovery patterns, what `warnings` carries (compile vs
    elaborate vs runtime), and the `before_time` semantics.
