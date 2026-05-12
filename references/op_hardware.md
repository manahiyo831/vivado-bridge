# hardware operations

All operations are invoked via the `vivado_op.py` JSON dispatcher.
See [SKILL.md](../SKILL.md) for the invocation pattern.

Hardware Manager + JTAG programming. These do *not* drive boundary-scan
pins directly -- that's deliberately out of scope (see the project
README for why).

## Common shape

All operations return a dict with `success`, `error_kind`, `message`,
`warnings`. On `tcl_error` failures the result also carries
`error_info` (Vivado's Tcl stack trace) and `error_code`. Operations
that change device state also return `changed` so callers can tell
"was already in the desired state" from "we just put it there".
Operation-specific fields are listed below.

## Lifecycle: open Hardware Manager mode, connect, open target, select device

The Hardware Manager workflow has four steps and they each map to a
distinct operation. **Operations don't auto-step**: you call them
explicitly, in order. The earlier ones are idempotent and report
`changed=false` when there's nothing to do; the device selection step
is a deliberate per-target choice and stays explicit.

```bash
# 1. HW Manager mode
echo '{"op":"hardware.open_hw_manager","params":{}}' | python vivado_op.py

# 2. connect to hw_server
echo '{"op":"hardware.connect_hw_server","params":{}}' | python vivado_op.py

# 3. open JTAG target
echo '{"op":"hardware.open_hardware_target","params":{}}' | python vivado_op.py

# 4. select device on chain (substring match)
echo '{"op":"hardware.open_hardware_device","params":{"device_filter":"xc7"}}' | python vivado_op.py
```

This four-step shape mirrors the Tcl flow: one cable can carry several
devices (e.g. on Zynq the chain has both `arm_dap_0` and the FPGA),
so target and device are separate concepts. Use
`hardware.list_hw_devices` to see what's on the chain before calling
`hardware.open_hardware_device`.

### hardware.open_hw_manager

Switch Vivado into Hardware Manager mode. Vivado's `open_hw_manager`
is itself idempotent, so this is safe to call any time.

**Request:**
```json
{"op": "hardware.open_hw_manager", "params": {}}
```

**Response:**
```json
{"success": true, "message": "hw_manager open"}
```

### hardware.connect_hw_server

Connect to the local Xilinx `hw_server` (port 3121).

**Request:**
```json
{"op": "hardware.connect_hw_server", "params": {}}
```

**Response (newly connected):**
```json
{"success": true, "server": "localhost:3121", "changed": true}
```

**Response (already connected):**
```json
{"success": true, "server": "localhost:3121", "changed": false,
 "message": "already connected (localhost:3121)"}
```

`connect_hw_server` is *not* idempotent in raw Vivado -- calling it
twice yields "Disconnect server connection ... before making a new
one". This wrapper checks `get_hw_servers` first and skips the call
when something's already connected.

### hardware.open_hardware_target

Open a JTAG target. Requires that the Hardware Manager is open and
the hw_server is connected (see above). This op opens the target
only -- it does not pick a device on the chain. Call
`hardware.list_hw_devices` and then `hardware.open_hardware_device`
for that.

**Request:**
```json
{"op": "hardware.open_hardware_target", "params": {}}
```

**Response:**
```json
{"success": true,
 "target": "localhost:3121/xilinx_tcf/Digilent/003017ABD79AA",
 "targets_available": ["..."],
 "changed": true}
```

If multiple cables are connected, pass `target_filter: "<substring>"`
to disambiguate. With one cable you can leave it null.

`force_refresh: true` closes and reopens the target so Vivado re-scans
the JTAG chain. Use this when an earlier session left a stale
"target open / chain empty" state -- the cheap fast path in default
mode (`changed=false`) won't notice that situation, because from
Vivado's viewpoint the target is already open. Power cycles on the
board are another reason to force a refresh.

After power-cycling the board, or whenever `hardware.list_hw_devices`
comes back empty:

**Request:**
```json
{"op": "hardware.open_hardware_target", "params": {"force_refresh": true}}
```

**Response:**
```json
{"success": true, "changed": true,
 "message": "opened ...:Digilent/... (refreshed)"}
```

#### Failure modes

| `error_kind` | When |
|---|---|
| `not_found` | No targets visible at all, or no target matched the filter. |
| `ambiguous` | Multiple targets present and no `target_filter` was given. |
| `tcl_error` | Vivado refused (cable busy, no permission, ...). See `error_info`. |

### hardware.list_hw_devices

Report the hw_devices visible under the currently open target.

**Request:**
```json
{"op": "hardware.list_hw_devices", "params": {}}
```

**Response:**
```json
{"success": true, "devices": ["arm_dap_0", "xc7z020_1"],
 "message": "2 device(s): arm_dap_0 xc7z020_1"}
```

An empty list comes back as `success=true` with `devices=[]` and a
message pointing at `hardware.open_hardware_target` with
`force_refresh: true`. That empty state is almost always a stale
Vivado-side cache from an earlier session, not a hardware problem --
try the refresh first before suspecting cable, power, or mode-pin
issues.

#### Failure modes

| `error_kind` | When |
|---|---|
| `not_found` | No hw_target is open. Call `hardware.open_hardware_target` first. |

### hardware.open_hardware_device

Make a specific hw_device the `current_hw_device` for the next
`program_device` / `refresh` call. Wraps
`current_hw_device [get_hw_devices ...]`.

**Request:**
```json
{"op": "hardware.open_hardware_device", "params": {"device_filter": "xc7"}}
```

**Response:**
```json
{"success": true,
 "device": "xc7z020_1",
 "devices_available": ["arm_dap_0", "xc7z020_1"]}
```

`device_filter` is a substring match against `get_hw_devices`. With
one device on the chain you can leave it null and the op picks that
single device. With multiple devices and no filter you get
`ambiguous` -- the op deliberately won't guess between e.g. an ARM
DAP and the FPGA.

#### Failure modes

| `error_kind` | When |
|---|---|
| `not_found` | No hw_target open, the chain is empty, or no device matched the filter. |
| `ambiguous` | Multiple devices on the chain and no `device_filter` was given. |
| `tcl_error` | Vivado refused the selection. See `error_info`. |

### hardware.close_hardware_target

**Request:**
```json
{"op": "hardware.close_hardware_target", "params": {}}
```

**Response (closed):**
```json
{"success": true, "changed": true}
```

**Response (nothing was open):**
```json
{"success": true, "changed": false,
 "message": "no hw_target open"}
```

### hardware.get_hardware_status

Snapshot of where the Hardware Manager is right now.

**Request:**
```json
{"op": "hardware.get_hardware_status", "params": {}}
```

**Response:**
```json
{"success": true,
 "connected": true, "server": "localhost:3121",
 "target": "...", "device": "xc7z020_1", "part": "xc7z020",
 "is_programmed": true,
 "program_file": ".../blink.bit",
 "vios": ["hw_vio_1"], "ilas": []}
```

`is_programmed` reads the FPGA's DONE pin via
`REGISTER.CONFIG_STATUS.BIT14_DONE_PIN`. On 7-series parts this
reflects "bitstream loaded"; on parts that don't expose this register
the field comes back as null ("unknown"). One thing to be aware of:
`is_programmed=true` can be left over from a previous Vivado session
since the FPGA's DONE pin stays high until power-cycle or reprogram.
If you're confirming "did *this* session program the part?", combine
the field with `program_file` (next paragraph) -- that's per-session
state.

`program_file` is the last bit set with
`set_property PROGRAM.FILE [...]` -- it's what `program_hw_devices`
will use, not necessarily what is currently loaded.

## Programming

### hardware.program_device

Push the project's bitstream onto the open FPGA, attaching the probes
file (`.ltx`) automatically when present so VIOs and ILAs light up
immediately.

**Request:**
```json
{"op": "hardware.program_device", "params": {
  "bit_path": null,
  "ltx_path": null,
  "refresh": true,
  "auto_attach_probes": true
}}
```

In request params, `bit_path: null` means "auto-detect from active impl
run", `ltx_path: null` means "auto-detect alongside the .bit",
`refresh: true` calls `refresh_hw_device` after program, and
`auto_attach_probes: true` sets `PROBES.FILE` before program.

**Response:**
```json
{"success": true,
 "bit_path": ".../blink.bit",
 "ltx_path": ".../blink.ltx",
 "vio_count": 1, "ila_count": 0,
 "warnings": []}
```

#### About `bit_path` and `ltx_path`

They're separate parameters on purpose. The "I forgot to attach probes
and the dashboard is empty" footgun is a classic; making `ltx_path` a
first-class argument means you (or an AI) can't ignore it by accident.

If both are null, the operation finds them automatically from
`build.find_bitstream`. If a `.ltx` is found, it's attached before
programming. If a design has VIO/ILA cores but no `.ltx` is available,
programming still succeeds, but `warnings` will explain that the
dashboard won't show probe names.

#### Failure modes

| `error_kind` | When | Fix |
|---|---|---|
| `not_found` | No `current_hw_device`, or `.bit` not found | Call `hardware.open_hardware_target` first, or run `build.implement`. |
| `tcl_error` | Vivado refused (incompatible part, busy, ...) | Read `error_info`. |

## Typical flow

```bash
# 1. Make sure we have something to program. Check impl status first;
#    if it isn't complete, run build.implement (long-running, prefer
#    wait:false + poll for production agents).
echo '{"op":"build.get_run_status","params":{"kind":"implementation"}}' | python vivado_op.py
# If "is_complete": false:
echo '{"op":"build.implement","params":{"jobs":4}}' | python vivado_op.py

# 2. Open the hardware (four explicit steps).
echo '{"op":"hardware.open_hw_manager","params":{}}' | python vivado_op.py
echo '{"op":"hardware.connect_hw_server","params":{}}' | python vivado_op.py
echo '{"op":"hardware.open_hardware_target","params":{}}' | python vivado_op.py
echo '{"op":"hardware.open_hardware_device","params":{"device_filter":"xc7"}}' | python vivado_op.py

# 3. Program (auto-detects bit + ltx). Inspect "warnings" in the response.
echo '{"op":"hardware.program_device","params":{}}' | python vivado_op.py
```
