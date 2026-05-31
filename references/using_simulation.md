# Using xsim (Vivado simulation) with vivado-bridge

How to drive Vivado's bundled simulator (xsim) through the bridge
without wedging the bridge for half an hour. This is the **design
companion** to the API reference in [op_sim.md](op_sim.md) -- read
both. op_sim.md tells you what the calls do; this document tells
you how to write a testbench that won't trip them up.

## Why a separate document

xsim is straightforward in interactive use, but driving it from a
single-threaded Tcl bridge has subtle pitfalls:

- `run all` blocks the Tcl interpreter until the testbench reaches
  `$finish`. A buggy testbench that never finishes will hang the
  bridge, not just xsim.
- `wait()` and `wait (...)` constructs in the testbench can cause
  xsim to enter a state where it neither exits nor responds to time
  advancement. The Tcl side sees a `run` that never returns.
- xsim's Verilog parser is stricter than third-party simulators on
  Verilog-2001 corner cases (declarations inside unnamed blocks, etc.)
  and the error messages aren't always obvious.

A common failure mode in early bring-up is a *30 minute* `run all`
hang on a buggy testbench, which can require asking the user to
Ctrl+C the Tcl Console manually. To avoid that, this skill's
simulation flow is built around bounded `run` chunks and an explicit
wall-clock deadline.

## The testbench rules

Keep these in mind when writing or generating a Verilog testbench
that will be driven through the bridge:

### 1. Use `#TIME` literals, not `wait()`

Time-based testbenches stay predictable:

```verilog
initial begin
    rst = 1;
    #100 rst = 0;
    #50  start = 1;
    #10  start = 0;
    #4000;          // observe for 4 us
    $finish;
end
```

`wait (running)` / `@(posedge done)` are fine when you're sure the
event happens. They are dangerous when you're not -- the simulator
silently sits forever waiting, and via the bridge that means a stuck
`run` call.

### 2. Always provide a `$finish` path AND a hard timeout

Every testbench must exit. Leaving exit purely to "the test conditions
will eventually be satisfied" is how 30-minute hangs happen. Add a
parallel `initial` whose only job is to bail out:

```verilog
initial begin
    #50_000;                // safety: 50 us hard cap
    $display("*** TIMEOUT ***");
    $finish;
end
```

This costs you nothing when the test passes (the main `initial`
finishes first) and saves the bridge when it doesn't.

### 3. Declare every variable at module scope (xsim quirk)

xsim rejects declarations inside unnamed `begin ... end` blocks:

```verilog
// ERROR: declarations are not allowed in an unnamed block
if (some_cond) begin
    integer total;          // <-- xsim VRFC 10-8885
    total = 0;
    ...
end
```

Move all `integer` / `reg` / `wire` declarations to the top of the
module. Other simulators are looser about this; xsim is not.

### 4. RAMs that you want to dump should be initialised

If your testbench reads a memory in full (e.g. dumping an output
RAM to CSV) and any address is *never written* by the design, xsim
returns `x` for those reads. The CSV will have `x,x,x,x,x` rows that
break Python's `int(...)` parser. Initialise the RAM in the design:

```verilog
integer i_init;
initial begin
    for (i_init = 0; i_init < DEPTH; i_init = i_init + 1) mem[i_init] = 0;
end
```

7-series block RAM picks this up via the INIT mechanism, so it costs
nothing in synthesis.

## Driving sim.run

The full call signature, return fields, and failure-mode list live
in [op_sim.md](op_sim.md). This guide focuses on the parts that
trip up testbench authors and AI callers in practice.

### Why the single-shot model

`sim.run` issues exactly one `run <sim_time_us> us` against xsim
and stops at whichever happens first: the testbench `$finish`es
(`finished=True`), or the cap fires (`finished=False`). There is
no internal chunk loop and no wall-clock retry. This is deliberate.

A previous version drove xsim with a chunked loop that kept
calling `run <chunk_ns>` until `current_time` stopped advancing.
That looked clever but produced 46 ms of runaway simulation
against a `wait (done)`-wedged testbench before anyone noticed,
because the loop's own "stop when no progress" condition was
fooled by Vivado returning success even when nothing real was
happening. Single-shot + explicit cap removes the class of
mistake entirely: the caller always tells `sim.run` the
maximum sim time, and `sim.run` always tells the caller whether
that cap fired.

To "run for another 100 µs", call `sim.run` again with
`sim_time_us=100` and `reuse=true`. Composition lives in the caller,
not inside the helper.

### Pre-flight check: don't pile sims on top of each other

If a simulation is already open at `current_time > 0`, `sim.run`
refuses with `error_kind="sim_already_running"` unless the caller
opts in via `reuse=True` (continue) or `restart=True` (close_sim
-force, then launch fresh). The bridge cannot tell whether the
open sim was started by the user from the Tcl Console, by an
earlier failing call, or by something else -- making the caller
state intent explicitly is what stops the runaway-46ms class of
bug.

### Reading `before_time` / `current_time`

`before_time` reflects xsim's clock immediately *after*
`launch_simulation` returned, not "time 0". xsim runs every
testbench's `initial` blocks during launch, which typically advances
the clock to the first explicit event in the stimulus and then
parks; in practice `before_time` lands at something like `1 us`
on a freshly-launched sim. So:

  - `current_time - before_time` is the amount the **`run` call**
    just advanced the simulator (this is what `finished` is
    derived from).
  - `current_time` alone is xsim's view of "now" and is **not** the
    same as "the testbench has been running this long". The
    testbench may have observed only a fraction of that time
    inside its `initial` blocks before parking on a `@(posedge
    clk)` etc.

When triaging, prefer to compare `current_time - before_time`
against `sim_time_us` rather than reading `current_time` as
elapsed-from-zero.

### What ends up in `warnings` (compile vs elaborate vs runtime)

`result["warnings"]` carries Vivado Tcl Console output across the
whole `sim.run` window — that includes the **xvlog (compile)** and
**xelab (elaboration)** phases, not just runtime `$display`. So:

  - A missing semicolon in the DUT shows up as
    `ERROR: [VRFC 10-4982] syntax error near 'else' ...` even
    though `success=False, error_kind="tcl_error"` and there is no
    runtime output.
  - A typo'd module name in the testbench shows up as
    `ERROR: [VRFC 10-2063] Module <foo_xx> not found ...`.
  - These appear in `result["warnings"]` directly, so you don't
    need to read `xvlog.log` / `elaborate.log` separately for the
    common cases.

If `sim.run` returns `success=False` and `result["warnings"]` is
empty, that is a sign you're looking at a stale state (e.g.
nothing actually launched because of a pre-flight refusal) — read
`result["message"]` first, and only then fall back to
`bridge.get_vivado_logs(c)` for the raw log path.

## Recovering from a hang

If `sim.run` reports an error and you want a clean slate:

```bash
python vivado_op.py '{"op":"sim.close_sim","params":{"force":true}}'
```

Then fix the testbench (see "The testbench rules" above) and re-run.
If `close_sim` itself times out, switch to the Tcl Console manually
and Ctrl+C there -- that path bypasses the bridge entirely.

## What `sim.run` does NOT do

- It does not write the testbench for you.
- It does not interpret your testbench's PASS/FAIL.
  `success=True` from `sim.run` only means xsim ran without erroring.
  Whether the design under test was correct is up to whatever your
  testbench prints (look in `result["warnings"]` for `RESULT:` /
  `PASS` / `FAIL` lines, or have the testbench dump a file and parse
  it host-side).
- It does not call `wait_on_run` or `wait_on_runs`. Those are blocking
  Tcl calls and would re-introduce the very hang this module avoids.

## Stimulus design patterns

When you verify against a Python gold model — the dominant use case
for this skill — you often need **the same input data** in three
places: the Python model (to compute expected outputs), the
SystemVerilog TB (to drive the DUT in simulation), and an HDL ROM
(to drive the DUT on real hardware in a VIO-triggered sweep, see
[using_ila.md "Sweeping a known input pattern"](using_ila.md#sweeping-a-known-input-pattern-via-vio-triggered-hdl-sequencer)).

Keeping three independently-written copies in sync is a classic
drift source: change the sine amplitude in Python, forget to update
the ROM literals, and the symptom is "sim and HW disagree" with no
obvious cause.

### Pattern: Python emits the HDL `.mem` file

One robust way to eliminate drift is to have Python emit a
`$readmemh`-format file and have both HDL sides `$readmemh` it. Then
the same 256 bytes are physically the source of truth for Python,
TB, and RTL ROM — changing one place changes all three.

```python
# python/foo_ref.py — one place, three uses
inputs = [...]                                # define once
with open("hdl/inputs.mem", "w") as f:
    for v in inputs:
        f.write(f"{v & 0xFFFF:04x}\n")        # 4-hex-digit lines

expected = [compute_gold(inputs, i) for i in range(len(inputs))]
# expected goes to JSON for the host-side comparator
```

```verilog
// RTL ROM (synthesised, also used on real HW)
reg [15:0] rom [0:255];
initial $readmemh("inputs.mem", rom);

// TB sees the same data via the same file
reg [15:0] tb_rom [0:255];
initial $readmemh("inputs.mem", tb_rom);
```

The file format is `$readmemh`'s standard: one hex value per line,
no `0x` prefix, optional `@` address directives. Vivado's `add_files`
treats `.mem` as a sim/synth source — it gets bundled into the
project and finds its way to both xsim and the placed bitstream.

This is a recommended pattern, not a requirement. Designs whose
input is a free-running counter or LFSR don't need a `.mem` file at
all; designs whose input is a small literal list may be fine with
hand-copied values. The pattern earns its keep when the input set
is large enough that hand-coding it is error-prone, **and** you
intend to compare HDL against a Python gold model at the bit-exact
level.

## xsim quirk: last `$display` before `$finish` can be truncated

Vivado 2024.1's xsim does not reliably flush stdout before
`$finish`: the **last `$display` (or `$write`) line right before
`$finish`** sometimes lands in `simulate.log` truncated mid-line, or
never appears at all. `$fflush` does not fix it. Inserting padding
clocks before `$finish` does not fix it either. The exact cut point
varies per run.

This breaks the natural pattern of "print a final `RESULT_TAIL`
summary line right before `$finish`": readers and grep-based parsers
miss the tail.

Two workarounds, both reliable:

1. **Dump verification data via `$writememh` / `$fwrite + $fclose`,
   not `$display`.** File output is properly flushed at end-of-sim.
   Read the file from the host comparator alongside the captured
   `result["warnings"]` lines.
2. **Print the critical summary line earlier**, with at least one
   benign `$display` (e.g. `"sim ending"`) BETWEEN the summary and
   `$finish`. Vivado is happy to drop the benign tail; the summary
   you actually need has already landed.

If you only need a single PASS/FAIL boolean at end-of-sim, option 2
is the simplest. If you need a large dump (per-cycle vectors), use
option 1 — `$writememh "results/sim_out.hex", mem;` is the standard
recipe.
