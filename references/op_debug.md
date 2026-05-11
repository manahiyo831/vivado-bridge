# debug operations

`from operations import debug`

Read and write VIO probes, customise the VIO IP at build time, and
insert / delete ILA debug cores into a synthesized design. The
runtime ILA capture flow (configure → set_triggers → arm →
wait_for_capture → export_csv → parse_csv) lives in a separate
module -- see [op_ila.md](op_ila.md) and `from operations import
ila`.

> This document is the **API reference** for `operations.debug`.
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

Trying to call `read_vio_probe(probe="probe_in0")` on a design where
the probe was renamed to `toggle` returns `error_kind="not_found"`.
The fix is mechanical: call `list_vio_probes()` once at the start
of the session, see what names Vivado actually exposes, and use
those.

```python
debug.list_vio_probes(c)["probes"]
# [{'name': 'toggle', 'direction': 'in', 'width': 1, ...}]
debug.read_vio_probe(c, probe="toggle")
```

### list_vios

```python
debug.list_vios(client)
# {'success': True, 'vios': ['hw_vio_1']}
```

Empty when nothing is on the device (programmed yet?) or when the
design has no VIO cores.

### list_vio_probes

Enumerate the probes on a VIO, with direction, width and value-encoding
radix.

```python
debug.list_vio_probes(client, vio="hw_vio_1")
# {
#   'success': True, 'vio': 'hw_vio_1',
#   'probes': [
#     {'name': 'probe_led', 'direction': 'in',  'width': 1,
#      'type_raw': 'vio_input',  'radix': 'BINARY'},
#     {'name': 'setpoint',  'direction': 'out', 'width': 32,
#      'type_raw': 'vio_output', 'radix': 'HEX'},
#   ],
# }
```

`width` and `radix` are read directly off the runtime hw_probe object
(via `get_property WIDTH` / `OUTPUT_VALUE_RADIX` / `INPUT_VALUE_RADIX`),
which is what `set_property OUTPUT_VALUE` and `get_property INPUT_VALUE`
will validate against. We deliberately do not try to recover them from
the IP's `CONFIG.C_PROBE_INn_WIDTH`: when an ILA shares wires with a VIO
the runtime probes get renamed (pitfall #6) and the IP-port-to-runtime
mapping becomes fragile.

If the result has zero probes but `list_vios()` shows the VIO, the
`.ltx` (probes) file probably wasn't attached during programming. Use
`hardware.program_device(ltx_path=...)` to fix that.

`vio` defaults to None and auto-resolves when there's exactly one VIO.

### read_vio_probe

Read a single input probe.

```python
debug.read_vio_probe(client, probe="probe_led")
# {'success': True, 'vio': 'hw_vio_1',
#  'probe': 'probe_led', 'value': '1'}
```

`value` is the raw string Vivado gave us. The format depends on the
probe's `INPUT_VALUE_RADIX`: typically hex characters for HEX /
UNSIGNED / SIGNED probes (e.g. `'0001fffe'`) and a 0/1 string for
BINARY probes.

Pass `as_int=True` to also get a decoded `int_value` field, which is
the most ergonomic path for multi-bit numeric probes:

```python
debug.read_vio_probe(client, probe="adc_sample", as_int=True)
# {'success': True, 'vio': 'hw_vio_1',
#  'probe': 'adc_sample', 'value': 'ffffe000',
#  'int_value': -8192, 'radix': 'SIGNED', 'width': 16, ...}
```

SIGNED probes are sign-extended within their width, so `-1` on a
16-bit signed probe round-trips as `int_value=-1` (raw `'ffff'`).
If decoding fails (unexpected radix, malformed Vivado response) the
raw `value` is still returned and a `warnings` entry explains why
`int_value` was not populated -- we deliberately do not guess.

`refresh=True` (default) calls `refresh_hw_vio` first so the value is
fresh. For polling loops you can leave it on; refresh is cheap.

#### Failure modes

| `error_kind` | When |
|---|---|
| `not_found` | Probe doesn't exist on the chosen VIO. |
| `wrong_direction` | Probe is an output -- use `write_vio_probe` instead. |
| `ambiguous` | Multiple VIOs are present and `vio` was None. |
| `tcl_error` | The probe exists but the underlying `get_property INPUT_VALUE` failed (e.g. target dropped during refresh). |

#### Note on the `value` field

`value` is only present on `success=True` results -- pitfall #5
(op-specific fields appear only on success) applies. On failure use
`r["message"]` and `r["error_kind"]`; `r.get("value")` if you absolutely
need a one-liner that won't raise, but echoing the failure message back
to the user is almost always more useful.

### read_vio_probes_all

Read every input probe on a VIO in one call.

```python
debug.read_vio_probes_all(client)
# {'success': True, 'vio': 'hw_vio_1',
#  'values': {'probe_led': '1', 'probe_cntmsb': '1'}}
```

Output probes are skipped (they're driven *by* you, not read from the
device).

If any single probe fails to read, the whole call fails with
`error_kind="tcl_error"` and `failed_probes` listing the offenders.
We deliberately do not return partial values with empty strings for
the failed ones -- that would silently hide read errors. Use
`read_vio_probe` per-probe to isolate which one is broken.

### write_vio_probe

Drive a VIO output probe. The recommended path is to pass an `int` and
let the bridge encode it:

```python
debug.write_vio_probe(client, probe="setpoint", value=0x10000)
# 32-bit HEX probe -> Vivado sees "00010000" (8 hex digits)

debug.write_vio_probe(client, probe="enable", value=1)
# 1-bit BINARY probe -> Vivado sees "1"

debug.write_vio_probe(client, probe="offset", value=-12345)
# 16-bit SIGNED probe -> Vivado sees the two's-complement form
```

The bridge looks up the probe's `WIDTH` and `OUTPUT_VALUE_RADIX` and
formats accordingly, so you don't have to count hex digits or remember
that Vivado checks digit *count* (a 32-bit HEX probe rejects `"1"`
with `[Designutils 20-1474] has [1] value characters; required [8]`).

You can still pass a pre-formatted string if you have one:

```python
debug.write_vio_probe(client, probe="enable", value="1")     # binary
debug.write_vio_probe(client, probe="setpoint", value="0001fffe")  # hex
```

Width and digit count must match exactly; Vivado will reject otherwise.

`commit=False` stages the value but doesn't push it to the device.
For driving multiple probes atomically, prefer `write_vio_probes`
below -- it commits exactly once and aborts cleanly on partial
failure.

#### Failure modes

| `error_kind` | When |
|---|---|
| `not_found` | Probe doesn't exist on the chosen VIO. |
| `wrong_direction` | Probe is an input -- use `read_vio_probe` instead. |
| `ambiguous` | Multiple VIOs are present and `vio` was None. |
| `invalid_value` | `int` value didn't fit the probe's width, or the radix isn't one we know how to encode. We refuse to silently truncate or wrap. |
| `tcl_error` | Vivado rejected the formatted literal (typically a width mismatch in a string `value`). See `error_info`. |

### write_vio_probes

Drive several VIO output probes coherently in one call.

```python
debug.write_vio_probes(client, values={
    "mode_in":  2,        # 2-bit BINARY -> "10"
    "setpoint": 0x10000,  # 32-bit HEX   -> "00010000"
    "start":    1,        # 1-bit BINARY -> "1"
})
# {'success': True, 'vio': 'hw_vio_1',
#  'values': {'mode_in': '10', 'setpoint': '00010000', 'start': '1'}}
```

Each probe is staged with `commit=False`, then a single `commit_hw_vio`
is issued for the whole VIO so the device sees a coherent update. If
any one set fails, the function returns immediately *without*
committing -- everything stays at whatever the device had before the
call. The `failed_probe` and `staged_before_failure` fields tell you
exactly where it stopped.

This is the preferred way to drive control bundles (e.g. mode + start +
run-length) and replaces the "set + commit chained via `;` in raw Tcl"
anti-pattern that silently swallows set errors.

## Build-time helpers

These helpers run at design-build time (not at runtime against a programmed
device). `create_vio` customises a VIO IP before synthesis; `create_ila_core`
and `delete_ila_core` insert / remove a debug core into an already-synthesized
design and require `build.open_synth(client)` first. Each wraps a multi-step
Vivado Tcl flow that is easy to get wrong by hand.

### create_vio

```python
debug.create_vio(
    client,
    name="vio_0",
    outputs=[
        {"width": 1, "init": 1},          # rst
        {"width": 1, "init": 0},          # en
        {"width": 8, "init": 0xAA},
    ],
    inputs=[
        {"width": 32},                    # observe count
    ],
    enable_activity_detection=False,
    overwrite=False,
)
```

Wraps `create_ip` + `set_property -dict {...}` so callers describe
the VIO with structured Python instead of memorising the
`CONFIG.C_NUM_PROBE_OUT` / `C_PROBE_OUTn_WIDTH` /
`C_PROBE_OUTn_INIT_VAL` / `C_PROBE_INn_WIDTH` /
`C_EN_PROBE_IN_ACTIVITY` property names.

Tested on Vivado 2024.1. Older Vivado releases may have a
different VIO IP CONFIG schema (some had per-probe
`C_PROBE_INn_TYPE` for edge-type selection that no longer
exists in 2024.1).

Per-probe options:
  - `width` (int, required, 1..256)
  - `init`  (int, output probes only, default 0). Must fit in `width`
            bits; raised as `client_error` otherwise.

Top-level options:
  - `enable_activity_detection` (bool, default False): when True,
            sets `CONFIG.C_EN_PROBE_IN_ACTIVITY` on the IP, enabling
            Vivado's runtime activity reporting on every input probe
            (read at runtime via the probe's `ACTIVITY_VALUE`
            property — see [using_vio.md](using_vio.md) §5).
            Per-probe edge-type selection is **not available** on
            Vivado 2024.1's VIO IP -- this is the only knob.

Returns the usual ok/fail dict plus:
  - `ip` -- the instance name actually used
  - `xci` -- absolute path to the generated `.xci` (useful for
             `git_management.md` purposes)

Failure modes:
  - `client_error` -- bad probe spec.
  - `ip_exists` -- an IP with this name already exists and
    `overwrite=False`. Pass `overwrite=True` to remove and re-create.
  - `tcl_error` -- propagated from `create_ip` / `set_property` /
    `generate_target`.

This operation only customises the IP; the **OOC synth** that puts
its `.dcp` on disk before the parent `synth_design` references it
is automatic when you call `build.synthesize(auto_synth_ips=True)`
(the default). See [op_build.md#synthesize](op_build.md#synthesize)
and [using_vio.md](using_vio.md) §1.

### create_ila_core

```python
from operations import build, debug

build.open_synth(client)            # required: a synth design must be open

debug.create_ila_core(
    client,
    name="u_ila_0",
    clock_net="clk_IBUF",           # post-synth net feeding the ILA clk port
    probes=[
        {"name": "count",     "nets": [f"count[{i}]" for i in range(16)]},
        {"name": "en",        "nets": "en"},          # str -> 1-bit probe
        {"name": "direction", "nets": "direction"},
    ],
    depth=4096,                           # 1024 / 2048 / 4096 / 8192
                                          # / 16384 / 32768 / 65536 /
                                          # 131072 only — Vivado rejects
                                          # other values
    dbg_hub_clock_freq_hz=125_000_000,    # PYNQ-Z1 default
)

build.close_design(client)
build.implement(client)             # impl picks up the new debug XDC
```

Wraps the `create_debug_core` + per-probe `connect_debug_port` +
`dbg_hub` clock-fix + dedicated XDC sequence into a single call. The
underlying flow is ~25 lines of Tcl with several footguns; see
[using_ila.md](using_ila.md) §10b for the mechanics this hides.

Per-probe spec:
  - `name` (str, required) -- runtime label / CSV column header
  - `nets` (str | list[str], required) -- one net or a list of bit
    nets (e.g. `[f"count[{i}]" for i in range(16)]`). String values
    are split on whitespace. Probe width is implied by `len(nets)`.
  - `width` (int, optional) -- redundant sanity check against
    `len(nets)`; mismatch is a `client_error`.

Other arguments:
  - `xdc_path` (str | None) -- where the debug XDC ends up. None
    (default) puts it under `<project>.srcs/constrs_1/imports/
    debug_<name>.xdc`. The dedicated XDC means the user-authored
    constraint file (e.g. `pynq_z1.xdc`) stays clean.
  - `dbg_hub_clock_freq_hz` (int) -- override of `C_CLK_INPUT_FREQ_HZ`
    on `dbg_hub`. Vivado's default is 300 MHz, which is wrong on
    most boards; default here is 125 MHz (PYNQ-Z1).
  - `dbg_hub_clock_net` (str | None) -- net for `dbg_hub/clk`.
    None (default) reuses `clock_net`.
  - `overwrite` (bool, default False) -- when True, an existing
    debug core with the same name is deleted first via
    `delete_ila_core`. Default False fails with `core_exists`.

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
  - `net_not_found` -- one or more requested nets don't resolve on
    the synthesized design. Pre-flight check via `get_nets -quiet
    <name>`; the result includes `missing_nets` (the names that
    failed) and `requested_nets` (the full list as passed in). Common
    causes: typo in the HDL wire name, the net was optimized away
    (add `keep = "true"` or `mark_debug = "true"` in HDL), or a
    hierarchical name without `KEEP_HIERARCHY` on the wrapping module.
  - `core_exists` -- a debug core with this name already exists and
    `overwrite=False`.
  - `not_open` -- no synthesized design currently open. Call
    `build.open_synth(client)` first.
  - `tcl_error` -- propagated from `create_debug_core` /
    `connect_debug_port` / `save_constraints`.

The function does not run synth or impl. The caller does that
afterwards (`build.implement(client)` is the usual next step).

What `create_ila_core` does **not** detect: a `connect_debug_port`
call that succeeds at the Tcl level but binds 0 channels because
the net is filtered out of the debug graph (the canonical case is
a VIO-attached net without `mark_debug_valid = "true"` -- see
`using_ila.md` §8.5). The Vivado 2024.1 query for "how many channels
did this port actually bind?" returns empty for both the working
and the broken case, so we can't distinguish them up-front. Such
binds fail at impl time with `[Chipscope 16-213] probeN has K
unconnected channels`; the fix is to add `mark_debug_valid` in HDL
and re-synth.

### delete_ila_core

```python
from operations import build, debug

build.open_synth(client)
debug.delete_ila_core(client, name="u_ila_0")
build.close_design(client)
build.implement(client)
```

Symmetric DELETE for `create_ila_core`. Drops the dedicated debug
XDC from `constrs_1`, deletes the debug core, saves the resulting
constraint set.

Returns the ok/fail dict plus:
  - `core` -- name of the core that was deleted.
  - `removed_xdc` -- path of the XDC file removed (None if the debug
    constraints lived in a hand-authored XDC and were stripped from
    there by `save_constraints` instead).
  - `residual_dbg_hub` -- True when `dbg_hub` survived the delete
    because (according to Vivado's heuristic) it didn't recognise
    the hub as orphaned. Surfaced for the caller to decide whether
    to issue `client.exec_tcl("delete_debug_core dbg_hub")` manually.

Failure modes:
  - `not_found` -- no debug core with this name on the open design.
  - `not_open` -- no design currently open.
  - `tcl_error` -- propagated from `delete_debug_core` /
    `remove_files` / `save_constraints`.

## ILA operations

ILA enumeration, capture, trigger setup, arm/wait, and CSV export
all live in `operations.ila` -- see [op_ila.md](op_ila.md) for the
API reference and [using_ila.md](using_ila.md) for the design-time
flow (mark_debug, IP-mode insertion, naming, capture).

This module no longer carries an `ila` entry point of its own; use
`from operations import ila` directly.

## Polling pattern

```python
import time
from vivado_bridge_client import Client
from operations import debug

c = Client.connect()
for _ in range(50):
    r = debug.read_vio_probe(c, probe="probe_led")
    print(r["value"])
    time.sleep(0.2)
```

A typical ~1 Hz blink shows clear 0/1 alternation in roughly half the
samples each.
