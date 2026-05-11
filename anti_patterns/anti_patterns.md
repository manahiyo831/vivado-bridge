# Vivado / vivado-bridge anti-patterns

A catalogue of failure modes confirmed in development, together with
the corresponding correct approach. Use this as a checklist consulted
at specific checkpoints (see SKILL.md "Anti-pattern checklist") rather
than as a document to read end-to-end.

> **Guard rail**: every entry here was added by **human triage**, not
> by an agent that hit the trap. Agents that suspect a new pattern
> should surface it in their final report so a human can decide
> whether to ingest it. Confident-sounding generalisations from a
> single observation have, in practice, turned out to be wrong on
> review more than once; the human-triage gate exists specifically
> to catch those before they mislead the next agent.

## Index by category

- [VERILOG-TB-*](#verilog-tb-) — testbench
- [VIVADO-ILA-*](#vivado-ila-) — ILA
- [VIVADO-VIO-*](#vivado-vio-) — VIO
- [VIVADO-XSIM-*](#vivado-xsim-) — xsim simulator
- [VIVADO-BUILD-*](#vivado-build-) — synth / impl / timing
- [VIVADO-XDC-*](#vivado-xdc-) — XDC constraints
- [VIVADO-AXIS-*](#vivado-axis-) — AXI-Stream interconnect
- [BRIDGE-*](#bridge-) — vivado-bridge SKILL usage

---

## VERILOG-TB-*

### VERILOG-TB-001: testbench race on `@(posedge clk); start = 1;`
- **Symptom**: a testbench writing `@(posedge clk); start = 1; @(posedge clk); start = 0;` to produce a one-cycle pulse races against the DUT's NBA `start_q <= start;` evaluated on the same edge. Depending on simulator scheduling, `start_q` never sees `1`, the rising-edge detector never fires, and the FSM never starts.
- **Correct approach**: drive testbench control signals on `negedge clk` (`@(negedge clk); start = 1;`), or use NBA on the testbench side (`start <= 1;`). In SystemVerilog, `program` block with a `clocking` block resolves it at the language level.
- **Industry status**: a textbook trap — Cliff Cummings, SNUG 2000, "Nonblocking Assignments in Verilog Synthesis: Coding Styles That Kill". Not vivado-bridge specific.

### VERILOG-TB-002: rising-edge detector defeated by testbench `force`
- **Symptom**: forcing a VIO output from the testbench (`force dut.vio_start = 1;`) interacts with the `vio_start_d <= vio_start;` NBA register such that the rising-edge term `vio_start & ~vio_start_d` is never observed as `1` for a full cycle, and the downstream FSM start pulse is missed.
- **Correct approach**: insert a `#1` after each `@(posedge clk)` so `force` lands mid-period and is stable across the next edge. Or hold the force for two cycles before releasing.

### VERILOG-TB-003: BRAM `x` initial values corrupt CSV output
- **Symptom**: dumping an output RAM (e.g. `out_ram` written by a streaming pipeline) covers addresses the design never wrote. xsim returns `x` for those cells, the CSV contains literal `x` characters, and `numpy.loadtxt` raises ValueError.
- **Correct approach**: zero-initialise the RAM in HDL: `initial begin for (i = 0; i < DEPTH; i = i + 1) mem[i] = 0; end`. Synthesises cleanly to a 7-series BRAM INIT — no LUT/cell cost.

### VERILOG-TB-004: `wait()` in xsim hangs the bridge
- **Symptom**: a testbench using event-style waits (`wait (running)`, `wait (!running)`) blocks xsim well past the expected condition; the bridge's exec_tcl times out at 30s while Vivado is still stuck inside `run all`.
- **Correct approach**: structure the testbench around absolute time delays (`#N`) instead of `wait`. Always provide a `$finish` reachable path and a safety hard-timeout: `initial begin #50_000; $display("*** TIMEOUT ***"); $finish; end`.

### VERILOG-TB-005: variable declaration inside an unnamed `begin/end` block
- **Symptom**: `if (...) begin ... integer total; ... end` errors with `[VRFC 10-8885] declarations are not allowed in an unnamed block`.
- **Correct approach**: name the block (`begin : my_block ... end`) or hoist the declaration to the surrounding `initial` / `always` block.

---

## VIVADO-ILA-*

### VIVADO-ILA-001: `mark_debug` alone does NOT insert an ILA on Vivado 2024.1
- **Symptom**: HDL annotated only with `(* mark_debug = "true" *)` builds cleanly, but `program_device` returns `ilas=0`. No error, no warning — the bitstream is just silently ILA-less.
- **Correct approach**: `mark_debug` only protects nets from synth optimisation; an explicit `create_debug_core` is required to instantiate the ILA. Use the SKILL helper `debug.create_ila_core(...)` which handles the create + connect + dbg_hub + dedicated XDC routing in one call.

### VIVADO-ILA-002: stale `TRIGGER_COMPARE_VALUE` AND-combines into the next arm
- **Symptom**: a probe configured with `set_property TRIGGER_COMPARE_VALUE eq1'b1` keeps that value across runs. With the default `CONTROL.TRIGGER_CONDITION = GLOBAL_AND`, the next arm waits on the new condition AND every previously-set probe, never triggering. No error.
- **Correct approach**: use `ila.set_triggers(values=..., clear_others=True)`. The default `clear_others=True` resets every probe not in `values` to all-X (don't-care).

### VIVADO-ILA-003: `dbg_hub` defaults to `C_CLK_INPUT_FREQ_HZ = 300_000_000`
- **Symptom**: a fresh `dbg_hub` is created with 300 MHz as its clock-frequency property, regardless of the actual board clock. On a 125 MHz design the implementation infers timing constraints from the wrong frequency, producing skew warnings and potentially missed false_path declarations.
- **Correct approach**: after `create_debug_core dbg_hub`, set `C_CLK_INPUT_FREQ_HZ` to the real frequency. The SKILL helper `debug.create_ila_core(dbg_hub_clock_freq_hz=125_000_000)` handles this; PYNQ-Z1 default is already 125 MHz.

### VIVADO-ILA-004: `save_constraints` rewrites the user-authored XDC
- **Symptom**: `save_constraints -force` after `create_debug_core` writes the ~30 lines of debug constraints into whichever XDC `target_constrs_file` points at — typically a hand-authored `pynq_z1.xdc`. Subsequent `delete_debug_core` + `save_constraints` can leave dbg_hub residue, polluting a file the user maintains.
- **Correct approach**: route the debug save into a dedicated `debug_<name>.xdc` by switching `target_constrs_file` for the duration of the save and restoring it afterwards. `debug.create_ila_core` does this internally.

### VIVADO-ILA-005: `implement_debug_core` blocks past the bridge timeout
- **Symptom**: explicitly calling `implement_debug_core` after `save_constraints -force` materialises the core into the open synth design — a long operation that exceeds the bridge's 30s exec_tcl timeout, returning client_error while Vivado is still working.
- **Correct approach**: do not call `implement_debug_core`. With debug constraints in `constrs_1`, Vivado auto-inserts during `opt_design` of the regular impl. The minimal recipe is `save_constraints -force` → `close_design` → normal `launch_runs impl_1`.

### VIVADO-ILA-006: ILA runtime probe names follow the net, not the logical name
- **Symptom**: `debug.create_ila_core(probes=[{'name':'dbg_out_valid', ...}])` accepts a logical name, but at runtime `ila.set_triggers` and `parse_csv` see the underlying net name (e.g. `keep_dov`). Calling `set_triggers` with the logical name fails with "probe not found".
- **Correct approach**: after `program_device`, call `ila.list_ila_probes` to discover the runtime names. Same shape as the VIO `_1` rename trap.

### VIVADO-ILA-007: `CONTROL.CAPTURE_MODE` / `TRIGGER_CONDITION` may be read-only
- **Symptom**: on a core synthesised without storage qualification or with a single trigger probe, `CONTROL.CAPTURE_MODE`, `CONTROL.TRIGGER_CONDITION`, and `TRIGGER_MODE` are read-only. Writing them returns `[Labtools 27-158] cannot be set to invalid value`.
- **Correct approach**: only touch `CONTROL.DATA_DEPTH` and `CONTROL.TRIGGER_POSITION` at runtime; the SKILL `ila.configure` deliberately exposes only those two. To use storage qualification you must enable it at create time (`C_EN_STRG_QUAL=true` on the debug core) — not yet wrapped in the SKILL.

### VIVADO-ILA-008: `STATIC.ILA_CLOCK_FREQ` is misleading
- **Symptom**: when investigating a "clock not running" suspicion, reading `STATIC.ILA_CLOCK_FREQ` always returns 0 — leading to the wrong conclusion that the clock is dead. The property is a static IP attribute, not a live measurement.
- **Correct approach**: ignore `STATIC.ILA_CLOCK_FREQ`. To check that a clock is alive, expose a free-running heartbeat counter on a VIO input and read it from the host.

### VIVADO-ILA-009: `debug.create_ila_core` read-back returns empty `clock_net` / `probes[].nets`
- **Symptom**: the `clock_net` and per-probe `nets` fields in the result of `debug.create_ila_core(...)` come back as empty strings / empty lists, even though the dedicated debug XDC on disk shows the connections were made correctly and the ILA works on hardware. The properties on the debug-core / debug-port objects themselves (`C_DATA_DEPTH`, `port_width`, dbg_hub frequency) read back fine — only the net resolution via `get_nets -of_objects [get_debug_ports ...]` is affected. Reproducible on both Vivado 2021.1 and 2024.1.
- **Correct approach**: read the connection facts **before** `save_constraints` rather than after. An earlier implementation read after the save, on the assumption that `save_constraints` was non-disturbing — but on both Vivado 2021.1 and 2024.1 the synthesized design ends up effectively closed under the hood after the save, so `get_debug_ports` then returns nothing. The current `debug.create_ila_core` reads right after `connect_debug_port` / dbg_hub setup, before any `save_constraints`. The XDC-on-disk path remains read after the save, since that's what `target_constrs_file` resolves to only post-save.

---

## VIVADO-VIO-*

### VIVADO-VIO-001: VIO probe names get renamed at runtime
- **Symptom**: in HDL `vio_0 u_vio (.probe_in0(rst), .probe_out0(start))`, but the runtime-visible probe names may differ from the HDL net names — typically a `_1` suffix is added when an ILA also touches the same net (`rst` -> `rst_1`). Hard-coded names in Python scripts then fail with "probe not found".
- **Correct approach**: after `program_device`, always call `debug.list_vio_probes` to discover the actual names. Documented in using_vio.md §2 and using_ila.md §8. Cannot be eliminated structurally — only made predictable through doc.

### VIVADO-VIO-002: VIO inputs and outputs both auto-acquire `MARK_DEBUG`
- **Symptom**: HDL nets connected to a VIO `probe_in*` or `probe_out*` automatically get `MARK_DEBUG=1`. Attaching an ILA to one of those nets via `connect_debug_port` returns success but binds 0 nets, producing a silent orphan that fails impl with `[Chipscope 16-213] probeN has K unconnected channels`. Auto-pickup of all `MARK_DEBUG` nets also captures the VIO ones, often unintentionally.
- **Correct approach**: target the inside-instance net (e.g. `u_pid/tick_pulse`) instead of the VIO-attached top-level wire. `debug.create_ila_core` reports `mark_debug_missing=[...]` with a hint when this happens. See using_ila.md §8 and using_vio.md §2.

### VIVADO-VIO-003: `set_property; commit_hw_vio` chained with `;` swallows set failures
- **Symptom**: combining `set_property OUTPUT_VALUE 1 ...; commit_hw_vio ...` into a single `exec_tcl` call means a failing `set_property` aborts before commit — but only the last-statement value is returned, so the failure is invisible. The device receives no update; later reads see the old value.
- **Correct approach**: use the SKILL helpers — `debug.write_vio_probe` (separate set + commit with explicit success check) or `debug.write_vio_probes` (batch, single commit, abort-without-commit on any per-probe failure). Do not chain raw Tcl.

### VIVADO-VIO-004: VIO IP stub goes stale after customisation
- **Symptom**: changing port count via `set_property -dict CONFIG.C_NUM_PROBE_IN ...` and immediately running `synth_1` fails with `port 'probe_in2' does not exist`. The OOC stub was generated before the customisation and Vivado does not regenerate it before the parent synth references it.
- **Correct approach**: after customising the VIO IP, run `synth_ip [get_ips vio_0]` to regenerate the stub. The SKILL helper `build.synthesize(auto_synth_ips=True)` handles this automatically.

### VIVADO-VIO-005: `debug.create_vio(overwrite=True)` cannot reuse the IP name
- **Symptom**: calling `debug.create_vio(name="vio_0", overwrite=True, ...)` to replace an existing VIO can fail with `[Common 17-69] IP name 'vio_0' is already in use in this project` if the helper clears the IP via `remove_files [get_files -of [get_ips $name]]`. On Vivado 2021.1 (and likely earlier 2024.x service packs as well) `get_files -of` returns empty after `export_ip_user_files -reset`, so the `.xci` registration is never actually dropped.
- **Correct approach**: read the IP's `.xci` path directly from `IP_FILE` and pass it to `remove_files`, then verify with `get_ips -quiet $name` that the catalog actually released the name before calling `create_ip` again. The current `debug.create_vio` does this and returns `error_kind="overwrite_failed"` if the readback shows residue, instead of silently letting `create_ip` blow up further down.

---

## VIVADO-XSIM-*

### VIVADO-XSIM-001: `wait()` infinite loop hangs the bridge
- See [VERILOG-TB-004](#verilog-tb-004-wait-in-xsim-hangs-the-bridge) for details.

### VIVADO-XSIM-002: diagnose a parked sim with `get_value /tb/sig`
- **Symptom**: simulation reaches a state where it neither progresses nor calls `$finish`. From outside (Python / bridge), there is no obvious way to inspect what value an internal signal has.
- **Correct approach**: from the Tcl Console, `get_value /tb/sig_name` returns the current value of any internal signal in the loaded sim. This is a standard Vivado xsim command (UG900). Useful for "why is the FSM stuck" investigations. Out of scope for the SKILL itself but worth knowing.

### VIVADO-XSIM-003: `sim.run`'s `finished` flag is unreliable on Vivado 2024.1 + Windows
- **Symptom**: a testbench with `initial begin #500; $finish; end` returns `finished=False` from `sim.run(sim_time_us=60.0, top=...)` on Vivado 2024.1 / Windows, even though `$finish called at time : 500 ns` does appear in `vivado.log`. The `current_time` field then reads `60500 ns`, suggesting `run` consumed the full requested window. Two separate observations explain this: (1) Vivado 2024.1 routes the `$finish called` line to `vivado.log`, not `simulate.log` (which stays size 0), so any detector that scans only `simulate.log` will miss it; (2) Vivado holds the `vivado.log` write handle through a buffered writer, and the OS-level `stat().st_size` Python reads can lag the actual log content by an unbounded amount (the next `$finish called` may already be on disk inside Vivado's process but invisible to a `stat()` from outside). The `sim.run` helper now scans both candidate logs and OR's the result, but the buffering issue defeats it on this exact combination.
- **Correct approach**: do not treat `finished=False` as authoritative on Vivado 2024.1 + Windows. Cross-check by either (a) inspecting `sim.summary()` afterwards (which reads the log fresh and is more likely to see new bytes once Vivado has issued a few subsequent Tcl commands), or (b) writing a sentinel marker like `RESULT: PASSED` from your testbench and checking `pass_markers` in the summary, or (c) running a follow-up short `run 0 us` so Vivado has a chance to flush. The bridge cannot force Vivado's writer to flush from outside.

---

## VIVADO-BUILD-*

### VIVADO-BUILD-001: long combinational chain fails timing at 125 MHz
- **Symptom**: a non-trivial signed-arithmetic datapath (e.g. a Sobel `|Gx|+|Gy|+saturate`, or a PID `Kp*e + Ki*∫e + Kd*Δe`) folded into a single cycle stretches to 16-20 logic levels and fails timing at 125 MHz (8 ns clk) with WNS in the negative ns range.
- **Correct approach**: pipeline the datapath — typically 2 to 5 register stages between major arithmetic groups. Bit-exact correctness is preserved per-tick; only output latency increases. **Try this before reaching for an MMCM clock divider** — see VIVADO-BUILD-002. Example before/after: a 5-stage pipeline took a PID-style design from WNS=-19.6 ns to +0.476 ns at 125 MHz; a 2-stage pipeline took an edge-detection design from WNS=-2.505 ns to +0.761 ns.

### VIVADO-BUILD-002: do not reach for MMCM before pipelining
- **Symptom**: a common response to a 125 MHz timing failure is to insert an MMCM and divide the clock down (e.g. to 25 MHz). It works, but it permanently adds an IP, a clock domain, and the associated CDC complexity.
- **Correct approach**: try pipeline staging first. A PID-style design that initially needed an MMCM closes at 125 MHz with a 5-stage pipeline — the MMCM is not needed. MMCM is only the right move when the design is rate-limited by external interfaces (slow SPI, low-baud UART, etc.), not when an internal datapath chain is too long.

### VIVADO-BUILD-003: VIO output paths often need a retiming flop for timing closure
- **Symptom**: routing a VIO `probe_out` directly into a multiplier or adder tree leaves a long path from the IP's internal pipeline output to the consuming flop, often eating most of the slack budget on its own.
- **Correct approach**: insert a retiming flop on every VIO output (`sp_q <= sp_vio;`) to isolate the IP boundary. This is often the final ingredient that pushes a timing-marginal design over the line (e.g. from -1.967 ns to +0.476 ns).

### VIVADO-BUILD-004: an ILA-instrumented build's timing failure is usually in the user datapath, not dbg_hub
- **Symptom**: a build that contains an ILA fails timing. The failing path's hierarchy includes a name involving `dbg_hub`, and at first glance it looks like the ILA infrastructure is the problem. It is tempting to conclude "ILA at 125 MHz fails timing" and start hunting for ILA-specific workarounds.
- **Correct approach**: read the actual `<top>_timing_summary_routed.rpt` rather than guessing from a path name. The worst path is typically in the user datapath, not in `dbg_hub`. Confirm what the failing endpoint actually is before reaching for an ILA-specific fix. If the failing path really is on a dbg_hub CDC handshake, the targeted fix is to increase the ILA's `C_INPUT_PIPE_STAGES` (more flop stages on the way into the hub) or to assert `set_false_path` on that handshake. dbg_hub at 125 MHz does not, in general, fail timing on a clean user datapath — so a "dbg_hub" name in the failing path is usually a routing artefact through the hub, not a hub timing problem.

---

## VIVADO-XDC-*

### VIVADO-XDC-001: stale debug XDC references signals from a previous top
- **Symptom**: switching the top module without cleaning up debug constraints leaves entries like `connect_debug_port u_ila_0/probe0 [get_nets old_signal]` pointing at signals that no longer exist. `opt_design` then errors with `[Chipscope 16-213]`.
- **Correct approach**: when switching tops, run `debug.delete_ila_core` first to remove the dedicated debug XDC, then re-create with the new top's signals. The SKILL helpers handle the cleanup automatically when used end to end.

### VIVADO-XDC-002: `xc7z020clg400-2` is not a real PYNQ-Z1 part
- **Symptom**: typing the part by hand can produce a non-existent speed grade (e.g. `-2`). Vivado accepts it for project setup but downstream resource estimates and timing are off.
- **Correct approach**: PYNQ-Z1 is `xc7z020clg400-1` (speed grade -1). Confirm via `project.info()`'s `part` field before synthesis, or apply a board file (`board_part`) which sets the part automatically.

---

## VIVADO-AXIS-*

### VIVADO-AXIS-001: master AXI-Stream output with `tready` left floating fails impl with `[Opt 31-67]`
- **Symptom**: an IP exposes a master AXI-Stream output you don't intend to consume (e.g. xfft's `m_axis_status_*` reporting overflow flags). The user-side wrapper leaves the `tready` input pin (which is the **slave-side** signal of the master channel and must be driven from outside the IP) unconnected. Synth issues a "CRITICAL WARNING" about the unconnected `*_tready`, but the build completes. `opt_design` then trims the upstream FIFO arithmetic that fed the dropped channel and emits a cryptic `[Opt 31-67]` LUT-input-pin-missing error pointing at internal FIFO logic — with no obvious link back to the AXI-Stream handshake.
- **Correct approach**: every master AXI-Stream port that you do not actively sink must have its `tready` tied to `1'b1` so the channel is always ready and the upstream FIFO is not optimised away. Even better, keep a wire for the corresponding `tdata` / `tvalid` so synth doesn't warn about driving a dangling output. The general rule: **a master AXIS output you ignore still needs `tready=1'b1` on the consumer side**. Drop-in pattern for an unused status channel:
  ```verilog
  wire [W-1:0] _unused_tdata;
  wire         _unused_tvalid;
  wire         _unused_tready = 1'b1;
  fft_ip u_fft (
      ...,
      .m_axis_status_tdata  (_unused_tdata),
      .m_axis_status_tvalid (_unused_tvalid),
      .m_axis_status_tready (_unused_tready)
  );
  ```
- **Industry status**: a generic AXI-Stream gotcha, not Xilinx-specific. AMBA AXI4-Stream specification §2.2: a master must wait for `TREADY` before considering a beat consumed; downstream optimisers reasonably treat a permanently-unready channel as dead logic. Worth knowing whenever you wrap any AXIS-output IP.

---

## BRIDGE-*

### BRIDGE-001: `return $foo` at top-level Tcl returned as failure (TCL_RETURN)
- **Symptom**: `c.exec_tcl("return [get_property DIRECTORY [current_project]]")` came back as `[tcl_error]` even though `output` contained the correct directory string. `return` outside a `proc` produces TCL_RETURN (rc=2), which an early version of the bridge treated as failure.
- **Correct approach**: the bridge now treats both rc=0 (TCL_OK) and rc=2 (TCL_RETURN) as success. `return $x` from a top-level snippet is a supported pattern.

### BRIDGE-002: `set_param general.maxBackup 0` swallowed by `catch`
- **Symptom**: an early version of the bridge's `quiet_vivado` setup wrapped a `set_param` call in `catch`, hiding genuine errors when the parameter name was wrong on a given Vivado version. Other failures masked behind the same `catch` were also invisible.
- **Correct approach**: the `_set_param_logged` helper now logs both success and failure paths explicitly.

### BRIDGE-003: every `exec_tcl` echoes `INFO: [vbridge 1-1]` to the Tcl Console
- **Symptom**: each call adds an INFO line, and after 100 lines Vivado auto-suppresses the message ID with `Message 'vbridge 1 1' appears 100 times and further instances ... will be disabled`. Functional impact is zero — return values are unaffected — but the Tcl Console, which is the human/AI primary observation window, fills up with bridge chatter.
- **Possible fixes**: (a) suppress the message ID at server start (`set_msg_config -id "vbridge 1-1" -severity SUPPRESS`), (b) make the per-call `puts` verbose-flag controlled, (c) redirect to vivado.log only and skip the Tcl Console.

---

## How this file is meant to be used

This file is **not** an introductory read. It is consulted at specific
moments during a project, see the table in SKILL.md ("Anti-pattern
checklist") for the mapping. Each entry is a quick lookup with two
parts: the symptom you might be observing and the correct approach.

If you (an agent) discover something new that fits the format above,
**do not add an entry yourself**. Surface it in your final report so
a human can decide whether to ingest it. Confident-sounding
generalisations from a single observation can turn out to be wrong on
review; the human-triage gate exists specifically to catch those before
they mislead the next agent.

Entries whose underlying SKILL bug has already been fixed are kept
here as historical record so the API shape remains explainable to
someone reading the SKILL fresh.
