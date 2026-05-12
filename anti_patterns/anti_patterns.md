# Vivado / vivado-bridge anti-patterns

A catalogue of failure modes confirmed in development, together with
the corresponding correct approach. Use this as a checklist consulted
at specific checkpoints (see SKILL.md "Anti-pattern checklist") rather
than as a document to read end-to-end.

> **Scope**: this file lists traps that the SKILL helpers cannot prevent
> on their own — Verilog/HDL pitfalls, Vivado specification quirks the
> helpers cannot paper over, and external constraints. Failure modes
> already prevented by the SKILL helpers are intentionally not listed
> here; the SKILL itself is the correct approach for those.

> **Guard rail**: every entry here was added by **human triage**, not
> by an agent that hit the trap. Agents that suspect a new pattern
> should surface it in their final report so a human can decide
> whether to ingest it. Confident-sounding generalisations from a
> single observation have, in practice, turned out to be wrong on
> review more than once; the human-triage gate exists specifically
> to catch those before they mislead the next agent.

## Index by category

- [VERILOG-TB-*](#verilog-tb-) — testbench
- [VIVADO-ILA-*](#vivado-ila-) — ILA (only traps the SKILL cannot prevent)
- [VIVADO-VIO-*](#vivado-vio-) — VIO
- [VIVADO-XSIM-*](#vivado-xsim-) — xsim simulator
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

### VIVADO-ILA-006: ILA runtime probe names follow the net, not the logical name
- **Symptom**: `debug.create_ila_core(probes=[{'name':'dbg_out_valid', ...}])` accepts a logical name, but at runtime `ila.set_triggers` and `parse_csv` see the underlying net name (e.g. `keep_dov`). Calling `set_triggers` with the logical name fails with "probe not found".
- **Correct approach**: after `program_device`, call `ila.list_ila_probes` to discover the runtime names. Same shape as the VIO `_1` rename trap.

### VIVADO-ILA-008: `STATIC.ILA_CLOCK_FREQ` is misleading
- **Symptom**: when investigating a "clock not running" suspicion, reading `STATIC.ILA_CLOCK_FREQ` always returns 0 — leading to the wrong conclusion that the clock is dead. The property is a static IP attribute, not a live measurement.
- **Correct approach**: ignore `STATIC.ILA_CLOCK_FREQ`. To check that a clock is alive, expose a free-running heartbeat counter on a VIO input and read it from the host.

---

## VIVADO-VIO-*

### VIVADO-VIO-001: VIO probe names get renamed at runtime
- **Symptom**: in HDL `vio_0 u_vio (.probe_in0(rst), .probe_out0(start))`, but the runtime-visible probe names may differ from the HDL net names — typically a `_1` suffix is added when an ILA also touches the same net (`rst` -> `rst_1`). Hard-coded names in Python scripts then fail with "probe not found".
- **Correct approach**: after `program_device`, always call `debug.list_vio_probes` to discover the actual names. Documented in using_vio.md §2 and using_ila.md §8. Cannot be eliminated structurally — only made predictable through doc.

### VIVADO-VIO-002: VIO inputs and outputs both auto-acquire `MARK_DEBUG`
- **Symptom**: HDL nets connected to a VIO `probe_in*` or `probe_out*` automatically get `MARK_DEBUG=1`. Attaching an ILA to one of those nets via `connect_debug_port` returns success but binds 0 nets, producing a silent orphan that fails impl with `[Chipscope 16-213] probeN has K unconnected channels`. Auto-pickup of all `MARK_DEBUG` nets also captures the VIO ones, often unintentionally.
- **Correct approach**: target the inside-instance net (e.g. `u_pid/tick_pulse`) instead of the VIO-attached top-level wire. `debug.create_ila_core` reports `mark_debug_missing=[...]` with a hint when this happens. See using_ila.md §8 and using_vio.md §2.

---

## VIVADO-XSIM-*

### VIVADO-XSIM-003: `sim.run`'s `finished` flag is unreliable on Vivado 2024.1 + Windows
- **Symptom**: a testbench with `initial begin #500; $finish; end` returns `finished=False` from `sim.run(sim_time_us=60.0, top=...)` on Vivado 2024.1 / Windows, even though `$finish called at time : 500 ns` does appear in `vivado.log`. The `current_time` field then reads `60500 ns`, suggesting `run` consumed the full requested window. Two separate observations explain this: (1) Vivado 2024.1 routes the `$finish called` line to `vivado.log`, not `simulate.log` (which stays size 0), so any detector that scans only `simulate.log` will miss it; (2) Vivado holds the `vivado.log` write handle through a buffered writer, and the OS-level `stat().st_size` Python reads can lag the actual log content by an unbounded amount (the next `$finish called` may already be on disk inside Vivado's process but invisible to a `stat()` from outside). The `sim.run` helper now scans both candidate logs and OR's the result, but the buffering issue defeats it on this exact combination.
- **Correct approach**: do not treat `finished=False` as authoritative on Vivado 2024.1 + Windows. Cross-check by either (a) inspecting `sim.summary()` afterwards (which reads the log fresh and is more likely to see new bytes once Vivado has issued a few subsequent Tcl commands), or (b) writing a sentinel marker like `RESULT: PASSED` from your testbench and checking `pass_markers` in the summary, or (c) running a follow-up short `run 0 us` so Vivado has a chance to flush. The bridge cannot force Vivado's writer to flush from outside.

---

## VIVADO-XDC-*

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
