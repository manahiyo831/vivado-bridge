# ila operations

All operations are invoked via the `vivado_op.py` JSON dispatcher.
See [SKILL.md](../SKILL.md) for the invocation pattern.

ILA capture flow as JSON operations: configure depth and trigger
position, arm the core, wait (in Python, not Tcl) for the buffer to
fill, upload and export to CSV. Plus a host-side CSV parser that
handles Vivado's bit-slice column names and per-column radix.

The design-time picture (mark_debug, debug XDC, the auto-start
generator pattern) lives in [using_ila.md](using_ila.md). This module
is for the runtime side: you've programmed the device, the ILA is
visible (`ila.list_ilas` returns a name), and you want samples in a
CSV.

## Common shape

All operations return a dict with `success`, `error_kind`, `message`,
`warnings`. On success the operation-specific fields below are added.

## Why these operations exist (what *not* to do in raw Tcl)

The Vivado ILA Tcl surface has three traps; this module sidesteps each:

1. **`wait_on_hw_ila -timeout 5` is unreliable.** It has been observed
   to interpret the timeout in units other than seconds (a "5" wait
   blocked for ~5 minutes in one session) and, worse, blocks Vivado's
   Tcl interpreter -- which then blocks the bridge. Use
   `ila.wait_for_capture` instead; it polls `CORE_STATUS` from Python
   with a real deadline.
2. **Some control properties are read-only on synthesized cores.**
   `CONTROL.CAPTURE_MODE` / `CONTROL.TRIGGER_CONDITION` / `TRIGGER_MODE`
   are listed as configurable in the docs but are read-only on cores
   that were synthesized without storage qualification or with a single
   trigger probe. We never write them. If you really need them, send the
   raw `set_property` yourself via `exec_tcl.py` and let Vivado reject
   it loudly.
3. **`implement_debug_core` is almost never necessary.** When `mark_debug`
   constraints are in `constrs_1`, Vivado picks them up during
   `opt_design`. Calling `implement_debug_core` explicitly tends to
   outlast the bridge's default exec_tcl timeout. This module never
   calls it; the build flow doesn't either. Even if older notes
   mention it, skip the call -- the auto-pickup path is the supported
   flow on Vivado 2024.1.

## Operations

### ila.list_ilas

Request:

```json
{"op": "ila.list_ilas", "params": {}}
```

Response:

```json
{"success": true, "ilas": ["hw_ila_1"]}
```

### ila.list_ila_probes

Request:

```json
{"op": "ila.list_ila_probes", "params": {"ila": null}}
```

Response:

```json
{
  "success": true,
  "ila": "hw_ila_1",
  "probes": [
    {"name": "count", "port": "probe0", "width": 32, "is_trigger": true, "is_data": true},
    {"name": "en",    "port": "probe1", "width":  1, "is_trigger": true, "is_data": true},
    {"name": "rst",   "port": "probe2", "width":  1, "is_trigger": true, "is_data": true}
  ]
}
```

Mirrors `debug.list_vio_probes`: returns structured per-probe info
so callers can iterate and read `name` / `width` / `port` /
`is_trigger` / `is_data` directly instead of parsing the flat
`get_hw_probes` output by hand.

If `ila` is `null` and only one ILA exists, it is used implicitly.

Failure modes:
  - `not_found` -- no ILAs on device, or named ILA missing.
  - `ambiguous` -- `ila` is `null` but multiple ILAs are present.
  - `tcl_error` -- propagated from `get_hw_probes` / `get_property`.

### ila.configure

Request:

```json
{"op": "ila.configure", "params": {"depth": 4096, "trigger_position": 16}}
```

Response:

```json
{"success": true, "ila": "hw_ila_1", "depth": 4096, "trigger_position": 16}
```

Only `depth` (CONTROL.DATA_DEPTH) and `trigger_position`
(CONTROL.TRIGGER_POSITION) are exposed. Both can be omitted; the call
becomes a no-op if both are absent. Pass exactly the values you need.

`depth` must be one of the values the IP was synthesized with --
typically 1024, 2048, or 4096. `trigger_position` is the index of the
trigger sample within the buffer (0 = all-post-trigger, depth/2 ≈
balanced).

### ila.set_triggers

Set per-probe trigger conditions atomically. The recommended way to
arm an ILA -- it sidesteps the trigger-AND footgun (trigger compare
values persist on probes across runs and AND together under the
default GLOBAL_AND condition).

Request (95% of cases: a single condition + reset everything else):

```json
{"op": "ila.set_triggers", "params": {"values": {"dbg_start": "rising"}}}
```

Default `clear_others=true` resets every other probe to don't-care.

Multiple conditions ANDed together:

```json
{"op": "ila.set_triggers", "params": {"values": {
  "dbg_start": "rising",
  "dbg_mode": 2,
  "dbg_busy": false
}}}
```

`dbg_mode: 2` → `eq2'h2` (probe width is read from the core).
`dbg_busy: false` → `eq1'b0`.

Accepted value forms (per probe):
- Vivado literal string (e.g. `"eq8'h2A"`) -- passed through verbatim
- `true` / `false`                          → all-1s / all-0s of the probe width
- integer                                   → hex literal sized to the probe width
- `"rising"` / `"falling"` / `"both"` / `"either"` → 1-bit edge characters
- `"X"` / `"x"` / `"*"`                     → all don't-care

`clear_others` (default `true`): probes not in `values` are reset to
don't-care first. Pass `false` only when you deliberately want to
preserve previous compare values on the unlisted probes.

Returns ok/fail dict plus:
- `set`: dict `{probe: literal}` -- value Vivado read back for each
  probe the caller named.
- `cleared`: dict `{probe: literal}` -- probes reset to don't-care
  (when `clear_others=true`).
- `unchanged`: list of probe names left as-is (when `clear_others=false`).
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

### ila.arm

Request:

```json
{"op": "ila.arm", "params": {}}
```

Response:

```json
{"success": true, "ila": "hw_ila_1"}
```

Equivalent to clicking "Run trigger" in the GUI. Returns immediately;
the core then waits in hardware for the trigger condition.

### ila.wait_for_capture

Request:

```json
{"op": "ila.wait_for_capture", "params": {"timeout": 5.0}}
```

Response:

```json
{"success": true, "status": "Idle  Has Data", "ila": "hw_ila_1"}
```

Polls `CORE_STATUS` every `poll` seconds (default 0.2). Returns
`success=true` when the buffer reports `"Has Data"` or `"Full"`.
`error_kind="timeout"` if the deadline expires; we do not reset the
ILA on timeout, so you can inspect why it never triggered.

Polling pattern: `ila.wait_for_capture` already encapsulates the
polling loop, so a single call with a generous `timeout` is the
normal pattern. Don't wrap it in your own outer poll loop unless
you need to interleave other work (e.g. driving stimulus via VIO
writes while waiting).

### ila.get_status

Request:

```json
{"op": "ila.get_status", "params": {}}
```

Response:

```json
{
  "success": true,
  "status": "WAITING FOR TRIGGER",
  "status_lower": "waiting for trigger",
  "sample_count": 0
}
```

Reads `STATUS.CORE_STATUS` plus `STATUS.SAMPLE_COUNT`. The status
string is unstructured and varies between Vivado versions (we've
observed `IDLE`, `WAITING FOR TRIGGER`, `FULL` on 2024.1; older docs
mention mixed-case forms like `Idle  Has Data`). `status_lower` is
the same string lowercased so callers can match without worrying
about case. `sample_count` is the most reliable "did anything actually
get captured" signal across versions -- `ila.wait_for_capture` uses
it to disambiguate the "ILA bounced back to IDLE after auto-draining"
race.

### ila.export_csv

Upload the latest capture and write CSV in one call:

Request:

```json
{"op": "ila.export_csv", "params": {"path": "results/capture.csv"}}
```

Response:

```json
{"success": true, "csv_path": "results/capture.csv", "bytes": 142080}
```

Wraps `upload_hw_ila_data` + `write_hw_ila_data -csv_file`. Creates
the parent directory if missing (the bridge blocks `file mkdir` from
Tcl, but Python can do it host-side).

### ila.parse_csv (host-side)

Request:

```json
{"op": "ila.parse_csv", "params": {
  "csv_path": "results/capture.csv",
  "signed_columns": {"fir_out": 16, "sp_obs": 32}
}}
```

Response:

```json
{
  "success": true,
  "columns": ["Sample in Buffer", "Sample in Window", "TRIGGER", "valid_in", "count_dbg[7:0]"],
  "radix": ["UNSIGNED", "UNSIGNED", "UNSIGNED", "HEX", "HEX"],
  "rows": [
    {"Sample in Buffer": 0, "Sample in Window": 0, "TRIGGER": 0, "valid_in": 0, "count_dbg[7:0]": 0},
    {"Sample in Buffer": 1, "Sample in Window": 1, "TRIGGER": 0, "valid_in": 1, "count_dbg[7:0]": 3}
  ]
}
```

Field shapes:

- `columns`: a flat list of **strings** — the column header names taken
  from the CSV's first header row, in their original order. Multi-bit
  probes keep their `name[hi:lo]` suffix (e.g. `count_dbg[7:0]`); use
  `ila.find_column` to look up by base name.
- `radix`: a flat list of **strings**, parallel to `columns` (same
  length, `radix[i]` is the radix declared by Vivado for `columns[i]`).
  Common values: `"UNSIGNED"`, `"HEX"`, `"BINARY"`. The first entry is
  literally `"Radix - UNSIGNED"` in older Vivado CSV preambles; the
  parser leaves it as Vivado wrote it.
- `rows`: a list of **dicts** keyed by column name. Cells decode to
  Python `int` for numeric columns; cells that couldn't be decoded
  (xsim `x`, an unrecognised radix) are stored as `null` and tracked
  separately in `decode_failures` / `warnings`.

Standard-library only -- no Vivado round trip required. Strips
Vivado's preamble, decodes hex/binary tokens using the per-column
radix from the second header row, and sign-extends columns named in
`signed_columns` within the given width. Use `ila.find_column` (a
sibling op) to look up a column by base name (it transparently
matches the bit-slice suffix Vivado writes, e.g. `my_probe[31:0]`).

Cells that don't decode (xsim `x`/`X` from uninitialised BRAM, an
unrecognised radix from a future Vivado version, etc.) are stored as
`null` in the row dicts -- *not* silently substituted with 0. Each
undecoded cell appears in `decode_failures` (a list of
`[row_idx, column, raw_token]` tuples), and the result's `warnings`
field summarises the count plus a short sample. The reason this
matters: a 0 that came from an undecoded sample causes downstream
computations to look fine until much later.

### ila.find_column (host-side)

Look up a column index in a `parse_csv` result by **base name**.
Vivado's CSV header writes multi-bit probes with a bit-slice suffix
(e.g. a probe named `fir_out` shows up as `fir_out[31:0]`), and the
exact width can change between builds. `find_column` matches the
base name and transparently absorbs the suffix, so callers don't
have to hard-code `"fir_out[31:0]"` and re-edit the script every
time the probe width changes.

**Request:**
```json
{"op": "ila.find_column", "params": {
  "columns": ["timestamp", "fir_out[31:0]", "valid"],
  "target": "fir_out"
}}
```

**Response:**
```json
{"success": true, "result": 1}
```

The integer index points into the `columns` array passed in `params`.
The dispatcher wraps the bare-int return as `{"success": true,
"result": <index>}`. On no match or multiple matches, the underlying
helper raises `ValueError`, which the dispatcher surfaces as a
`helper_exception` with a message describing whether it was a
zero-match or duplicate-match failure.

Standard-library only; no Vivado round trip. Pair with `ila.parse_csv`
when you need to look up a probe by base name from a freshly parsed
CSV.

## End-to-end recipe

```bash
python vivado_op.py '{"op":"ila.configure","params":{"depth":4096,"trigger_position":16}}'
python vivado_op.py '{"op":"ila.set_triggers","params":{"values":{"dbg_start":"rising"}}}'
python vivado_op.py '{"op":"ila.arm","params":{}}'
# stimulus the design here (e.g. debug.write_vio_probe via vivado_op.py)
python vivado_op.py '{"op":"ila.wait_for_capture","params":{"timeout":5.0}}'
python vivado_op.py '{"op":"ila.export_csv","params":{"path":"results/capture.csv"}}'

# Analyse without Vivado:
python vivado_op.py '{"op":"ila.parse_csv","params":{"csv_path":"results/capture.csv","signed_columns":{"fir_out":16}}}'
```

Branch on `success` between steps: if `ila.wait_for_capture` returns
`error_kind="timeout"`, skip the export and inspect why the trigger
never fired (stale conditions, design not generating the edge, etc.)
before re-arming.
