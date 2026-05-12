---
name: vivado-bridge
description: Drive Xilinx Vivado from Python via a small Tcl socket bridge. The Tcl side only forwards `exec_tcl`; all logic stays in Python. Use for (1) Hardware Manager / JTAG control, (2) project create/modify, (3) synthesis/implementation automation, (4) any Vivado Tcl automation.
---

# vivado-bridge SKILL

Run Vivado Tcl commands from Python over a localhost socket. The Vivado
side stays a thin Tcl server; everything else lives in Python.

```
Python scripts -> socket (configured port) -> Tcl server -> Vivado
```

## First-time setup

The skill ships with a working `.env` (host=127.0.0.1, port=53729).
That works as-is for a single Vivado on the same machine. Edit `.env`
only if:

- You need a different port (e.g. running a second Vivado in parallel
  and the default is already taken — see "Quick start" for the
  per-instance override pattern).
- You want to listen on a non-loopback address. `127.0.0.1` is the
  recommended default; `0.0.0.0` accepts arbitrary Tcl from anywhere
  reachable, with no authentication.

```
VIVADO_BRIDGE_HOST=127.0.0.1
VIVADO_BRIDGE_PORT=53729
```

## Connect first, then work

Whenever this skill is invoked, **the first thing to do is verify the
bridge is up** -- everything else assumes it is. The bridge runs inside
the user's Vivado, which the skill cannot start on its own (Vivado is
a GUI program the user keeps open across many tasks). So:

1. Run `connection_check.py` by absolute path:
   ```bash
   python <skill-dir>/scripts/connection_check.py
   ```
   - If it prints `OK` with a Vivado version, you're done -- continue.
   - If it prints `Connection refused`, the bridge is not sourced yet.
     Stop and ask the user to paste **one line** into Vivado's Tcl
     Console:
     ```tcl
     source <skill-dir>/vivado_socket_server.tcl
     ```
     Use the absolute path; the user will copy-paste it directly.
     Once they confirm the banner appeared, re-run `connection_check.py`.

Don't attempt other operations before this passes -- they would all
fail with `Connection refused` and clutter the conversation. A single
`connection_check.py` call up front saves several round trips.

## Quick start

1. Start Vivado the way you normally do (with or without a project).
2. In the Vivado Tcl Console, run:
   ```tcl
   source /path/to/vivado-bridge/vivado_socket_server.tcl
   ```
   You should see a banner: `vivado-bridge v0.1.0 started ...`
3. From a separate shell, drive Vivado through the **JSON dispatcher**:
   ```bash
   python scripts/connection_check.py                              # verify bridge identity
   echo '{"op":"project.info"}'         | python scripts/vivado_op.py
   echo '{"op":"build.summary"}'        | python scripts/vivado_op.py
   ```

   `vivado_op.py` is the canonical entry point: one JSON request on
   stdin, one JSON response on stdout. See `--list` for the 45
   registered ops and `--help` for the protocol shape:

   ```bash
   python scripts/vivado_op.py --list      # list all op names
   python scripts/vivado_op.py --help      # protocol / examples
   ```

   For raw Tcl when no op covers what you need, `scripts/exec_tcl.py`
   is the escape hatch:

   ```bash
   python scripts/exec_tcl.py "version -short"
   python scripts/exec_tcl.py "get_property part [current_project]"
   ```

   The CLI scripts locate `.env` and the Tcl server **relative to their
   own file**, not the current working directory, so an absolute path
   works from anywhere:

   ```bash
   python /abs/path/to/vivado-bridge/scripts/vivado_op.py --list
   ```

   Calling them with an absolute path avoids having to `cd` into the
   bridge directory before every invocation.

To run a second Vivado on a different port without editing `.env`:
```tcl
set ::vbridge_port 53730
source /path/to/vivado-bridge/vivado_socket_server.tcl
```

When you do run multiple Vivados side by side, **always set
`VIVADO_BRIDGE_PORT` explicitly** for each Python invocation:
```bash
VIVADO_BRIDGE_PORT=53730 python scripts/connection_check.py
```
Without the env var the client falls back to `.env`'s default
(53729) and may connect to a *different* Vivado instance than you
intended. There is no auto-detection — the client asks the host/port
you give it, period. The same applies to `capture_screenshot.py`,
which uses the bridge's `current_project` to identify which Vivado
window to grab; pointing it at the wrong port screenshots the wrong
GUI.

A second multi-Vivado caveat: by default both Vivados run with the
same cwd (`%APPDATA%/Xilinx/Vivado` on Windows) and write into the
same `vivado.log`. The auto-tail mechanism that surfaces Vivado
warnings into operation `warnings` lists does not know how to
distinguish "my Vivado's lines" from "the other one's", so on a
shared-log setup the warnings field can't be relied on. If this
matters, launch each Vivado from a separate empty cwd so each gets
its own log file.

## Pitfalls AIs hit (read this once before going further)

A handful of Vivado quirks trip up nearly every assistant on first
contact. None of them indicate a real problem with the bridge or the
hardware -- they're just things to know up front.

1. **Raw Tcl through `exec_tcl.py` returns the last statement's value,
   not stdout.** `puts "x=$x"` writes to the Vivado Console but is not
   a return value, so it won't appear in `output`. End your snippet
   with the value as the last expression, or use `set x ...; return $x`.
   Both `return $x` and a bare last expression are treated as success --
   the bridge accepts Tcl return codes 0 (TCL_OK) and 2 (TCL_RETURN)
   alike, so you can write `return $x` from a top-level snippet and
   it lands in stdout with success. Dispatcher ops never hit this
   because they package the return for you; this matters only when
   you drop through to `exec_tcl.py`. (See AI-assistant section §4
   for the safe pattern.)

2. **VIO probe names come from the connected signal, not the IP port.**
   A VIO customized with `probe_in0` will show up as e.g. `toggle`
   in `debug.list_vio_probes` if it was wired to a signal called
   `toggle` in HDL. Always call `debug.list_vio_probes` once at the
   start of the session and use the names that come back -- never
   assume the IP-port names.

   ```bash
   echo '{"op":"debug.list_vio_probes"}' | python <bridge>/scripts/vivado_op.py
   ```

   (See [op_debug.md "Probe names: list before you
   read"](references/op_debug.md#probe-names-list-before-you-read).)

3. **"Target already open" does not mean the JTAG chain was re-scanned.**
   `hardware.open_hardware_target` is idempotent by default -- if
   Vivado already has a target open from a previous session it returns
   `changed: false` and skips the actual open. The cached chain may
   be empty even when the board is fine. If `hardware.list_hw_devices`
   comes back empty, retry with `force_refresh: true`:

   ```bash
   echo '{"op":"hardware.open_hardware_target","params":{"force_refresh":true}}' \
       | python <bridge>/scripts/vivado_op.py
   ```

   ...before suspecting cable, power, or mode-pin issues. (See
   [op_hardware.md](references/op_hardware.md).)

4. **Build to bitstream is one call.** `build.implement` runs
   synth-impl-write_bitstream by default. Pass `generate_bitstream:
   false` to stop after `route_design` when you only want timing
   reports.

   ```bash
   echo '{"op":"build.implement","params":{"generate_bitstream":false}}' \
       | python <bridge>/scripts/vivado_op.py
   ```

   The flag exists; don't go looking for a separate `build.write_bit`.
   (See [op_build.md](references/op_build.md).)

5. **Operation results have op-specific fields only on success.**
   `debug.read_vio_probe` returns `value` only when `success: true`;
   on failure (e.g. `error_kind: "not_found"`) there is no `value`
   field. Always check `.success` (and the dispatcher's exit code)
   before reading op-specific fields. The `success` / `error_kind` /
   `message` / `warnings` keys are always present.

6. **VIO + ILA on the same net needs `mark_debug_valid`, and the
   VIO probe gets renamed.** Two distinct gotchas come up when the
   same Verilog wire feeds both a VIO and an ILA:

   *(a) The ILA's `connect_debug_port` silently binds 0 channels.*
   By default any net attached to a VIO is filtered out of the ILA
   debug graph. `connect_debug_port` returns success, the SKILL's
   net-existence pre-flight passes, and `build.implement` then dies
   in opt_design with `[Chipscope 16-213] probeN has K unconnected
   channels`. Fix: tag the shared wire with **all three** attributes
   in HDL — `(* mark_debug = "true", mark_debug_valid = "true", keep
   = "true" *)`. The `_valid` flag tells Vivado the net is allowed
   on an ILA probe even though it also feeds a VIO. After re-synth
   the bind survives all the way through bitstream. (Full worked
   example in [using_ila.md](references/using_ila.md) §8.5.)

   *(b) The VIO-side runtime probe name gains a suffix.* Even when
   (a) is fixed, Vivado disambiguates the runtime probe names by
   appending `_1` / `_2`. A VIO output `mode_select` becomes
   `mode_select_2` once an ILA also taps `mode_select`. Any caller
   that hardcoded VIO names from a pre-ILA build will fail with
   `error_kind: "not_found"`. Always re-run `debug.list_vio_probes`
   after a fresh `hardware.program_device` and use the names you get
   back.

7. **VIO/ILA design patterns live in topic guides.** The per-module
   reference (`op_debug.md`) covers the Python API surface; the
   *design-time* questions ("how do I add a VIO IP correctly", "what
   trigger value syntax does ILA use", "why is my dashboard empty",
   "can I capture a 1 Hz signal with ILA") are collected in
   [using_vio.md](references/using_vio.md) and
   [using_ila.md](references/using_ila.md). Read those before adding
   debug cores or wiring up the first capture flow -- they exist
   precisely to save you from the pitfalls that come up *during*
   design and bring-up rather than at API call time.

8. **Don't `git add` a Vivado project as-is.** A small Zynq-7
   project with a few synth/impl/sim runs reaches several GB on
   disk -- the bulk lives in `*.sim/`, `*.cache/`, `*.runs/` and
   is regenerable. The supported pattern is "commit the recipe and
   the inputs": run `scripts/export_project.py` to produce a TCL
   that re-creates the project, commit that plus your HDL / XDC /
   `.xci`, and `.gitignore` the rest. Full guidance and a
   ready-to-paste `.gitignore` live in
   [git_management.md](references/git_management.md). When a user
   asks "should I commit this?" about a Vivado project, point them
   there before they accidentally push 6 GB.

9. **Vivado Tcl Console output appears in `warnings` automatically.**
   The dispatcher's response carries a `warnings` array on every op.
   Everything Vivado prints to its Tcl Console -- WARNING / CRITICAL
   WARNING / ERROR *and* `$display`, `puts`, testbench `RESULT:` lines,
   `$finish called at time ...`, and the like -- goes to `vivado.log`,
   not into the dispatcher's main response fields. To make sure
   callers actually see them, the bridge tails `vivado.log` between
   exec_tcl calls and any new lines are merged into the response's
   `warnings` list. **Read `warnings` after every meaningful
   operation** -- a clock-pin XDC mistake or a missing IP surfaces
   there even when `success` is true, and a testbench's own
   `RESULT: PASSED (16/16 checks)` is the only proof you have that
   it actually finished. (`success: true` from `sim.run` only means
   the simulator executed; it does not interpret your testbench's
   pass/fail.)

   The bridge filters out INFO noise, the bridge's own `vbridge 1-*`
   lines, and bare `#`-comment Tcl echoes; everything else passes
   through. Severity-tagged lines (`WARNING:` / `CRITICAL WARNING:`
   / `ERROR:`) are folded by message id (a single root cause that
   Vivado echoes 100 times appears once tagged `(×100 occurrences)`)
   and capped at 50 unique ids per category per operation, with a
   trailing `[bridge] N more unique <CATEGORY> id(s) suppressed ...`
   summary pointing at `bridge.get_vivado_logs` for the raw path.
   Untagged lines (`$display` output, etc.) bypass dedup and the cap
   and pass through verbatim.

   When using `exec_tcl.py` directly, the same console drain is
   exposed via the script's stderr stream, so you can pipe it
   through `grep` or capture it separately from the Tcl return value
   on stdout.

10. **Timeouts are client-side only.** Whether you set one through
    the dispatcher (a helper-specific `timeout` param) or via
    `exec_tcl.py --timeout`, the timeout bounds how long the Python
    client will wait for a reply on the socket. If it fires, the
    response reports `error_kind: "client_error"` with a
    `Timed out after Ns` message, but the Tcl statement Vivado is
    running **keeps running on the Vivado side** — the bridge has no
    way to interrupt it. You'll see the late result land in
    `vivado.log`, but the dispatcher has already moved on. Use
    timeouts as a "give up waiting" valve, not as a "cancel the
    work" signal.

11. **Pass paths to Tcl through forward slashes or `{...}` braces.**
    When you send a Windows-style path through `exec_tcl.py "..."`
    or as a string inside a JSON `params` value, Tcl sees the raw
    string. Backslash sequences like `\v` (vertical tab) or `\n`
    (newline) get interpreted before Tcl, so a path like
    `C:\work\foo` ends up referencing something else entirely.
    Either convert to forward slashes (`C:/work/foo`) or wrap the
    path in Tcl braces when you build the Tcl text (`add_files
    {C:/work/foo}`). Inside JSON itself, JSON's own backslash escape
    rules apply (`"C:\\work\\foo"`), but the result that reaches
    Tcl is still subject to Tcl's interpretation -- forward slashes
    are the safest path representation across both layers.

12. **PowerShell's `>` redirect writes UTF-16, not UTF-8.** Piping
    dispatcher output to a file with `... | python vivado_op.py > out.json`
    on PowerShell produces a UTF-16-LE file with a BOM, which Python's
    `json.load(open(path))` then fails to parse with
    `JSONDecodeError: Expecting value: line 1 column 1 (char 0)`.
    Two safe alternatives on PowerShell:

    ```powershell
    # (a) Capture into a variable, then write as UTF-8 explicitly:
    $out = '{"op":"project.info"}' | python <bridge>/scripts/vivado_op.py
    [System.IO.File]::WriteAllText("out.json", $out, [System.Text.Encoding]::UTF8)

    # (b) Pipe straight into a host-side parser without touching disk:
    $out = '{"op":"project.info"}' | python <bridge>/scripts/vivado_op.py
    $out | ConvertFrom-Json | Format-List
    ```

    Bash / WSL hosts don't have this problem -- `>` writes raw bytes.
    The `>` mistake only bites on native PowerShell.

13. **Op kwarg names come from the helper's Python signature, not
    your memory.** Each op forwards `params` straight to the
    corresponding helper as `**kwargs`, so a typo in a key name comes
    back as `helper_exception` / `TypeError: <op>() got an unexpected
    keyword argument '<wrong-name>'`. The error message names the
    bad key, but it doesn't tell you the correct one. When you hit
    this, check the op's signature in its `references/op_*.md` page
    (the request example in each op section lists the canonical kwargs)
    or `Grep "^def <name>" operations/<module>.py` for the source-of-
    truth signature.

    Concrete recent example: `ila.parse_csv` takes `csv_path`, not
    `path` (despite its sibling `ila.export_csv` taking `path`). The
    canonical answer is always the helper's Python signature in
    `operations/<module>.py`; the op_*.md examples should match it
    but treat the source as the tiebreaker.

## Anti-pattern checklist (consult at key checkpoints)

Real bring-up surfaces a body of recurring failure patterns
(testbench races, ILA/VIO pitfalls the SKILL helpers cannot prevent
on their own, AXI-Stream gotchas, etc.). They are catalogued in
[anti_patterns/anti_patterns.md](anti_patterns/anti_patterns.md) by
category: `VERILOG-TB-*`, `VIVADO-ILA-*`, `VIVADO-VIO-*`,
`VIVADO-XSIM-*`, `VIVADO-XDC-*`, `VIVADO-AXIS-*`, `BRIDGE-*`.

The file lists only traps the SKILL helpers cannot paper over —
failure modes already prevented by the helpers are intentionally not
catalogued; the SKILL itself is the correct approach for those.

This file is meant to be **consulted at specific checkpoints during
a project**, not read end-to-end up front. The intended flow:

| Checkpoint | Categories to scan |
|---|---|
| Right after writing HDL (RTL or testbench) | `VERILOG-TB-*`, `VIVADO-XSIM-*` |
| Wrapping any IP that exposes AXI-Stream ports | `VIVADO-AXIS-*` |
| Before kicking off `build.synthesize` | `VIVADO-XDC-*`, `VIVADO-AXIS-*` |
| After synth completes (especially before adding ILA) | `VIVADO-VIO-*`, `VIVADO-XDC-*` |
| Adding ILA / VIO cores | `VIVADO-ILA-*`, `VIVADO-VIO-*` |
| `program_device` returned but device behaves wrong | `VIVADO-ILA-*`, `VIVADO-VIO-*`, `VIVADO-XSIM-*` |
| Bridge / exec_tcl behaviour surprises you | `BRIDGE-*` (and pull `vivado.log` / `vivado.jou` via `bridge.get_vivado_logs` for the full Tcl-Console transcript) |

For each entry the file lists the symptom and the workaround.

If you (the agent) discover a NEW failure pattern that isn't yet
captured, **do not silently work around it** -- surface it in your
final report so the lead can decide whether to add it to the
catalogue. Entries are added by human triage, not by the agent that
hit the trap; this protects against false generalisations from
n=1 observations.

## What the dispatcher looks like

The skill exposes **one canonical entry point**: `scripts/vivado_op.py`.
Drive every supported operation through it -- a JSON request on stdin,
a JSON response on stdout. The 45 ops cover project metadata, synth /
impl, Hardware Manager, VIO and ILA, simulation, and log inspection.

```bash
# Quick look at the open project + build state
echo '{"op":"project.info"}'  | python <bridge>/scripts/vivado_op.py
echo '{"op":"build.summary"}' | python <bridge>/scripts/vivado_op.py

# Branch on the response in your agent loop: build.summary returns
# impl_complete / bit_exists / ready_to_program / met_timing as
# top-level booleans, so the agent can decide whether to launch
# implement before programming.
echo '{"op":"build.implement","params":{"jobs":4}}' | python <bridge>/scripts/vivado_op.py

# Hardware Manager workflow -- one dispatcher call per step
echo '{"op":"hardware.open_hw_manager"}'        | python <bridge>/scripts/vivado_op.py
echo '{"op":"hardware.connect_hw_server"}'      | python <bridge>/scripts/vivado_op.py
echo '{"op":"hardware.open_hardware_target"}'   | python <bridge>/scripts/vivado_op.py
echo '{"op":"hardware.open_hardware_device","params":{"device_filter":"xc7"}}' \
    | python <bridge>/scripts/vivado_op.py    # pick FPGA, not arm_dap

echo '{"op":"hardware.program_device"}' | python <bridge>/scripts/vivado_op.py
# response includes bit_path, vio_count, ltx_path, warnings

echo '{"op":"debug.read_vio_probe","params":{"probe":"probe_led"}}' \
    | python <bridge>/scripts/vivado_op.py
# response.value -> '0' or '1' (string; radix-aware)
```

Every operation returns a dict with the same shape:

| Field | Meaning |
|---|---|
| `success` (bool) | Did the operation finish without errors? |
| `error_kind` (str or None) | One of `not_found`, `tcl_error`, `no_device`, `blocked_command`, ... |
| `message` (str) | One-line summary, safe to print. |
| `warnings` (list of str) | Non-fatal notes (e.g. "design has VIO but no .ltx"). |

### Op-specific fields: success vs failure policy

Operation-specific fields (e.g. `value` from `read_vio_probe`,
`bit_path` from `program_device`, `csv_path` from `ila.export_csv`)
follow a deliberate split:

- **Result-of-the-operation fields are present only on success.**
  Things like `value`, `int_value`, `bit_path`, the parsed `rows`
  list -- these only exist when there's a real value to put there.
  Reading them on a failure result is a bug; branch on `.success`
  before reading any op-specific field. The dispatcher's exit code
  (0 on success, 1 otherwise) mirrors the same signal.
- **Identity fields can also appear on failure.** A failure result
  may carry the parameters needed to *locate* the failure -- the
  probe name, the ILA name, the target output path -- so the caller
  doesn't need to remember which operation it was inspecting. These
  are minimal and tagged "where", not "what". Examples:
  `failed_probe` / `staged_before_failure` (`write_vio_probes`),
  `csv_path` / `ila` (`ila.export_csv`).

If you're adding a new op-specific field, ask: *is this what the
operation produced (success-only) or where the operation was acting
(may stay on failure)?* Don't pad failure dicts with placeholder
values -- they reintroduce the silent-fallback class of bugs the
rest of the bridge is built to avoid.

Operations target the project's *active* runs (`current_run -synthesis`
/ `current_run -implementation`). They don't add or switch runs.

See the per-module reference for full details. Each function below
links to its anchor in the corresponding `references/op_*.md`.

> **Looking up operation details**: Use `Grep "### <module>.<name>" references/op_<module>.md -A 25` to retrieve only the specific section instead of reading the entire file. This keeps token usage small when you already know which op you need. Use `Grep "^### " references/op_<module>.md` to see all ops in a module. Or enumerate everything in one shot with `python scripts/vivado_op.py --list`.

### `project.*` ops -- [op_project.md](references/op_project.md)

Read-only project metadata snapshot.

| Function | What it does |
|---|---|
| [`info`](references/op_project.md#projectinfo) | **Snapshot.** name / part / board_part / top / sources / runs in one round trip. |

### `build.*` ops -- [op_build.md](references/op_build.md)

Synth, impl, polling, run logs.

| Function | What it does |
|---|---|
| [`summary`](references/op_build.md#buildsummary) | **Snapshot.** synth/impl status, bit_path, WNS/TNS, ready_to_program. |
| [`get_active_runs`](references/op_build.md#buildget_active_runs) | Active synth/impl run names (lighter than `summary`). |
| [`get_run_status`](references/op_build.md#buildget_run_status) | STATUS / PROGRESS / NEEDS_REFRESH for a specific run. |
| [`wait_for_run`](references/op_build.md#buildwait_for_run) | Block-and-poll until run completes / fails / times out. |
| [`synthesize`](references/op_build.md#buildsynthesize) | reset_run + launch_runs synth + auto-OOC IP synth. |
| [`synthesize_ip`](references/op_build.md#buildsynthesize_ip) | OOC synthesize one IP. |
| [`implement`](references/op_build.md#buildimplement) | reset_run + launch_runs impl + write_bitstream. |
| [`get_run_log_path`](references/op_build.md#buildget_run_log_path) | Resolve `runme.log` path. |
| [`get_run_diagnostics`](references/op_build.md#buildget_run_diagnostics) | Count + preview ERROR / CRITICAL WARNING / WARNING. |
| [`find_bitstream`](references/op_build.md#buildfind_bitstream) | Locate freshest `.bit` and `.ltx` for active impl run. |
| [`open_synth`](references/op_build.md#buildopen_synth) | open_run on active synth (needed before `create_debug_core`). |
| [`close_design`](references/op_build.md#buildclose_design) | close_design (pair with `open_synth`). |

### `hardware.*` ops -- [op_hardware.md](references/op_hardware.md)

Hardware Manager lifecycle, device selection, programming.

| Function | What it does |
|---|---|
| [`open_hw_manager`](references/op_hardware.md#hardwareopen_hw_manager) | Switch Vivado into Hardware Manager mode. |
| [`connect_hw_server`](references/op_hardware.md#hardwareconnect_hw_server) | Connect to local hw_server (idempotent). |
| [`open_hardware_target`](references/op_hardware.md#hardwareopen_hardware_target) | Open the JTAG target; pass `force_refresh=True` to re-scan a stale chain. |
| [`list_hw_devices`](references/op_hardware.md#hardwarelist_hw_devices) | Enumerate detected devices on the open target. |
| [`open_hardware_device`](references/op_hardware.md#hardwareopen_hardware_device) | Pick the FPGA (e.g. `device_filter="xc7"`), not arm_dap. |
| [`close_hardware_target`](references/op_hardware.md#hardwareclose_hardware_target) | Release the JTAG target. |
| [`get_hardware_status`](references/op_hardware.md#hardwareget_hardware_status) | **Snapshot.** server / target / device / DONE / VIO+ILA core counts. |
| [`program_device`](references/op_hardware.md#hardwareprogram_device) | Auto-detect `.bit` + `.ltx`, program, refresh_hw_device. |

### `debug.*` ops -- [op_debug.md](references/op_debug.md)

VIO IP customisation, VIO probe read/write, ILA core create/delete.

| Function | What it does |
|---|---|
| [`list_vios`](references/op_debug.md#debuglist_vios) | Enumerate VIO cores on the programmed device. |
| [`list_vio_probes`](references/op_debug.md#debuglist_vio_probes) | List runtime probe names per VIO (call after every `program_device`). |
| [`read_vio_probe`](references/op_debug.md#debugread_vio_probe) | Read one probe; radix-aware (`bin` / `hex` / `unsigned` / `signed`). |
| [`read_vio_probes_all`](references/op_debug.md#debugread_vio_probes_all) | Read every probe on a VIO in one call. |
| [`write_vio_probe`](references/op_debug.md#debugwrite_vio_probe) | Write one probe; radix-aware, with width validation. |
| [`write_vio_probes`](references/op_debug.md#debugwrite_vio_probes) | Atomically stage + commit multiple probe writes (no half-state on failure). |
| [`create_vio`](references/op_debug.md#debugcreate_vio) | Build-time helper: customise a VIO IP from a probe spec (replaces ~25 lines of raw Tcl). |
| [`create_ila_core`](references/op_debug.md#debugcreate_ila_core) | Build-time helper: insert `xil_defaultlib_ila` debug core into a synthesized design. |
| [`delete_ila_core`](references/op_debug.md#debugdelete_ila_core) | Build-time helper: remove a previously created ILA core. |

### `ila.*` ops -- [op_ila.md](references/op_ila.md)

ILA capture flow: configure, trigger, arm, wait, export, parse.

| Function | What it does |
|---|---|
| [`list_ilas`](references/op_ila.md#ilalist_ilas) | Enumerate ILA cores on the programmed device. |
| [`list_ila_probes`](references/op_ila.md#ilalist_ila_probes) | List runtime probe names per ILA. |
| [`configure`](references/op_ila.md#ilaconfigure) | Set capture depth, trigger position, capture-control mode. |
| [`set_triggers`](references/op_ila.md#ilaset_triggers) | Stage trigger compares (`"rising"` / `"falling"` / value+operator). |
| [`arm`](references/op_ila.md#ilaarm) | `run_hw_ila` to arm the core (non-blocking). |
| [`wait_for_capture`](references/op_ila.md#ilawait_for_capture) | Poll until trigger fires or timeout. |
| [`get_status`](references/op_ila.md#ilaget_status) | Current armed / captured / sample count state. |
| [`export_csv`](references/op_ila.md#ilaexport_csv) | Upload the captured buffer and write a CSV. |
| [`parse_csv`](references/op_ila.md#ilaparse_csv-host-side) | Host-side CSV decoder (hex/bin, sign-extend, no Vivado needed). |
| [`find_column`](references/op_ila.md#ilafind_column-host-side) | Look up a `parse_csv` column by base name (handles `[31:0]` suffix). |

### `sim.*` ops -- [op_sim.md](references/op_sim.md)

xsim driver: single-shot `run` with `sim_time_us` cap, pre-flight check. Pair with [using_simulation.md](references/using_simulation.md) for testbench design rules.

| Function | What it does |
|---|---|
| [`get_sim_status`](references/op_sim.md#simget_sim_status) | Is a sim already open? which testbench? (pre-flight check before `run`) |
| [`close_sim`](references/op_sim.md#simclose_sim) | `close_sim` (idempotent). |
| [`run`](references/op_sim.md#simrun-the-main-entry-point) | Launch xsim with a `sim_time_us` wall-clock cap; the main entry point. |
| [`summary`](references/op_sim.md#simsummary) | Post-run summary: sim_time_ns reached, $finish detected, RESULT lines. |

### `bridge.*` ops -- [op_bridge.md](references/op_bridge.md)

Bridge / log-file inspection.

| Function | What it does |
|---|---|
| [`get_vivado_logs`](references/op_bridge.md#bridgeget_vivado_logs) | Locate `vivado.log` / `vivado.jou` (Tcl Console transcript) for the running Vivado. |

Topic guides (read before designing in debug cores, not just before
calling the API):

| Topic | Guide |
|---|---|
| Adding a VIO core, naming, radix, loopback verification, GUI dashboard limits | [using_vio.md](references/using_vio.md) |
| `mark_debug` flow, ILA core insertion, trigger syntax, capture/CSV, VIO+ILA naming collision | [using_ila.md](references/using_ila.md) |
| Auto-populating VIO Dashboard / ILA Waveform pane by editing `hw.xml` + `.wcfg` (best-effort) | [using_dashboard_hack.md](references/using_dashboard_hack.md) |
| Putting a Vivado project under git without dragging in the multi-GB generated state | [git_management.md](references/git_management.md) |

### Escape hatch: raw Tcl via `exec_tcl.py`

When no op covers what you need (a quick `report_*` query, a `get_*`
attribute lookup the dispatcher doesn't wrap, a one-off
`set_property`), drop through to raw Tcl with `scripts/exec_tcl.py`:

```bash
python <bridge>/scripts/exec_tcl.py "pwd"
python <bridge>/scripts/exec_tcl.py "get_property part [current_project]"
python <bridge>/scripts/exec_tcl.py "report_clocks"
```

stdout is Vivado's return value (the `output` of `uplevel #0` -- see
"4. Show the response" below for what that means). Stderr carries
errors with a structured `error_kind` (`tcl_error` / `blocked_command`
/ `protocol_error` / `unknown_command` / `client_error` /
`identity_error`), `error_info`, and `error_code`. Exit code is 0 on
success, non-zero on error.

The escape hatch is the right tool when:

- you need a one-shot Tcl primitive the dispatcher doesn't expose
  (e.g. `report_utilization`, `get_property` on an obscure object)
- you're debugging the bridge itself and want to see exactly what
  Vivado returns for a single Tcl line
- you're following a Tcl recipe verbatim from UG835 / a user-pasted
  snippet

Prefer the dispatcher for anything supported there -- the ops do
extra validation, cleanup, and structured error reporting that raw
Tcl doesn't. The escape hatch deliberately doesn't try to replace
the dispatcher; it just lets you reach Tcl when nothing else fits.

## Included CLI scripts

| Script | What it does |
|---|---|
| `scripts/vivado_op.py` | **Primary entry point.** JSON dispatcher: stdin `{"op":"...","params":{...}}` → stdout JSON response. `--list` to enumerate ops, `--help` for protocol. Exits 0 on `success: true`, 1 otherwise. |
| `scripts/exec_tcl.py "TCL"` | Raw Tcl escape hatch. Run one Tcl snippet for cases the dispatcher doesn't cover. stdout = output, stderr = structured error. |
| `scripts/connection_check.py` | Verify bridge identity, show Vivado version + Tcl pwd. |
| `scripts/reload_server.py` | Reload the Tcl bridge in-place (after editing the .tcl file). |
| `scripts/capture_screenshot.py [path]` | Win32-only: grab Vivado main window via `PrintWindow + PW_RENDERFULLCONTENT`. Identifies the right HWND by querying Vivado for `current_project` and matching the title (so it works on multi-monitor setups, with the window occluded, and ignores other apps that happen to have "Vivado" in their title). No arg = overwrite default `screenshots/vivado_screenshot.png`; pass a path to keep a specific shot. |
| `scripts/export_project.py <output_dir>` | Drive `write_project_tcl` + `write_bd_tcl` through the bridge so you can commit a re-creation recipe instead of the project's generated state. **The output directory must already exist** — the script returns `not_found` rather than creating it on the fly, so you can't accidentally place the recreation recipe somewhere unintended. See `references/git_management.md`. |

`vivado_op.py` is what you'll use for nearly everything; the others
are situational. Discover the dispatcher's surface area with
`python scripts/vivado_op.py --list`.

## What the bridge protects against

The Tcl server refuses certain commands so a stray Claude-generated Tcl
snippet can't trash the host:

- `exec` (any OS process invocation) -- always blocked.
- `file delete` / `file rename` / `file copy` / `file mkdir` /
  `file attributes` / `file link` / `file tempfile` -- blocked.
- Read-only `file` subcommands (`exists`, `dirname`, `join`, `mtime`,
  ...) are still allowed because Vivado scripts use them.

When blocked, you get a clear `error_kind: "blocked_command"` response
naming the offending token. Do file/process work via the Claude Code
host tools (Bash/PowerShell/Edit/Write), not through Tcl.

This is an oops-guard, not a sandbox -- a determined Tcl snippet can
work around it. The bridge is meant for local development only.

## Operational notes

- **Always run `version -short` early** to confirm which Vivado you're
  driving. Tcl APIs differ across Vivado versions.
- **Don't use `wait_on_run`**. It blocks Vivado's main thread, which
  blocks the bridge until the run completes. Use `wait_for_run.py`
  (polling) instead.
- **Long Vivado operations block the GUI**. The bridge can't change
  this -- Vivado's Tcl interpreter is single-threaded. Fire-and-poll
  patterns (launch + poll status) are the right shape.
- **Tcl Console logs**. The server logs via `send_msg_id`, which means
  every connect / disconnect / exec_tcl shows up in the Console as
  `INFO: [vivado-bridge 1-1] ...` even when called from event-loop
  callbacks.

## Files

```
vivado-bridge/
├── SKILL.md                       (this file)
├── README.md / README.ja.md       (OSS-facing intro)
├── LICENSE                        (MIT)
├── .env                           (host/port config; ships with sane defaults)
├── vivado_socket_server.tcl       (the Tcl server)
├── operations/                    (high-level Python verbs)
│   ├── project.py                 (read-only project metadata snapshot)
│   ├── build.py                   (synth / impl / wait / log lookups, WNS/TNS)
│   ├── hardware.py                (HW Manager, program_device)
│   ├── debug.py                   (VIO read/write radix-aware)
│   ├── ila.py                     (ILA capture: configure/arm/wait/CSV/parse)
│   ├── sim.py                     (xsim driver: single-shot, sim_time_us cap)
│   └── bridge.py                  (locate vivado.log / vivado.jou)
├── scripts/
│   ├── vivado_op.py               (JSON dispatcher — primary entry point)
│   ├── exec_tcl.py                (raw Tcl CLI escape hatch)
│   ├── vivado_bridge_client.py    (shared Python client, used by all scripts)
│   ├── connection_check.py
│   ├── reload_server.py
│   ├── capture_screenshot.py
│   ├── setup_dashboard.py         (hw.xml + wcfg dashboard editor)
│   ├── dashboard_layout.example.json
│   └── export_project.py          (write_project_tcl + write_bd_tcl)
└── references/
    ├── op_project.md              (project metadata reference)
    ├── op_build.md                (build operations reference)
    ├── op_hardware.md             (hardware operations reference)
    ├── op_debug.md                (debug operations reference -- VIO)
    ├── op_ila.md                  (ILA capture operations reference)
    ├── op_sim.md                  (sim operations reference)
    ├── op_bridge.md               (bridge / log-file reference)
    ├── using_vio.md               (topic guide: VIO design patterns)
    ├── using_ila.md               (topic guide: ILA capture flow)
    ├── using_simulation.md        (topic guide: xsim testbench patterns)
    ├── using_dashboard_hack.md    (topic guide: dashboard hw.xml/wcfg edit)
    └── git_management.md          (topic guide: project git management)
```

## Requirements

- Xilinx Vivado (developed against 2024.1; older versions likely work
  if their bundled tcllib provides `package require json`).
- Python 3.9+ (standard library only -- no extra dependencies for the
  bridge itself; `capture_screenshot.py` needs `pywin32`).
- A free TCP port on the loopback interface.

## Troubleshooting

| Symptom | Most likely cause |
|---|---|
| `Connection refused` | Vivado isn't running, or you haven't sourced the Tcl server yet. |
| `[identity_error]` from `connection_check.py` | Some other app is on the configured port. Pick a different `VIVADO_BRIDGE_PORT`. |
| `[blocked_command] Command 'exec' is blocked` | The Tcl you tried to run uses `exec`; do it on the host instead. |
| `[tcl_error] invalid command name ...` | The Vivado Tcl command doesn't exist (typo, or wrong Vivado version). |
| Bridge call returned empty `output` even though my Tcl ran successfully | Did you `puts` the value? `output` is the return of the last Tcl statement, not stdout -- use `set x ...; return $x` or end the snippet with the value as the last expression. |
| Bridge works but `puts` from your Tcl doesn't show in the Console | `puts` from event-loop callbacks is silently dropped by Vivado. Use `send_msg_id` for logging. |

## Working with this skill from an AI coding assistant

When an AI assistant (Claude Code or similar) drives vivado-bridge on
the user's behalf, the user experience is much better if the assistant
follows a few habits. These are the default expectations for *this*
skill -- not generic advice.

### 1. Confirm the bridge is up, then probe Vivado state

The very first action in any session is `connection_check.py` (see
"Connect first, then work" above). If it fails, ask the user to source
the server and stop -- everything else is wasted effort against a
dead socket.

Once connected, Vivado still has implicit state the user may have set
up by hand (open project, current_hw_target, etc.). Don't ask the
user to describe it -- read it via the bridge. Two aggregate snapshot
ops cover the common cases:

```bash
echo '{"op":"project.info"}'             | python <bridge>/scripts/vivado_op.py
# response.message -> "project_1: part=xc7z020clg400-1, board_part=<unset>, top=led_blink, sources=2, runs=4"

echo '{"op":"hardware.get_hardware_status"}' | python <bridge>/scripts/vivado_op.py
# response.message -> "server connected, target xilinx_tcf/..., device xc7z020, programmed=True, 1 VIO, 0 ILA"
```

`project.info` returns the project's static metadata in one round
trip; `hardware.get_hardware_status` does the same for the Hardware
Manager (target, device, whether the FPGA's DONE pin is high, VIO /
ILA core counts). Together they replace what would otherwise be five
or six separate `exec_tcl` calls and give you a one-line summary the
user can confirm at a glance.

Pay attention to `project.info`'s `part` and `board_part`: a wrong
part (typed by the user when creating the project) is the kind of
mistake the bridge cannot detect for you, but the snapshot makes it
visible immediately so you can ask before doing minutes of synth on
the wrong device.

Lead with a one-line summary of what you found. The user reads it,
confirms or corrects in seconds, and you're aligned.

### 2. Plan -> propose -> execute

Don't fire off a chain of Tcl commands and hope. After probing, write
out the steps you intend to run -- and what you will *not* run -- and
ask. Pattern:

> I see project_1 is open and HW Manager is closed. I'd like to:
>   1. open_hw_manager
>   2. connect_hw_server
>   3. open_hw_target (read-only, no -jtag_mode)
>   4. report PART and IDCODE for the detected device
>
> I will not program the device or modify any sources. OK to proceed?

This is especially important when:

- the operation has hardware side-effects (`program_hw_devices`,
  `boot_hw_device`, anything that flashes or resets the board)
- the operation modifies the user's project (`add_files`,
  `remove_files`, `import_files`, `delete_files`)
- the operation runs for a long time (`launch_runs`, `synth_design`,
  `place_design`, `route_design`) -- the Tcl Console will be blocked
  while it runs

### 3. Read-only first, then narrow

For exploratory tasks, prefer read-only Tcl (`get_*`, `report_*`,
`current_*`, `version`, `pwd`, `info commands ...`). Only after you've
confirmed what's there should you propose anything that writes.

### 4. Show the response, not just "OK"

Vivado's Tcl returns are often empty strings on success. That's not
the same as nothing useful happening. Echo `output` back when
non-empty, and on errors echo `error_kind` and the first useful line
of `error_info` so the user can see exactly what Vivado said.

`output` is the **return value of the last Tcl statement** -- the
result of `uplevel #0 $tcl_code`. `puts "x=$x"` writes to Vivado's
stdout (visible in the Tcl Console) but is not a return value, so it
will not appear in `output`. To send a value back through the bridge,
end your snippet with the value as the last expression -- e.g.
`set x [...]; return $x` or simply leave the desired value as the
last statement. `puts` is for logging into the Console, not for
talking to the bridge.

#### Guard on `success` before reading op-specific fields

Every operation result has the four common keys (`success`,
`error_kind`, `message`, `warnings`). Op-specific fields like
`value` (debug.read_vio_probe), `bit_path` (hardware.program_device),
or `runs` (project.info) are **only present when `success` is
true** -- a failed call won't have them. The dispatcher also exits
non-zero on failure, so the two are kept in sync.

The pattern that survives all cases (illustrated with `jq` for the
JSON inspection step):

```bash
echo '{"op":"debug.read_vio_probe","params":{"probe":"toggle"}}' \
    | python <bridge>/scripts/vivado_op.py > result.json

# Inspect: did it succeed?
jq -r '.success' result.json     # true / false

# If false, the agent should surface .message and stop:
jq -r 'if .success then .value else .message end' result.json
```

In agent code: branch on `.success`. If true, read `.value` (or
whichever op-specific field). If false, read `.message` and decide
whether to retry or surface the error to the user. Don't read
op-specific fields on a failure response -- they won't be there.

#### When Tcl can't see it, take a screenshot

Tcl gives you the *model* state -- which project is open, what the
runs say, which probes are visible. There are situations where the
model says one thing and the GUI shows another:

- a modal dialog is sitting on top of the IDE waiting for a click
  (Vivado will block further GUI updates until it's dismissed, even
  though Tcl reports happily on the underlying state)
- you ran `current_project new` but a project upgrade prompt
  intercepted it, and the open project didn't actually change
- a refresh hasn't reached the dashboard yet, so what the user sees
  on screen disagrees with what the bridge just reported
- you "switched" something (top module, current_run, view tab) and
  want to verify the GUI actually reflects it

In any of these cases, fall back to `scripts/capture_screenshot.py`
to capture the Vivado main window as a PNG and inspect it directly:

```bash
# Default: overwrite a single rolling file at
# <project_root>/screenshots/vivado_screenshot.png. Use this for
# routine sanity checks -- it never accumulates files on disk.
python <bridge>/scripts/capture_screenshot.py

# Only when you actually want a snapshot kept (e.g. before/after a
# specific operation, or as evidence to attach to a report), pass
# an explicit path so it doesn't get overwritten by the next call:
python <bridge>/scripts/capture_screenshot.py results/before_program.png
```

Default to the no-argument form. Naming every shot with a timestamp
or counter just to "be safe" leaves a screenshot graveyard the user
has to clean up later. If a default-path PNG happens to be useful
later, copy it out with a real name *then* -- don't pre-emptively
keep every capture.

This is one of the few cases where reading bytes off disk is
genuinely the right answer -- Tcl simply doesn't expose dialog
state, modal popups, or "did the GUI redraw" the same way it
exposes project/run/probe state. The script is Windows-only and
needs `pywin32` + `Pillow`; treat it as an optional but valuable
sanity check. (See README.md "Optional dependencies" for setup.)

### 5. Respect the bridge's blocks

If you get `error_kind: "blocked_command"` for `exec` or
`file delete` etc., don't try to work around it inside Tcl. Switch to
the host -- run the equivalent shell command, edit the file, etc. via
the surrounding tooling. The block exists precisely so a stray
generated Tcl can't trash the user's filesystem.

### 6. Use reload_server.py during bridge development

If you (the assistant) edit `vivado_socket_server.tcl`, run
`python <bridge>/scripts/reload_server.py` to apply the change. Don't
ask the user to switch to the Tcl Console for this -- the whole point
is to keep the iteration loop in Python.

### 6.5. Call the bridge scripts by absolute path

The CLI scripts resolve `.env` and the Tcl server relative to their own
file, so an absolute path works from any working directory:

```bash
python /abs/path/to/vivado-bridge/scripts/exec_tcl.py "..."
python /abs/path/to/vivado-bridge/scripts/reload_server.py
```

Don't `cd` into the bridge directory before each invocation -- it
clutters the conversation and makes long sessions tedious. Pick the
absolute path once and reuse it.

### 7. Don't auto-cd

The bridge intentionally doesn't change the working directory on
source. If you need a different `pwd`, run `cd ...` explicitly via
exec_tcl and announce it in the chat. Tcl users have strong
expectations about pwd; surprising them is worse than one extra line.

### 8. Never reach for raw boundary-scan / JTAG pin control

vivado-bridge deliberately does not include `-jtag_mode true`,
`scan_ir_hw_jtag`, `scan_dr_hw_jtag`, or any other primitive that
drives FPGA pins directly outside the user's synthesized design.
Those operations bypass the design's drive-strength and direction
constraints; a wrong toggle can damage the board or whatever it's
wired to.

If a user asks for pin-level control, do not work around this by
sending the raw Tcl through `exec_tcl`. Tell them why it's out of
scope and point them at building a board-aware layer on top of
vivado-bridge instead.

### 9. Note the project's board, not just its part

`project.info()` returns both `part` and `board_part`. They answer
different questions:

- `part` is the silicon (e.g. `xc7z020clg400-1`). Always present.
- `board_part` is the board file mapping (e.g.
  `digilentinc.com:pynq-z1:part0:1.1`). Often `None`, and that's
  fine for purely-RTL designs that just toggle pins.

For anything that touches the PS, DDR, Ethernet, or other board-level
peripherals, the design needs `board_part` set so Vivado knows the
pin maps and IP defaults. Reaching `set_property part xc7z020...` on
its own does *not* fix this -- you'll silently get unconnected pins
or wrong defaults.

If `project.info()` shows `board_part = None` and the user is asking
for anything beyond pure RTL, mention it before diving in. Do not run
`set_property board_part xilinx.com:pynq-z1:...` on your own without
asking; the right value depends on which board the user actually
has, and getting it wrong wastes a build.

## See also

- Xilinx UG835 -- Vivado Design Suite Tcl Command Reference. The most
  reliable source for any Tcl command not covered by a dispatcher op;
  pair it with `scripts/exec_tcl.py` to drop through to raw Tcl when
  needed.
