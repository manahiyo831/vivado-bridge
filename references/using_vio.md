# Using VIO with vivado-bridge

A practical guide for designing, building, programming, and driving
Xilinx Virtual IO (VIO) cores from this bridge. The API surface lives
in [op_debug.md](op_debug.md); this document is the *design pattern*
companion -- the pieces you can't see by reading function signatures.

> Scope: VIO design + bring-up only. ILA waveform capture has its
> own guide -- see [using_ila.md](using_ila.md) for the design-time
> flow and [op_ila.md](op_ila.md) for the Python API.

## Why a separate document

Most of the friction with VIO is **not in the bridge API** -- it's in
how Vivado synthesizes, names, and exposes VIO probes. An AI assistant
that knows `read_vio_probe` / `write_vio_probe` exists will still trip
on:

- The probe name in the IP customizer (`probe_in0`) is **not** the
  probe name at runtime (Vivado renames it after the connected signal).
- Default probe radix is HEX, not binary, with strict character-count
  rules.
- Adding a VIO IP and immediately running `synth_1` produces a
  bitstream where the VIO is **silently optimized away** (no `.ltx`).
- `ACTIVITY_VALUE` for activity detection has its own semantics
  (persistence, refresh-clear) that aren't obvious from the API.
- The VIO Dashboard in the Vivado GUI cannot be opened from Tcl.

This document collects those pitfalls and gives a tested recipe.

## 1. Adding a VIO core to a project

### IP creation

```tcl
create_ip -name vio -vendor xilinx.com -library ip -module_name vio_0
set_property -dict [list \
    CONFIG.C_NUM_PROBE_IN  {N_in} \
    CONFIG.C_PROBE_IN0_WIDTH {w0} \
    CONFIG.C_PROBE_IN1_WIDTH {w1} \
    ... \
    CONFIG.C_NUM_PROBE_OUT {N_out} \
    CONFIG.C_PROBE_OUT0_WIDTH {wo0} \
    CONFIG.C_PROBE_OUT0_INIT_VAL {0x0} \
    ... \
    CONFIG.C_EN_PROBE_IN_ACTIVITY {1} \
] [get_ips vio_0]
```

`C_EN_PROBE_IN_ACTIVITY=1` enables the per-bit activity detectors
(see "Activity detection" below). The IP's GUI label is "Enable Input
Probe Activity Detectors".

### Critical: synthesize the IP OOC *before* main synthesis

When you add the VIO IP to a fresh project and run `synth_1`, Vivado
treats `vio_0` as a black box and silently optimizes it out. The
implementation log shows:

```
CRITICAL WARNING: [Designutils 20-1281] Could not find module 'vio_0'.
The XDC file ... will not be read for this module.
```

The `.bit` is still produced, `success=True` is returned, but the
generated `.ltx` does **not** contain the VIO -- so the dashboard is
empty when you program. This footgun is easy to miss because
`build.implement` reports overall success.

Fix: explicitly synthesize the IP out-of-context before the parent
build, so its `.dcp` is on disk when `synth_design` references it.
There are two practical options:

### Option A (recommended): create the IP run and launch it

```tcl
create_ip_run [get_ips vio_0]
launch_runs vio_0_synth_1 -jobs 4
# poll get_property STATUS [get_runs vio_0_synth_1] until "Complete"
```

`create_ip_run` is the part that's easy to miss. Vivado's
**GUI** does this automatically when you add the IP, so users
who learnt the flow in the GUI often jump straight to
`launch_runs vio_0_synth_1` and hit
`[Common 17-162] Invalid option value` -- there is no
`vio_0_synth_1` run object yet.

### Option B: let parent synth do it implicitly

If the IP has `GENERATE_SYNTH_CHECKPOINT=1` (the default
when `create_ip` was used), running the parent `synth_design`
or `launch_runs synth_1` triggers an implicit OOC synth for
any IP whose dcp is missing. This works in both GUI and
batch, and is the path the GUI defaults to.

The downside is that "the IP's OOC synth ran" is not separately
visible -- it folds into the parent run's log, so a failure
(e.g. probe count too high for the device) is harder to
isolate. Option A keeps the IP run as its own first-class
artifact, which is why this guide recommends it for
non-trivial IPs.

Then run `build.implement(c)` as usual. After it finishes:

- `build.summary(c)` should report `ltx_exists=True`.
- `warnings` should **not** include `Designutils 20-1281`.

If either of those is wrong, do not proceed to programming -- the
VIO is missing from the bitstream.

## 2. Probe naming at runtime

This is the single biggest surprise for AI assistants. The VIO IP
declares ports as `probe_in0`, `probe_in1`, `probe_out0`, ... in
Verilog, but at runtime the probes are renamed after the **signal
that was connected to them at synthesis time.**

Example -- this Verilog:

```verilog
vio_0 u_vio (
    .clk        (clk),
    .probe_in0  (toggle),       // 1 bit input
    .probe_out0 (mode_select),  // 1 bit output
    .probe_out1 (led_force)     // 4 bit output
);
```

produces these runtime probes (observed on Vivado 2024.1):

| Verilog port | Runtime name |
|---|---|
| `probe_in0`  | `toggle` |
| `probe_out0` | `mode_select` |
| `probe_out1` | `led_force` |

**Always call `debug.list_vio_probes(client)` first** to discover the
actual names before reading or writing. Hard-coding `probe_in0` will
fail with `error_kind: not_found`.

### Loopback probes get `_1` suffix

If the same wire is both a VIO output and a VIO input (e.g. for
write-readback verification), Vivado disambiguates by appending `_1`
to the input side:

```verilog
wire [3:0] led_force;        // driven by probe_out1, sensed by probe_in2
vio_0 u_vio (
    ...
    .probe_in2  (led_force),   // -> probe name: led_force_1
    .probe_out1 (led_force)    // -> probe name: led_force
);
```

The output probe keeps the bare signal name; the input gets `_1`.
This is deterministic but easy to forget.

### Want stable probe names?

If you really need to control naming, attach `(* keep = "true" *)`
or `(* mark_debug = "true" *)` to the wire and use a name that won't
collide. But the simpler answer is: just call `list_vio_probes` once
and use whatever Vivado gave you.

### VIO inputs and outputs both come pre-marked for debug

Every net connected to a VIO `probe_in*` or `probe_out*` port has
`MARK_DEBUG=1` set as a side-effect of customizing the IP -- you did
not write the attribute in HDL, but it is on the netlist. This bites
in two ways when you also add an ILA: automatic `MARK_DEBUG` pickup
captures the VIO nets too, and explicit ILA attachment to a VIO net
silently binds 0 wires (`debug.create_ila_core` reports this as
`mark_debug_missing=[...]`).

See [using_ila.md §8](using_ila.md) for the full picture and the
recommended workaround (explicit `connect_debug_port` to a named net
list, preferring inside-instance nets).

## 3. Reading and writing probe values

### Default radix is HEX

A 4-bit probe needs **one** hex character, not four binary digits:

```python
debug.write_vio_probe(c, probe='led_force', value='A')   # OK -> 0xA = 1010
debug.write_vio_probe(c, probe='led_force', value='1010')
# FAIL: tcl_error
# "The hw_probe VIO value [1010] has [4] value characters.
#  The required number of value characters for radix [HEX], is [1]."
```

Read values come back in the same radix:

```python
r = debug.read_vio_probe(c, probe='led_force_1')
# r['value'] == 'a'
n = int(r['value'], 16)   # 10
```

### Switching to binary radix

If you'd rather work in binary strings, set the probe's radix once:

```python
c.exec_tcl(
  'set_property INPUT_VALUE_RADIX BINARY '
  '[get_hw_probes <name> -of [get_hw_vios hw_vio_1]]')
```

For output probes the analogous property is `OUTPUT_VALUE_RADIX`.
After this, write/read use binary strings (`'1010'` for 4 bit).

### Failed writes can leave the dashboard in an intermediate state

If `write_vio_probe` returns `success=False` (typically a radix /
width mismatch), the underlying VIO commit may have partially gone
through. Always re-issue a known-good write or check the result with
a read before continuing.

## 4. Verification by loopback

A reliable pattern for "did my write actually land?": connect each
VIO output internally to a VIO input as well, then read it back.

```verilog
wire       mode_select;    // driven by probe_out0
wire [3:0] led_force;      // driven by probe_out1

assign led = mode_select ? led_force : {4{toggle}};

vio_0 u_vio (
    .clk        (clk),
    .probe_in0  (toggle),       // observe the design's own logic
    .probe_in1  (mode_select),  // -> mode_select_1: loopback
    .probe_in2  (led_force),    // -> led_force_1:   loopback
    .probe_in3  (led),          // -> led_OBUF:      observe the actual output
    .probe_out0 (mode_select),
    .probe_out1 (led_force)
);
```

After a write, you can verify three things in one pass:

1. **Loopback** (`mode_select_1`, `led_force_1`) -- did the value reach
   the FPGA fabric? If the loopback differs from what you wrote, the
   write didn't take.
2. **Effect on logic** (`led_OBUF`) -- did `mode_select=1` actually
   switch the MUX? Comparing against `led_force_1` confirms the design
   responded as expected.
3. **Free-running design signals** (`toggle`) -- still alive? Useful
   for catching "VIO works but the rest of the design hung" cases.

This is more robust than reading just the output probe back, because
Vivado's dashboard caches the last commanded `OUTPUT_VALUE` -- if you
read only the output, you see what you tried to write, not what the
fabric received.

`debug.read_vio_probes_all(client)` reads every input probe in one
call and returns a `{name: value}` dict, which is convenient for
this pattern.

## 5. Activity detection

When `C_EN_PROBE_IN_ACTIVITY=1` is set on the IP, each input bit gets
a small edge-detector. The result shows up in the probe's
`ACTIVITY_VALUE` property -- **one character per bit**:

| Char | Meaning |
|---|---|
| `R` | Rising edge seen this window |
| `F` | Falling edge seen this window |
| `B` | Both edges seen this window |
| `N` | No activity this window |
| `X` | Unknown / activity not enabled (observed; not in official docs) |

So a 4-bit probe might read `BBBB` (all bits toggling), `NNNN` (idle),
or `BNNB` (only the outer bits moved).

### How the window works

Vivado documents the model as: activity accumulates between successive
software reads, and reading **clears** the activity registers in the
VIO hardware (UG908). The size of the accumulation window is set by
`ACTIVITY_PERSISTENCE`:

| Value | Window | Behaviour |
|---|---|---|
| `SHORT` | 8 samples | Default. Edges decay quickly. |
| `LONG` | 80 samples | Decay over a longer window. |
| `INFINITE` | until manual reset | Sticky. Use `reset_hw_vio_activity` to clear. |

`INFINITE` is great for "did this signal *ever* toggle?" hang detection
-- arm it once, run for a while, read once. Don't forget the reset.

### Reading activity reliably

`debug.read_vio_probe(...)` only returns `INPUT_VALUE`, not activity.
Until the bridge gets a dedicated helper, use raw Tcl:

```python
c.exec_tcl(
  'refresh_hw_vio [get_hw_vios hw_vio_1]')        # baseline + clear
import time; time.sleep(observe_seconds)          # let signal run
r = c.exec_tcl(
  'concat '
  'V=[get_property INPUT_VALUE    [get_hw_probes <name> -of [get_hw_vios hw_vio_1]]] '
  'A=[get_property ACTIVITY_VALUE [get_hw_probes <name> -of [get_hw_vios hw_vio_1]]]')
```

Two observations from real bring-up:

- **Skipping the baseline refresh** is the #1 source of "weird"
  results: you'll read activity from the previous test phase. Always
  refresh once after changing state, then wait, then refresh + read.
- **One sample window may not be enough** for slow signals. A 1 Hz
  toggle observed for 0.1 s probably looks like `N`; observe for at
  least a couple of cycles (or use `LONG` persistence).

## 6. The VIO Dashboard (Vivado GUI)

The dashboard is the live-interaction window in the Vivado IDE that
lets a human see and write probe values graphically. **Both opening
the dashboard and adding probes to it are GUI-only operations** --
neither has a Tcl API in Vivado 2024.x.

What is and isn't programmatic:

| Action | Tcl? | How it actually happens |
|---|---|---|
| Open VIO Dashboard window | No | Auto-opens on first VIO detection after `program_hw_devices` + `refresh_hw_device`. If closed: right-click VIO -> *Open Dashboard*. |
| Add probes to the dashboard | No | Right-click in the dashboard -> *Add Probes...*. UG835 has no `add_hw_probe_to_dashboard` and `hw_probe` has no documented `DASHBOARD_VISIBLE` property. |
| Set probe radix | Yes | `set_property INPUT_VALUE_RADIX BINARY [get_hw_probes ...]` (also `OUTPUT_VALUE_RADIX`). Reflected in the dashboard. |
| Commit output values | Yes | `commit_hw_vio` updates the visible widgets in real time. |

Practical pattern for shared (human + AI) workflows:

1. **One-time per project, ask the user to**:
   - Open the dashboard if it isn't already open (right-click VIO in
     *Hardware* or *Debug Probes*).
   - Right-click in the dashboard, *Add Probes*, select the probes the
     user wants visible.
   - Save the project (Ctrl+S).
2. **Subsequent sessions**: re-open the project, program the device.
   Vivado restores the saved dashboard automatically (UG908 *"Saving
   User Dashboard Preferences and Settings"*). Tcl-driven writes from
   the bridge update the visible widgets.

If you (the AI) are starting a session and don't know whether the
user has set this up: just tell them once, then proceed. Driving the
probes from Tcl works regardless of whether the dashboard shows
anything -- the dashboard is purely for the human.

### Automating "Add Probes" via hw.xml editing (best-effort)

If you would rather install a dashboard layout without asking the
user to click around, see [using_dashboard_hack.md](using_dashboard_hack.md).
That guide covers `scripts/setup_dashboard.py`, which edits the
project's `hw.xml` (and the ILA `.wcfg`) between a `close_hw_manager`
and a re-open. It is undocumented Vivado territory but works on
Vivado 2024.1 and saves the user a multi-click ritual on every
fresh project. Use it when the AI is also choosing the layout
(probe set, order, radix, Digital/Analog) -- not when the user has
an existing custom layout you'd prefer to keep.

## 7. Cheat sheet

| Task | Call |
|---|---|
| List all probes (and find runtime names) | `debug.list_vio_probes(c)` |
| Read one probe | `debug.read_vio_probe(c, probe="...")` |
| Read all input probes at once | `debug.read_vio_probes_all(c)` |
| Drive an output probe (HEX) | `debug.write_vio_probe(c, probe="...", value="A")` |
| Read activity | raw Tcl, see section 5 |
| Reset activity (INFINITE persistence) | `c.exec_tcl("reset_hw_vio_activity [get_hw_vios hw_vio_1]")` |
| Reset all output probes to init values | `c.exec_tcl("reset_hw_vio_outputs [get_hw_vios hw_vio_1]")` |

## 8. References

- [UG908 -- Programming and Debugging](https://docs.amd.com/r/en-US/ug908-vivado-programming-debugging/) -- VIO dashboard, activity, persistence
- [PG159 -- VIO IP Product Guide](https://docs.amd.com/v/u/en-US/pg159-vio) -- IP customization options
- [UG912 -- Vivado Properties](https://docs.amd.com/r/en-US/ug912-vivado-properties/HW_VIO) -- `hw_vio` / `hw_probe` property reference
- [ChipScoPy VIO docs](https://xilinx.github.io/chipscopy/2024.1/vio.html) -- the only place R/F/B/N is enumerated officially
