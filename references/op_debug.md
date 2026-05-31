# debug operations

All operations are invoked via the `vivado_op.py` JSON dispatcher.
See [SKILL.md](../SKILL.md) for the invocation pattern.

Read and write VIO probes, customise the VIO IP at build time, and
insert / delete ILA debug cores into a synthesized design. The
runtime ILA capture flow (configure -> set_triggers -> arm ->
wait_for_capture -> export_csv -> parse_csv) lives in a separate
module -- see [op_ila.md](op_ila.md).

> This document is the **API reference** for the `debug.*` operations.
> For the *design-time* picture (how to add a VIO/ILA core,
> probe-naming behaviour, value radixes, trigger syntax, the GUI
> dashboard limits, the VIO+ILA naming collision), read the
> companion topic guides:
>
> - [using_vio.md](using_vio.md) -- VIO design patterns and pitfalls
> - [using_ila.md](using_ila.md) -- ILA capture flow, headless trigger,
>   CSV analysis

## Common shape

All operations return a dict with `success`, `error_kind`, `message`,
`warnings`. On `tcl_error` failures the result also carries
`error_info` (Vivado's Tcl stack trace) and `error_code`.
Operation-specific fields are listed below.

## VIO operations

### Probe names: list before you read

Probe names on a hardware VIO are not the same as the IP-port names
in the VIO customization GUI (`probe_in0`, `probe_in1`, ...).
After synthesis Vivado renames each probe after the *signal* you
hooked it up to in HDL -- so `vio_inst.probe_in0(toggle)` shows up
as a probe named `toggle`. This is the same auto-naming Vivado uses
for `mark_debug` signals.

Trying to call `debug.read_vio_probe` with `probe: "probe_in0"` on a
design where the probe was renamed to `toggle` returns
`error_kind="not_found"`. The fix is mechanical: call
`debug.list_vio_probes` once at the start of the session, see what
names Vivado actually exposes, and use those.

```bash
# Discover probe names, then read by the runtime name
echo '{"op":"debug.list_vio_probes","params":{}}' | python vivado_op.py
# -> {"success": true, "probes": [{"name": "toggle", "direction": "in", "width": 1, ...}]}

echo '{"op":"debug.read_vio_probe","params":{"probe":"toggle"}}' | python vivado_op.py
```

### debug.list_vios

**Request:**
```json
{"op": "debug.list_vios", "params": {}}
```

**Response:**
```json
{"success": true, "vios": ["hw_vio_1"]}
```

Empty when nothing is on the device (programmed yet?) or when the
design has no VIO cores.

### debug.list_vio_probes

Enumerate the probes on a VIO, with direction, width and value-encoding
radix.

**Request:**
```json
{"op": "debug.list_vio_probes", "params": {"vio": "hw_vio_1"}}
```

**Response:**
```json
{
  "success": true, "vio": "hw_vio_1",
  "probes": [
    {"name": "probe_led", "direction": "in",  "width": 1,
     "type_raw": "vio_input",  "radix": "BINARY"},
    {"name": "setpoint",  "direction": "out", "width": 32,
     "type_raw": "vio_output", "radix": "HEX"}
  ]
}
```

`width` and `radix` are read directly off the runtime hw_probe object
(via `get_property WIDTH` / `OUTPUT_VALUE_RADIX` / `INPUT_VALUE_RADIX`),
which is what `set_property OUTPUT_VALUE` and `get_property INPUT_VALUE`
will validate against. We deliberately do not try to recover them from
the IP's `CONFIG.C_PROBE_INn_WIDTH`: when an ILA shares wires with a VIO
the runtime probes get renamed (pitfall #6) and the IP-port-to-runtime
mapping becomes fragile.

If the result has zero probes but `debug.list_vios` shows the VIO, the
`.ltx` (probes) file probably wasn't attached during programming. Use
`hardware.program_device` with `ltx_path` set to fix that.

`vio` defaults to null and auto-resolves when there's exactly one VIO.

### debug.read_vio_probe

Read a single input probe.

**Request:**
```json
{"op": "debug.read_vio_probe", "params": {"probe": "probe_led"}}
```

**Response:**
```json
{"success": true, "vio": "hw_vio_1",
 "probe": "probe_led", "value": "1"}
```

`value` is the raw string Vivado gave us. The format depends on the
probe's `INPUT_VALUE_RADIX`: typically hex characters for HEX /
UNSIGNED / SIGNED probes (e.g. `"0001fffe"`) and a 0/1 string for
BINARY probes.

Pass `as_int: true` to also get a decoded `int_value` field, which is
the most ergonomic path for multi-bit numeric probes:

**Request:**
```json
{"op": "debug.read_vio_probe", "params": {"probe": "adc_sample", "as_int": true}}
```

**Response:**
```json
{"success": true, "vio": "hw_vio_1",
 "probe": "adc_sample", "value": "ffffe000",
 "int_value": -8192, "radix": "SIGNED", "width": 16}
```

SIGNED probes are sign-extended within their width, so `-1` on a
16-bit signed probe round-trips as `int_value=-1` (raw `"ffff"`).
If decoding fails (unexpected radix, malformed Vivado response) the
raw `value` is still returned and a `warnings` entry explains why
`int_value` was not populated -- we deliberately do not guess.

`refresh: true` (default) calls `refresh_hw_vio` first so the value is
fresh. For polling loops you can leave it on; refresh is cheap.

#### Failure modes

| `error_kind` | When |
|---|---|
| `not_found` | Probe doesn't exist on the chosen VIO. |
| `wrong_direction` | Probe is an output -- use `debug.write_vio_probe` instead. |
| `ambiguous` | Multiple VIOs are present and `vio` was null. |
| `tcl_error` | The probe exists but the underlying `get_property INPUT_VALUE` failed (e.g. target dropped during refresh). |

#### Note on the `value` field

`value` is only present on `success=true` results -- pitfall #5
(op-specific fields appear only on success) applies. On failure use
the `message` and `error_kind` fields from the response; echoing the
failure message back to the user is almost always more useful than
trying to extract a missing `value`.

### debug.read_vio_probes_all

Read every input probe on a VIO in one call.

**Request:**
```json
{"op": "debug.read_vio_probes_all", "params": {}}
```

**Response:**
```json
{"success": true, "vio": "hw_vio_1",
 "values": {"probe_led": "1", "probe_cntmsb": "1"}}
```

Output probes are skipped (they're driven *by* you, not read from the
device).

If any single probe fails to read, the whole call fails with
`error_kind="tcl_error"` and `failed_probes` listing the offenders.
We deliberately do not return partial values with empty strings for
the failed ones -- that would silently hide read errors. Use
`debug.read_vio_probe` per-probe to isolate which one is broken.

### debug.write_vio_probe

Drive a VIO output probe. The recommended path is to pass an integer
and let the bridge encode it.

For a 32-bit HEX probe (Vivado sees `"00010000"`, 8 hex digits):

**Request:**
```json
{"op": "debug.write_vio_probe", "params": {"probe": "setpoint", "value": 65536}}
```

For a 1-bit BINARY probe (Vivado sees `"1"`):

**Request:**
```json
{"op": "debug.write_vio_probe", "params": {"probe": "enable", "value": 1}}
```

For a 16-bit SIGNED probe (Vivado sees the two's-complement form):

**Request:**
```json
{"op": "debug.write_vio_probe", "params": {"probe": "offset", "value": -12345}}
```

The bridge looks up the probe's `WIDTH` and `OUTPUT_VALUE_RADIX` and
formats accordingly, so you don't have to count hex digits or remember
that Vivado checks digit *count* (a 32-bit HEX probe rejects `"1"`
with `[Designutils 20-1474] has [1] value characters; required [8]`).

You can still pass a pre-formatted string if you have one:

```json
{"op": "debug.write_vio_probe", "params": {"probe": "enable", "value": "1"}}
{"op": "debug.write_vio_probe", "params": {"probe": "setpoint", "value": "0001fffe"}}
```

Width and digit count must match exactly; Vivado will reject otherwise.

`commit: false` stages the value but doesn't push it to the device.
For driving multiple probes atomically, prefer `debug.write_vio_probes`
below -- it commits exactly once and aborts cleanly on partial
failure.

#### Failure modes

| `error_kind` | When |
|---|---|
| `not_found` | Probe doesn't exist on the chosen VIO. |
| `wrong_direction` | Probe is an input -- use `debug.read_vio_probe` instead. |
| `ambiguous` | Multiple VIOs are present and `vio` was null. |
| `invalid_value` | Integer value didn't fit the probe's width, or the radix isn't one we know how to encode. We refuse to silently truncate or wrap. |
| `tcl_error` | Vivado rejected the formatted literal (typically a width mismatch in a string `value`). See `error_info`. |

### debug.write_vio_probes

Drive several VIO output probes coherently in one call.

**Request:**
```json
{"op": "debug.write_vio_probes", "params": {"values": {"mode_in": 2, "setpoint": 65536, "start": 1}}}
```

**Response:**
```json
{"success": true, "vio": "hw_vio_1",
 "values": {"mode_in": "10", "setpoint": "00010000", "start": "1"}}
```

In the request above, `mode_in` is a 2-bit BINARY probe (encoded
`"10"`), `setpoint` is a 32-bit HEX probe (encoded `"00010000"`),
and `start` is a 1-bit BINARY probe (encoded `"1"`).

Each probe is staged with `commit=false`, then a single `commit_hw_vio`
is issued for the whole VIO so the device sees a coherent update. If
any one set fails, the function returns immediately *without*
committing -- everything stays at whatever the device had before the
call. The `failed_probe` and `staged_before_failure` fields tell you
exactly where it stopped.

This is the preferred way to drive control bundles (e.g. mode + start +
run-length) and replaces the "set + commit chained via `;` in raw Tcl"
anti-pattern that silently swallows set errors.

## Build-time operations

### debug.create_vio

**Request:**
```json
{"op": "debug.create_vio", "params": {
  "name": "vio_0",
  "outputs": [
    {"width": 1, "init": 1},
    {"width": 1, "init": 0},
    {"width": 8, "init": 170}
  ],
  "inputs": [
    {"width": 32}
  ],
  "enable_activity_detection": false,
  "overwrite": false
}}
```

**Response:**
```json
{"success": true, "ip": "vio_0", "xci": "D:/.../vio_0/vio_0.xci"}
```

Wraps `create_ip` + `set_property -dict {...}` so callers describe
the VIO with structured JSON instead of memorising the
`CONFIG.C_NUM_PROBE_OUT` / `C_PROBE_OUTn_WIDTH` /
`C_PROBE_OUTn_INIT_VAL` / `C_PROBE_INn_WIDTH` /
`C_EN_PROBE_IN_ACTIVITY` property names.

Tested on Vivado 2024.1. Older Vivado releases may have a
different VIO IP CONFIG schema (some had per-probe
`C_PROBE_INn_TYPE` for edge-type selection that no longer
exists in 2024.1).

Per-probe options:
  - `width` (int, required, 1..256)
  - `init`  (int, output probes only, default 0). Decimal int in JSON;
            the helper rejects hex strings with an explanatory
            `client_error` if you pass `"0xAA"`. Must fit in `width`
            bits.

Top-level options:
  - `enable_activity_detection` (bool, default false): when true,
            sets `CONFIG.C_EN_PROBE_IN_ACTIVITY` on the IP, enabling
            Vivado's runtime activity reporting on every input probe
            (read at runtime via the probe's `ACTIVITY_VALUE`
            property -- see [using_vio.md](using_vio.md) section 5).
            Per-probe edge-type selection is **not available** on
            Vivado 2024.1's VIO IP -- this is the only knob.

Returns the usual ok/fail dict plus:
  - `ip` -- the instance name actually used
  - `xci` -- absolute path to the generated `.xci` (useful for
             `git_management.md` purposes)

Failure modes:
  - `client_error` -- bad probe spec.
  - `ip_exists` -- an IP with this name already exists and
    `overwrite=false`. Pass `overwrite: true` to remove and re-create.
  - `tcl_error` -- propagated from `create_ip` / `set_property` /
    `generate_target`.

This operation only customises the IP; the **OOC synth** that puts
its `.dcp` on disk before the parent `synth_design` references it
is automatic when you call `build.synthesize` with `auto_synth_ips:
true` (the default). See [op_build.md](op_build.md) for
`build.synthesize` and [using_vio.md](using_vio.md) section 1.

### debug.create_ila_core

A synthesized design must be open before this op runs. Typical
sequence:

```bash
# Open the synthesized netlist
echo '{"op":"build.open_synth","params":{}}' | python vivado_op.py

# Insert the ILA core. Use `bus_base + bus_width` shorthand for buses;
# fall back to an explicit `nets` list when bit positions are not
# contiguous or you want to skip bits.
echo '{"op":"debug.create_ila_core","params":{
  "name":"u_ila_0",
  "clock_net":"clk_IBUF",
  "probes":[
    {"name":"count","bus_base":"count","bus_width":16},
    {"name":"en","nets":"en"},
    {"name":"direction","nets":"direction"}
  ],
  "depth":4096,
  "dbg_hub_clock_freq_hz":125000000
}}' | python vivado_op.py

# Close the design and re-run impl to pick up the new debug XDC
echo '{"op":"build.close_design","params":{}}' | python vivado_op.py
echo '{"op":"build.implement","params":{}}' | python vivado_op.py
```

**Response (from `debug.create_ila_core`):**
```json
{
  "success": true,
  "core": "u_ila_0",
  "depth": 4096,
  "clock_net": "clk_IBUF",
  "probes": [
    {"name": "count", "port": "probe0", "width": 16, "nets": ["count[0]", "..."]},
    {"name": "en", "port": "probe1", "width": 1, "nets": ["en"]},
    {"name": "direction", "port": "probe2", "width": 1, "nets": ["direction"]}
  ],
  "xdc_path": "D:/.../constrs_1/imports/debug_u_ila_0.xdc",
  "dbg_hub_clock_freq_hz": 125000000
}
```

`depth` must be one of `1024 / 2048 / 4096 / 8192 / 16384 / 32768 /
65536 / 131072` -- Vivado rejects other values. `dbg_hub_clock_freq_hz`
defaults to 125 MHz (PYNQ-Z1) when omitted.

Wraps the `create_debug_core` + per-probe `connect_debug_port` +
`dbg_hub` clock-fix + dedicated XDC sequence into a single call. The
underlying flow is ~25 lines of Tcl with several footguns; see
[using_ila.md](using_ila.md) section 10b for the mechanics this hides.

Per-probe spec — pass EITHER an explicit `nets` list OR the bus
shorthand, not both:
  - `name` (str, required) -- runtime label / CSV column header
  - `nets` (str | list[str]) -- one net or a list of bit nets
    (e.g. `["count[0]", "count[1]", ...]`). String values are split
    on whitespace. Probe width is implied by `len(nets)`.
  - `bus_base` (str) + `bus_width` (int) -- shorthand for a
    contiguous `[0..bus_width-1]` bus. Expands to
    `[<bus_base>[0], <bus_base>[1], ..., <bus_base>[bus_width-1]]`
    before the rest of the helper runs. Use this whenever bit
    positions are contiguous; fall back to `nets` for irregular
    bit selections.
  - `width` (int, optional) -- redundant sanity check against
    `len(nets)`; mismatch is a `client_error`. Ignored when the
    bus shorthand is used (width = `bus_width` by construction).

Other arguments:
  - `xdc_path` (str | null) -- where the debug XDC ends up. null
    (default) puts it under `<project>.srcs/constrs_1/imports/
    debug_<name>.xdc`. The dedicated XDC means the user-authored
    constraint file (e.g. `pynq_z1.xdc`) stays clean.
  - `dbg_hub_clock_freq_hz` (int) -- override of `C_CLK_INPUT_FREQ_HZ`
    on `dbg_hub`. Vivado's default is 300 MHz, which is wrong on
    most boards; default here is 125 MHz (PYNQ-Z1).
  - `dbg_hub_clock_net` (str | null) -- net for `dbg_hub/clk`.
    null (default) reuses `clock_net`.
  - `overwrite` (bool, default false) -- when true, an existing
    debug core with the same name is deleted first via
    `debug.delete_ila_core`. Default false fails with `core_exists`.

Returns the ok/fail dict plus:
  - `core` -- instance name of the created core.
  - `depth` -- read-back C_DATA_DEPTH.
  - `clock_net` -- read-back net wired to `<name>/clk`.
  - `probes` -- list of `{name, port, width, nets}` dicts read back
    via `get_debug_ports`. Canonicalisation differences (Vivado
    re-formatting the bit-net list) are visible here.
  - `xdc_path` -- absolute path to the XDC file holding the debug
    constraints.
  - `dbg_hub_clock_freq_hz` -- read-back hub frequency.

Failure modes:
  - `client_error` -- bad `probes` spec (missing name/nets, width
    mismatch, no probes, XDC path not creatable).
  - `core_exists` -- a debug core with this name already exists and
    `overwrite=false`.
  - `not_open` -- no synthesized design currently open. Call
    `build.open_synth` first.
  - `tcl_error` -- propagated from `create_debug_core` /
    `connect_debug_port` / `save_constraints`.

The function does not run synth or impl. The caller does that
afterwards (`build.implement` is the usual next step).

### debug.delete_ila_core

Symmetric DELETE for `debug.create_ila_core`. Drops the dedicated
debug XDC from `constrs_1`, deletes the debug core, saves the
resulting constraint set.

```bash
echo '{"op":"build.open_synth","params":{}}' | python vivado_op.py
echo '{"op":"debug.delete_ila_core","params":{"name":"u_ila_0"}}' | python vivado_op.py
echo '{"op":"build.close_design","params":{}}' | python vivado_op.py
echo '{"op":"build.implement","params":{}}' | python vivado_op.py
```

**Response (from `debug.delete_ila_core`):**
```json
{
  "success": true,
  "core": "u_ila_0",
  "removed_xdc": "D:/.../constrs_1/imports/debug_u_ila_0.xdc",
  "residual_dbg_hub": false
}
```

Returns the ok/fail dict plus:
  - `core` -- name of the core that was deleted.
  - `removed_xdc` -- path of the XDC file removed (null if the debug
    constraints lived in a hand-authored XDC and were stripped from
    there by `save_constraints` instead).
  - `residual_dbg_hub` -- true when `dbg_hub` survived the delete
    because (according to Vivado's heuristic) it didn't recognise
    the hub as orphaned. Surfaced for the caller to decide whether
    to drop `delete_debug_core dbg_hub` through the `exec_tcl.py`
    escape hatch manually.

Failure modes:
  - `not_found` -- no debug core with this name on the open design.
  - `not_open` -- no design currently open.
  - `tcl_error` -- propagated from `delete_debug_core` /
    `remove_files` / `save_constraints`.

### debug.find_clock_net

Resolve the post-synth net driving a clock pin on an internal cell.
The value `create_ila_core` / `create_debug_core` need as their
`clock_net` argument is a post-synth net name (typically
`clk_125_IBUF_BUFG`), not the port name `clk` from your HDL.

In a flat post-synth netlist the top-level `clk` port is no longer
addressable as `<top>/clk` — the top module is unwrapped during
synthesis and only the leaf cells (plus the IBUF/BUFG chain) survive.
The reliable recipe is to point at the `clk` pin of a known DUT
instance under the top:

```tcl
get_nets -of_objects [get_pins <instance>/<clock_port>]
```

For example, `u_cordic/clk`, `u_core/clk`, or `u_crc/clk` —
whatever you named the DUT instance in your HDL top. This op wraps
that recipe so it does not have to be re-typed (and re-quoted
through PowerShell, which expands `$` inside double-quoted
`exec_tcl.py "..."` arguments).

Requires the synthesized design to be open: call
`build.open_synth(c)` first. The op does not auto-open it so session
state changes stay explicit.

Request:

```json
{"op": "debug.find_clock_net",
 "params": {"instance": "u_foo", "clock_port": "clk"}}
```

`clock_port` defaults to `"clk"` (the overwhelming convention);
override for designs that use `sys_clk` / `ACLK` / etc.

Response:

```json
{
  "success": true,
  "message": "clock net = clk_125_IBUF_BUFG",
  "net": "clk_125_IBUF_BUFG",
  "candidates": ["clk_125_IBUF_BUFG"],
  "warnings": []
}
```

When more than one net resolves (unusual netlist, e.g. heavy clock
buffer fan-out), `net` is set to the first candidate and a `warnings`
entry lists the full set so the caller can pick explicitly.

#### Failure modes

| `error_kind` | When |
|---|---|
| `no_open_synth` | The synthesized design is not open. Call `build.open_synth(c)` first. |
| `not_found`     | The pin or net could not be resolved. Most common cause: passing the HDL top module name (e.g. "foo_top") instead of an instance name (e.g. "u_foo"). The top is flattened post-synth and is not addressable as `<top>/clk`. |

### debug.verify_probe_nets

Check which net names exist in the current netlist before passing
them to `create_ila_core`. `create_ila_core` accepts a list of nets
silently; if a net was optimised away or mis-typed, it will be wired
to nothing and the discovery happens later (at implementation, or
worse, at runtime when probes return nonsense). Calling this op
first surfaces the problem at design time.

Request — each entry in `nets` is either a string net name or the
same `{bus_base, bus_width}` shorthand `create_ila_core` accepts.
Mix freely:

```json
{"op": "debug.verify_probe_nets",
 "params": {"nets": [
   "foo/bar",
   "foo/baz[7:0]",
   {"bus_base": "samples_out_w", "bus_width": 32},
   "missing_net"
 ]}}
```

Response — the dict entry is expanded to per-bit names before the
check, so `found` / `missing` always contain expanded names:

```json
{
  "success": true,
  "message": "35 net(s): 34 found, 1 missing",
  "found":   ["foo/bar", "foo/baz[7:0]",
              "samples_out_w[0]", "...", "samples_out_w[31]"],
  "missing": ["missing_net"],
  "all_found": false,
  "warnings": []
}
```

`success` is `true` even when `missing` is non-empty — the op
completed and returned its diagnostic. The caller decides whether to
abort or proceed based on `all_found`. Bit-slice notation
(`bus[7:0]`) inside string entries is accepted; it is forwarded to
Vivado's `get_nets` verbatim.

#### Failure modes

| `error_kind` | When |
|---|---|
| `client_error`  | A `bus_*` shorthand entry was malformed (missing key, non-int width, or width &lt; 1). The dispatcher does not reject the call; the helper does, with a precise message. |
| `no_open_synth` | The synthesized design is not open. Call `build.open_synth(c)` first. |

## ILA operations

ILA enumeration, capture, trigger setup, arm/wait, and CSV export
all live under the `ila.*` namespace -- see [op_ila.md](op_ila.md)
for the API reference and [using_ila.md](using_ila.md) for the
design-time flow (mark_debug, IP-mode insertion, naming, capture).

This module no longer carries an `ila` entry point of its own; use
the `ila.*` ops directly.

## Polling pattern

```bash
# Poll a 1 Hz blink probe; a typical run shows clear 0/1 alternation
# in roughly half the samples each.
for i in 1 2 3 4 5; do
  echo '{"op":"debug.read_vio_probe","params":{"probe":"probe_led"}}' | python vivado_op.py
  sleep 0.2
done
```

The dispatcher exits 1 on failure; check exit code or inspect the
`success` field in each response.
