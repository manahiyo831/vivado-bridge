# ila operations

`from operations import ila`

ILA capture flow as Python verbs: configure depth and trigger position,
arm the core, wait (in Python, not Tcl) for the buffer to fill, upload
and export to CSV. Plus a host-side CSV parser that handles Vivado's
bit-slice column names and per-column radix.

The design-time picture (mark_debug, debug XDC, the auto-start
generator pattern) lives in [using_ila.md](using_ila.md). This module
is for the runtime side: you've programmed the device, the ILA is
visible (`list_ilas` returns a name), and you want samples in a CSV.

## Common shape

All operations return a dict with `success`, `error_kind`, `message`,
`warnings`. On success the operation-specific fields below are added.

## Why these operations exist (what *not* to do in raw Tcl)

The Vivado ILA Tcl surface has three traps; this module sidesteps each:

1. **`wait_on_hw_ila -timeout 5` is unreliable.** It has been observed
   to interpret the timeout in units other than seconds (a "5" wait
   blocked for ~5 minutes in one session) and, worse, blocks Vivado's
   Tcl interpreter -- which then blocks the bridge. Use `wait_for_capture`
   instead; it polls `CORE_STATUS` from Python with a real deadline.
2. **Some control properties are read-only on synthesized cores.**
   `CONTROL.CAPTURE_MODE` / `CONTROL.TRIGGER_CONDITION` / `TRIGGER_MODE`
   are listed as configurable in the docs but are read-only on cores
   that were synthesized without storage qualification or with a single
   trigger probe. We never write them. If you really need them, send the
   raw `set_property` yourself and let Vivado reject it loudly.
3. **`implement_debug_core` is almost never necessary.** When `mark_debug`
   constraints are in `constrs_1`, Vivado picks them up during
   `opt_design`. Calling `implement_debug_core` explicitly tends to
   outlast the bridge's default exec_tcl timeout. This module never
   calls it; the build flow doesn't either. Even if older notes
   mention it, skip the call -- the auto-pickup path is the supported
   flow on Vivado 2024.1.

## Operations

### list_ilas

```python
ila.list_ilas(client)
# {'success': True, 'ilas': ['hw_ila_1']}
```

### list_ila_probes

```python
ila.list_ila_probes(client, ila=None)
# {'success': True, 'ila': 'hw_ila_1',
#  'probes': [
#      {'name': 'count', 'port': 'probe0', 'width': 32,
#       'is_trigger': True, 'is_data': True},
#      {'name': 'en',    'port': 'probe1', 'width':  1,
#       'is_trigger': True, 'is_data': True},
#      {'name': 'rst',   'port': 'probe2', 'width':  1,
#       'is_trigger': True, 'is_data': True},
#  ]}
```

Mirrors `debug.list_vio_probes`: returns structured per-probe info
so callers can iterate and read `name` / `width` / `port` /
`is_trigger` / `is_data` directly instead of parsing the flat
`get_hw_probes` output by hand.

If `ila` is None and only one ILA exists, it is used implicitly.

Failure modes:
  - `not_found` -- no ILAs on device, or named ILA missing.
  - `ambiguous` -- ila=None but multiple ILAs are present.
  - `tcl_error` -- propagated from `get_hw_probes` / `get_property`.

### configure

```python
ila.configure(client, depth=4096, trigger_position=16)
# {'success': True, 'ila': 'hw_ila_1', 'depth': 4096, 'trigger_position': 16}
```

Only `depth` (CONTROL.DATA_DEPTH) and `trigger_position`
(CONTROL.TRIGGER_POSITION) are exposed. Both can be omitted; the call
becomes a no-op if both are None. Pass exactly the values you need.

`depth` must be one of the values the IP was synthesized with --
typically 1024, 2048, or 4096. `trigger_position` is the index of the
trigger sample within the buffer (0 = all-post-trigger, depth/2 ≈
balanced).

### set_triggers

Set per-probe trigger conditions atomically. The recommended way to
arm an ILA -- it sidesteps the trigger-AND footgun (trigger compare
values persist on probes across runs and AND together under the
default GLOBAL_AND condition).

```python
# 95% of cases: a single condition + reset everything else.
ila.set_triggers(client, values={"dbg_start": "rising"})
# default clear_others=True resets every other probe to don't-care.

# Multiple conditions ANDed together:
ila.set_triggers(client, values={
    "dbg_start": "rising",
    "dbg_mode":  0x2,        # int → eq2'h2 (probe width is read from the core)
    "dbg_busy":  False,      # → eq1'b0
})
```

Accepted value forms (per probe):
- Vivado literal string (e.g. `"eq8'h2A"`) -- passed through verbatim
- `True` / `False`                          → all-1s / all-0s of the probe width
- `int`                                     → hex literal sized to the probe width
- `"rising"` / `"falling"` / `"both"` / `"either"` → 1-bit edge characters
- `"X"` / `"x"` / `"*"`                     → all don't-care

`clear_others` (default `True`): probes not in `values` are reset to
don't-care first. Pass `False` only when you deliberately want to
preserve previous compare values on the unlisted probes.

Returns ok/fail dict plus:
- `set`: dict {probe: literal} -- value Vivado read back for each
  probe the caller named.
- `cleared`: dict {probe: literal} -- probes reset to don't-care
  (when `clear_others=True`).
- `unchanged`: list of probe names left as-is (when `clear_others=False`).
- `trigger_condition`: current `CONTROL.TRIGGER_CONDITION`. Vivado
  reports this as `GLOBAL_AND` on 2024.1 and as `AND` on 2021.1 — the
  underlying behaviour (every probe's compare value AND-ed into the
  trigger) is the same; only the property string differs.

Failure modes:
- `not_found`: one or more `values` keys aren't probes on the ILA.
  **No on-core state is modified** -- the function fails up-front.
  Result includes `unmatched` (the bad names) and `available_probes`.
- `client_error`: a shorthand value isn't valid for the probe (e.g.
  `"rising"` on a multi-bit probe).
- `tcl_error`: Vivado rejected the literal on `set_property`.

### arm

```python
ila.arm(client)
# {'success': True, 'ila': 'hw_ila_1'}
```

Equivalent to clicking "Run trigger" in the GUI. Returns immediately;
the core then waits in hardware for the trigger condition.

### wait_for_capture

```python
ila.wait_for_capture(client, timeout=5.0)
# {'success': True, 'status': 'Idle  Has Data', 'ila': 'hw_ila_1'}
```

Polls `CORE_STATUS` every `poll` seconds (default 0.2). Returns
`success=True` when the buffer reports `"Has Data"` or `"Full"`.
`error_kind="timeout"` if the deadline expires; we do not reset the
ILA on timeout, so you can inspect why it never triggered.

### get_status

```python
ila.get_status(client)
# {'success': True,
#  'status': 'WAITING FOR TRIGGER',
#  'status_lower': 'waiting for trigger',
#  'sample_count': 0,
#  ...}
```

Reads `STATUS.CORE_STATUS` plus `STATUS.SAMPLE_COUNT`. The status
string is unstructured and varies between Vivado versions (we've
observed `IDLE`, `WAITING FOR TRIGGER`, `FULL` on 2024.1; older docs
mention mixed-case forms like `Idle  Has Data`). `status_lower` is
the same string lowercased so callers can match without worrying
about case. `sample_count` is the most reliable "did anything actually
get captured" signal across versions -- `wait_for_capture` uses it
to disambiguate the "ILA bounced back to IDLE after auto-draining"
race.

### export_csv

Upload the latest capture and write CSV in one call:

```python
ila.export_csv(client, path="results/capture.csv")
# {'success': True, 'csv_path': 'results/capture.csv', 'bytes': 142080}
```

Wraps `upload_hw_ila_data` + `write_hw_ila_data -csv_file`. Creates
the parent directory if missing (the bridge blocks `file mkdir` from
Tcl, but Python can do it host-side).

### parse_csv (host-side)

```python
result = ila.parse_csv(
    "results/capture.csv",
    signed_columns={"fir_out": 16, "sp_obs": 32},
)
# {'success': True, 'columns': [...], 'radix': [...], 'rows': [...]}
```

Standard-library only -- no client, no Vivado. Strips Vivado's
preamble, decodes hex/binary tokens using the per-column radix from
the second header row, and sign-extends columns named in
`signed_columns` within the given width. To look up a column by base
name (without remembering the bit-slice suffix Vivado adds), see
[`find_column`](#find_column) below.

Cells that don't decode (xsim `x`/`X` from uninitialised BRAM, an
unrecognised radix from a future Vivado version, etc.) are stored as
`None` in the row dicts -- *not* silently substituted with 0. Each
undecoded cell appears in `decode_failures` (a list of
`(row_idx, column, raw_token)` tuples), and the result's `warnings`
field summarises the count plus a short sample. The reason this
matters: a 0 that came from an undecoded sample causes downstream
computations to look fine until much later.

### find_column

```python
fir_idx = ila.find_column(parsed["columns"], "fir_out")
fir_col = parsed["columns"][fir_idx]
samples = [r[fir_col] for r in parsed["rows"]]
```

Look up a column index in a `parse_csv` result by **base name**.
Vivado's CSV header writes multi-bit probes with a bit-slice suffix
(e.g. a probe named `fir_out` shows up as `fir_out[31:0]`), and the
exact width can change between builds. `find_column` matches the
base name and transparently absorbs the suffix, so callers don't
have to hard-code `"fir_out[31:0]"` and re-edit the script every
time the probe width changes.

Returns the integer index into `parsed["columns"]`. Raises
`ValueError` if no column matches, or if more than one column
matches the same base name (which would indicate a malformed CSV --
two probes with the same name).

Standard-library only; no client, no Vivado. Pair with `parse_csv`.

## End-to-end recipe

```python
from operations import ila

ila.configure(c, depth=4096, trigger_position=16)
ila.set_triggers(c, values={"dbg_start": "rising"})
ila.arm(c)
# stimulus the design here (write_vio_probe etc.)
got = ila.wait_for_capture(c, timeout=5.0)
if not got["success"]:
    print(got["message"]); raise SystemExit(1)
ila.export_csv(c, path="results/capture.csv")

# Analyse without Vivado:
parsed = ila.parse_csv(
    "results/capture.csv",
    signed_columns={"fir_out": 16},
)
fir_idx = ila.find_column(parsed["columns"], "fir_out")
fir_col = parsed["columns"][fir_idx]
samples = [r[fir_col] for r in parsed["rows"]]
```
