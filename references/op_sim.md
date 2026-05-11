# sim operations

`from operations import sim`

Python verbs for driving Vivado's behavioural simulator (xsim) via
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

### get_sim_status

Read whether a simulation is currently open and at what sim time.

```python
sim.get_sim_status(client)
# {
#   'success': True, 'open': True,
#   'sim': 'simulation_4', 'current_time': '2696 ns',
#   'message': 'simulation_4 open at 2696 ns',
# }
```

`current_sim` (Vivado Tcl) returns the open sim name or empty when
no sim is open. We pass both back as-is so the caller can decide
what to do.

Failure modes:

  - `tcl_error` -- the underlying `current_sim -quiet` call itself
    failed. This is rare; if it happens the bridge or Vivado is in
    an unusual state (read SKILL.md §"Connect first, then work").

### close_sim

Close the active simulation. `force=True` mirrors `close_sim
-force`, which is the right default when you want to recover after
a stuck `run`.

```python
sim.close_sim(client, force=True)
# {'success': True, 'message': 'simulation closed', ...}
```

Op-specific fields: none.

Failure modes:

  - `tcl_error` -- forwarded from xsim (e.g. there was no sim to
    close, depending on Vivado version).

### run (the main entry point)

Single-shot launch + run for at most `sim_time_us` µs. Returns
when xsim either reaches `$finish` (early) or consumes the full
window. There is no internal chunk loop.

```python
sim.run(
    client,
    *,
    sim_time_us: float,            # required, max sim time in µs
    top: str | None = None,        # sim_1 top to set before launching
    timeout: float = 60.0,         # per-call exec_tcl deadline (wall-clock seconds)
    reuse: bool = False,           # mutually exclusive with restart
    restart: bool = False,         # mutually exclusive with reuse
)
```

Returns:

  - `success` -- True iff xsim ran without erroring (this is NOT
    "the testbench passed"; check `warnings` for `RESULT:` lines).
  - `sim` -- current_sim string ("simulation_1" / "sim_1").
  - `before_time` -- sim time just *after* launch_simulation
    (typically not 0; xsim parks at ~1 us after launch). See
    using_simulation.md §"Reading before_time / current_time".
  - `current_time` -- sim time after the run window.
  - `finished` -- True iff the testbench `$finish`ed before the
    full `sim_time_us` was consumed.
  - `elapsed_s` -- wall-clock seconds the call took.

Pre-flight check: if a simulation is already open and
`current_time > 0`, `run` fails with `error_kind="sim_already_running"`
unless the caller passed `reuse=True` (continue running against
the existing sim) or `restart=True` (close_sim -force then launch
fresh). This exists because the bridge cannot tell whether the
open sim was started by the user from the Tcl Console, by an
earlier `sim.run` that errored out, or by something else --
silently piling on is exactly how runaway sims happen.

Cap-without-finish hint: when `finished=False`, `warnings[0]` is
prepended with a `[bridge]` line summarising the common causes
(wedged on wait/event, silent-park, sim_time_us too short).

Failure modes:

  - `client_error` -- `reuse=True` and `restart=True` were both
    passed, or `sim_time_us <= 0`, or `top=` set together with
    `reuse=True` (top can only be set at launch time; the open
    sim's top is fixed).
  - `sim_already_running` -- pre-flight check refused. The
    `sim` and `current_time` fields tell you what is already
    open. Choose `reuse=True` or `restart=True` to override.
  - `tcl_error` -- propagated from `set_property top`,
    `launch_simulation`, or `run <us> us`. The actionable lines
    (e.g. `[VRFC ...]` for a syntax error in the DUT, `[USF-XSim-62]`
    for a failed compile step) appear in `warnings`.

Composing "run for another N µs": call `sim.run(client,
sim_time_us=N, reuse=True)` -- no internal loop required, the
caller composes.

### summary

Brief textual summary of the most recent `simulate.log`. Counts
ERROR / Fatal / `$finish` markers and any `*** ALL PASS ***`-style
PASS markers. Useful as a sanity cross-check, but rarely needed
now that `sim.run`'s `warnings` already carries the full Tcl
Console transcript.

```python
sim.summary(client, sample_size=5)
# {
#   'success': True,
#   'log_path': '.../simulate.log',
#   'errors': [...up to sample_size lines...],
#   'fatals': [...],
#   'finishes': [...],
#   'pass_markers': 1,
#   'message': 'errors=0, fatals=0, finishes=1, pass_markers=1',
# }
```

Failure modes:

  - `parse_failed` -- could not locate or read `simulate.log`.
    The `log_path` field carries the path it tried (or None if
    even that couldn't be resolved).

## Typical flow

```python
from vivado_bridge_client import Client
from operations import sim

c = Client.connect()

result = sim.run(c, top="tb_simple_counter", sim_time_us=200, restart=True)

if not result["success"]:
    print(result["message"])
    # warnings carries the actionable Vivado lines (xvlog/xelab errors etc.)
    for w in result["warnings"][:5]:
        print(" ", w)
    raise SystemExit(1)

if result["finished"]:
    print(f"$finish at {result['current_time']}")
else:
    print(f"hit cap ({result['current_time']}) without $finish")

# Find the testbench's own RESULT line.
for line in result["warnings"]:
    if "RESULT:" in line:
        print(line)
        break
```

## See also

  - [using_simulation.md](using_simulation.md) -- testbench design
    rules, recovery patterns, what `warnings` carries (compile vs
    elaborate vs runtime), and the `before_time` semantics.
