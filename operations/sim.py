"""Simulation operations: launch xsim, run, close, summarise.

The bridge sees Vivado xsim through three Tcl entry points:
`launch_simulation`, `run`, and `close_sim`. Each is a normal Tcl
call -- *but* `run all` against a buggy testbench is the single most
reliable way to wedge the bridge for tens of minutes, because it ties
up Vivado's main Tcl thread until the testbench `$finish`es. The
operations here are explicitly shaped to keep the bridge responsive:

- `launch_simulation` and `run` are issued separately. Each `run`
  carries an explicit `time_value` (seconds-of-sim-time, never
  `run all`) so that even a stuck testbench can only block for that
  finite slice before the bridge regains control.
- The Python-side `time_limit` is a wall-clock deadline. We keep
  issuing short `run` commands until the simulator reports `$finish`
  (visible via `current_time` not advancing across two consecutive
  runs) or the wall-clock budget runs out.
- We never call `wait` or `wait_on_run` in the testbench-driving path.

Testbench guidance lives in [using_simulation.md](../references/using_simulation.md).
The short version: write tests with `#TIME` literals, declare every
variable at module scope, ensure a `$finish` path exists, and add a
hard timeout of your own (`initial begin #50_000; $finish; end`).

These operations target the *active* simulation set (`current_sim` and
`get_filesets sim_1` style). Multi-sim setups should call
`client.exec_tcl` directly.
"""

from __future__ import annotations

from pathlib import Path
import re
import time
from typing import Any

from ._common import fail, from_tcl_failure, ok, query_one


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------

def get_sim_status(client) -> dict[str, Any]:
    """Return whether a simulation is currently open and at what sim time.

    `current_sim` returns the open sim name (e.g. "sim_1") or empty when
    there is no simulation. `current_time` returns the current sim time
    as a string like "1234.5 ns". We pass both back as-is so the caller
    can decide what to do.
    """
    sim = query_one(client, "current_sim -quiet")
    if sim is None:
        # current_sim itself failed -- not "no sim open", but the Tcl
        # call errored. Surface it as tcl_error.
        return fail("tcl_error", "current_sim -quiet failed")
    if not sim:
        return ok("no simulation open", open=False, sim=None, current_time=None)
    cur = query_one(client, "current_time") or ""
    return ok(
        f"{sim} open at {cur or '?'}",
        open=True,
        sim=sim,
        current_time=cur,
    )


def close_sim(client, *, force: bool = True) -> dict[str, Any]:
    """Close the active simulation. `force=True` mirrors `close_sim -force`,
    which is the right default when you want to recover after a stuck
    `run`.

    Idempotent: if no simulation is open the call still returns success
    with `was_open=False`, so "guarantee no sim is open" composes in
    one line without a pre-check.
    """
    pre_sim = (query_one(client, "current_sim -quiet") or "").strip()
    if not pre_sim:
        return ok("no simulation was open", client=client, was_open=False)
    flag = "-force" if force else ""
    r = client.exec_tcl(f"close_sim {flag}".strip())
    if not r.success:
        return from_tcl_failure(r, client=client)
    return ok("simulation closed", client=client, was_open=True)


# ---------------------------------------------------------------------------
# launch + bounded run loop
# ---------------------------------------------------------------------------

_TIME_RE = re.compile(r"([-+]?\d+(?:\.\d+)?)\s*([a-z]+)")


def _parse_sim_time_ns(raw: str) -> float | None:
    """Parse a Vivado sim-time string ('1234.5 ns') into nanoseconds.

    Returns None if the format isn't recognised -- callers should treat
    that as "couldn't tell whether sim advanced", not "advanced by 0".
    """
    if not raw:
        return None
    m = _TIME_RE.search(raw.strip().lower())
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    unit = m.group(2)
    factor = {
        "fs": 1e-6, "ps": 1e-3, "ns": 1.0,
        "us": 1e3, "ms": 1e6, "s": 1e9,
    }.get(unit)
    if factor is None:
        return None
    return v * factor


def run(
    client,
    *,
    sim_time_us: float,
    top: str | None = None,
    timeout: float = 60.0,
    reuse: bool = False,
    restart: bool = False,
) -> dict[str, Any]:
    """Run the active behavioural simulation for at most `sim_time_us` µs.

    Single-shot model. The bridge issues exactly one `run <sim_time_us> us`
    against xsim and returns when either:

      - the testbench reaches `$finish` (xsim stops early; reported as
        `finished=True`), or
      - `sim_time_us` of sim time has elapsed (reported as `finished=False`)

    There is no chunking and no internal loop. If you want to keep going
    after this call returns, call `run()` again with `reuse=True` and
    another `sim_time_us` -- "give me the next 100 µs" composes from
    multiple plain calls instead of a long-running loop the caller
    can't see into.

    Pre-flight check: if a simulation is already open and has advanced
    past `0 ns`, fail with `error_kind="sim_already_running"` unless the
    caller passes `reuse=True` (continue against it) or `restart=True`
    (close_sim -force then launch fresh). The bridge can't tell whether
    the open sim was started by the user from the GUI, by a previous
    bridge session, or by an earlier failed `run()` -- silently
    overwriting or piling on top is what produced the original 46 ms
    runaway, so the policy is "make the caller decide".

    Args:
        sim_time_us: maximum sim time to advance, in microseconds. The
            simulator may stop earlier if the testbench `$finish`es.
            Required -- there is no implicit "run forever" mode.
        top: optional sim_1 top to set before launching. Ignored when
            reuse=True (you can't change the top of an already-open
            sim from inside this call). None leaves the current
            sim_1 top property alone.
        timeout: per-call exec_tcl deadline for the underlying
            `run <sim_time_us> us`, in seconds. The bridge will give up
            and raise if Vivado hasn't returned in this much wall time.
            This is purely a safety valve for a wedged simulator -- a
            testbench advancing sim_time_us of sim time should normally
            return well within the default.
        reuse: if True and a sim is already open, skip launch and run
            against it (the existing top stays in effect). Mutually
            exclusive with restart.
        restart: if True and a sim is already open, close_sim -force
            it before launching fresh. Mutually exclusive with reuse.

    Returns the usual ok/fail dict plus:
        sim:               current_sim string ("simulation_1" / "sim_1")
        before_time:       sim time before the run() (string from Vivado)
        current_time:      sim time after the run() (string from Vivado)
        finished:          True iff the testbench `$finish`ed. The
                           detection is multi-pronged (Tcl error path,
                           sim-time-advance heuristic, and a `$finish
                           called` scan against simulate.log /
                           vivado.log), but the log scan can be
                           defeated on Vivado 2024.1 + Windows: xsim
                           writes `$finish called` to vivado.log
                           through a buffered handle that flushes only
                           on Vivado-side events, and the OS-level
                           file size we read from Python may lag the
                           actual content by an arbitrary amount. As
                           a result, `finished=False` does NOT
                           guarantee the testbench actually ran past
                           sim_time_us. Cross-check by reading
                           `current_time` and `before_time` and
                           inspecting `sim.summary()` for the
                           `$finish called` line if you need a hard
                           verdict.
        elapsed_s:         wall-clock seconds spent in this call.
    """
    if reuse and restart:
        return fail(
            "client_error",
            "reuse=True and restart=True are mutually exclusive",
        )
    if sim_time_us is None:
        return fail(
            "client_error",
            "sim_time_us is required (None is not accepted; pass a "
            "positive number of microseconds).",
        )
    if sim_time_us <= 0:
        return fail(
            "client_error",
            f"sim_time_us must be > 0 (got {sim_time_us})",
        )

    # Pre-flight: inspect current sim state before doing anything.
    pre_sim = (query_one(client, "current_sim -quiet") or "").strip()
    pre_time_str = (query_one(client, "current_time") or "").strip() if pre_sim else ""
    pre_time_ns = _parse_sim_time_ns(pre_time_str) if pre_time_str else None
    sim_open = bool(pre_sim)
    sim_advanced = sim_open and pre_time_ns is not None and pre_time_ns > 0.0

    if sim_open and sim_advanced and not (reuse or restart):
        return fail(
            "sim_already_running",
            f"A simulation ({pre_sim}) is already open at {pre_time_str}. "
            f"Refusing to launch another on top of it. Pass reuse=True to "
            f"continue against the existing sim, or restart=True to "
            f"close_sim -force it and launch fresh.",
            sim=pre_sim,
            current_time=pre_time_str,
        )

    if sim_open and restart:
        rc = client.exec_tcl("close_sim -force")
        if not rc.success:
            return from_tcl_failure(rc)
        sim_open = False

    # `top` only applies when we're about to launch. Changing the top of
    # an already-open sim has no effect until next launch_simulation, so
    # quietly accepting it under reuse=True would be misleading.
    if top is not None and sim_open and reuse:
        return fail(
            "client_error",
            "top= cannot be set together with reuse=True (top only takes "
            "effect at launch_simulation time; the open sim's top is fixed)",
        )
    if top is not None:
        r = client.exec_tcl(
            f"set_property top {top} [get_filesets sim_1]"
        )
        if not r.success:
            return from_tcl_failure(r, client=client)

    # Snapshot xsim log sizes BEFORE launch_simulation, not just
    # before `run`. On some Vivado/xsim versions a testbench that
    # `$finish`es very early hits `$finish` *during* launch_simulation
    # (the initial block runs to its first delay before launch returns),
    # so we need the snapshot to predate the launch as well as the run.
    # We snapshot both candidate logs:
    #   - simulate.log -- where older Vivado releases write xsim output
    #   - vivado.log    -- where Vivado 2024.1 routes the `$finish
    #     called ...` line; simulate.log stays empty there.
    # If either log doesn't exist yet (first launch), the snapshot is 0
    # and the scan reads the whole log -- still correct.
    pre_simlog_p = _sim_log_path(client)
    pre_simlog_size = pre_simlog_p.stat().st_size if pre_simlog_p else 0
    pre_vivlog_p = _vivado_log_path(client)
    pre_vivlog_size = pre_vivlog_p.stat().st_size if pre_vivlog_p else 0

    if not (sim_open and reuse):
        r = client.exec_tcl(
            "launch_simulation -mode behavioral -simset sim_1",
            timeout=120,
        )
        if not r.success:
            return from_tcl_failure(r, client=client)
        # launch_simulation creates simulate.log if it didn't exist; if
        # we couldn't resolve the path before, try again now.
        if pre_simlog_p is None:
            pre_simlog_p = _sim_log_path(client)

    # Detect "$finish during launch_simulation" *before* attempting the
    # `run` call. A testbench whose `initial begin #N; $finish; end`
    # fires inside `launch_simulation` will not emit a fresh `$finish
    # called` line during a subsequent `run` (the sim is already
    # parked), so the post-run scan would miss it. We snapshot a
    # second log offset here, between launch and run, so that:
    #   - launch-time $finish shows up in the launch_emit_size delta
    #   - run-time $finish (the more common case) shows up in the
    #     post-run delta starting from launch_emit_size
    launch_emit_simlog_size = (
        pre_simlog_p.stat().st_size if pre_simlog_p else 0
    )
    launch_emit_vivlog_size = (
        pre_vivlog_p.stat().st_size if pre_vivlog_p else 0
    )
    finished_during_launch = (
        _scan_log_for_finish(pre_simlog_p, pre_simlog_size)
        or _scan_log_for_finish(pre_vivlog_p, pre_vivlog_size)
    )

    sim = (query_one(client, "current_sim -quiet") or "").strip()
    before_str = (query_one(client, "current_time") or "").strip()
    before_ns = _parse_sim_time_ns(before_str)

    start_t = time.time()
    rr = client.exec_tcl(
        f"run {sim_time_us:.6g} us",
        timeout=timeout,
    )
    elapsed = time.time() - start_t

    after_str = (query_one(client, "current_time") or "").strip()
    after_ns = _parse_sim_time_ns(after_str)

    if not rr.success:
        # xsim raises a tcl_error on $finish in some Vivado versions.
        # Treat that as "finished early", anything else as a real error.
        err = (rr.message or "").lower()
        if "finish" in err or "finished" in err:
            return ok(
                f"{sim} finished at {after_str}",
                client=client,
                sim=sim,
                before_time=before_str,
                current_time=after_str,
                finished=True,
                elapsed_s=elapsed,
            )
        return from_tcl_failure(rr, client=client)

    # `run` returned cleanly. Did sim time advance the full requested
    # amount? If less, the testbench $finish'd inside the window.
    requested_ns = sim_time_us * 1000.0
    advanced_ns = (
        (after_ns - before_ns)
        if (after_ns is not None and before_ns is not None)
        else None
    )
    finished_by_time = (
        advanced_ns is not None
        and advanced_ns + 0.5 < requested_ns  # 0.5 ns slack for fp rounding
    )
    # Some Vivado/xsim versions complete the full requested window even
    # after a testbench `$finish` -- advanced_ns reaches requested_ns
    # and the time-based heuristic returns False even though the
    # testbench did finish. Two log scans cover the cases:
    #   * `finished_during_launch` (set above, between launch and run):
    #       caught a `$finish called` line emitted while
    #       launch_simulation ran -- testbenches with very short delays
    #       reach $finish before the launch returns. The subsequent
    #       `run` call advances time on a parked sim and does not
    #       produce a new $finish line, so this *must* be checked
    #       before the run, not after.
    #   * post-run scan starting from launch_emit_*_size: the more
    #       common case where the testbench reaches $finish during the
    #       `run` window itself.
    # The relevant log path differs between Vivado releases
    # (simulate.log on older, vivado.log on 2024.1), so we OR both
    # candidates within each scan.
    finished_during_run = (
        _scan_log_for_finish(pre_simlog_p, launch_emit_simlog_size)
        or _scan_log_for_finish(pre_vivlog_p, launch_emit_vivlog_size)
    )
    finished_by_log = finished_during_launch or finished_during_run
    finished = finished_by_time or finished_by_log
    result = ok(
        f"{sim} {'finished' if finished else 'ran'} to {after_str}",
        client=client,
        sim=sim,
        before_time=before_str,
        current_time=after_str,
        finished=finished,
        elapsed_s=elapsed,
    )

    # Cap-without-finish self-diagnosis. When the simulator consumed
    # the full sim_time_us window without reaching $finish, prepend a
    # one-line hint so callers don't have to deduce that fact from
    # finished=False alone. The hint lists
    # the common causes so the AI / user can triage faster than
    # "why didn't it finish?". We do NOT try to second-guess whether
    # the testbench produced output; finished=False already means
    # "you asked for N µs and the sim used all of it" -- that's the
    # definitive signal.
    if not finished:
        hint = (
            "[bridge] Simulator advanced the full sim_time_us window "
            "without reaching $finish. Common causes: (a) the testbench "
            "is wedged on a wait()/event that never fires; (b) the "
            "stimulus initial block parked on a clock edge without "
            "$finish; (c) sim_time_us is shorter than the testbench "
            "actually needs. Read result['warnings'] for any RESULT: / "
            "PASS / FAIL the testbench produced before the cap fired; "
            "see references/using_simulation.md."
        )
        existing = result.get("warnings") or []
        result["warnings"] = [hint, *existing]

    return result


# ---------------------------------------------------------------------------
# log lookup
# ---------------------------------------------------------------------------

def _sim_log_path(client) -> Path | None:
    """Resolve the path to simulate.log for the current sim_1 / xsim run.

    Returns None if anything in the chain is missing rather than
    guessing -- this is a read-only helper, callers handle the absence.
    """
    sim_dir = query_one(
        client,
        "file normalize "
        "[file join [get_property DIRECTORY [current_project]] "
        "[get_property NAME [current_project]].sim sim_1 behav xsim]",
    )
    if not sim_dir:
        return None
    p = Path(sim_dir) / "simulate.log"
    return p if p.exists() else None


def _scan_log_for_finish(log_path: Path | None, start_offset: int) -> bool:
    """Return True iff `log_path` gained a `$finish called` line starting
    from `start_offset`.

    Scans only the bytes added since the snapshot was taken so a
    previous run's $finish doesn't false-positive on a later run that
    actually wedged. Tolerates the log not existing yet (returns False)
    and read errors (also returns False -- the time-based heuristic is
    the fallback). Does NOT use `$finish` token alone, because a
    testbench's own diagnostic `$display "$finish soon"` would falsely
    match; the canonical xsim line begins with `$finish called`.

    Used against both simulate.log (some Vivado releases) and
    vivado.log (Vivado 2024.1 routes xsim output here, leaving
    simulate.log empty). The caller scans both and OR's the results.
    """
    if log_path is None or not log_path.exists():
        return False
    try:
        cur_size = log_path.stat().st_size
    except OSError:
        return False
    if cur_size <= start_offset:
        return False
    try:
        with log_path.open("rb") as f:
            f.seek(start_offset)
            chunk = f.read(cur_size - start_offset)
    except OSError:
        return False
    try:
        text = chunk.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        return False
    return "$finish called" in text


def _vivado_log_path(client) -> Path | None:
    """Resolve the path to vivado.log via the bridge.

    Vivado 2024.1 routes xsim's `$finish called` line to vivado.log
    (the Tcl Console transcript) rather than to simulate.log. Older
    releases write it to simulate.log. To be portable across both,
    the sim-finished detection scans whichever of the two has gained
    a marker line during the run.
    """
    cwd = query_one(client, "pwd")
    if not cwd:
        return None
    cwd = cwd.strip()
    p = Path(cwd) / "vivado.log"
    return p if p.exists() else None


def summary(client, *, sample_size: int = 5) -> dict[str, Any]:
    """Return a brief summary of the most recent simulate.log.

    Pulls the path from the project's sim_1 directory layout and
    counts ERROR / Fatal / `$finish` / `[TB]` markers. This is the
    "did the testbench pass?" call after `sim.run` returns.

    On failure to locate the log we deliberately fail with a parse_failed
    error rather than returning success with empty fields -- silent
    "looks fine" responses for missing logs are the exact kind of
    fallback the bridge avoids elsewhere.
    """
    p = _sim_log_path(client)
    if p is None:
        return fail(
            "parse_failed",
            "Could not locate simulate.log. Has launch_simulation run yet, "
            "and is the project layout standard (project.sim/sim_1/behav/xsim)?",
            log_path=None,
        )
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return fail(
            "parse_failed",
            f"Could not read {p}: {e}",
            log_path=str(p),
        )

    errors: list[str] = []
    fatals: list[str] = []
    finishes: list[str] = []
    pass_markers = 0
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("ERROR:"):
            if len(errors) < sample_size:
                errors.append(line[:250])
        if "Fatal:" in line:
            if len(fatals) < sample_size:
                fatals.append(line[:250])
        # The canonical xsim line is "$finish called at time : N ns ...".
        # Match on the substring `$finish called` so a testbench's own
        # diagnostic `$display "$finish in 100 ns"` does not produce a
        # false positive.
        if "$finish called" in line:
            if len(finishes) < sample_size:
                finishes.append(line[:250])
        # Pass-marker matching is intentionally permissive. Testbench
        # conventions vary widely: "ALL PASS", "TEST PASSED",
        # "RESULT: PASSED", "[TB] PASS", etc. The narrow earlier
        # pattern (`ALL PASS` or `TEST PASSED`) missed the common
        # "RESULT: PASSED" form. We now match any line that mentions
        # "PASS" near a marker token. Worth knowing this is heuristic --
        # callers that need a deterministic pass signal should look
        # for a specific string they emit themselves.
        upper = line.upper()
        if (
            "ALL PASS" in upper
            or "TEST PASSED" in upper
            or "TEST PASS " in upper
            or "RESULT: PASSED" in upper
            or "RESULT: PASS " in upper
            or "[TB] PASS" in upper
        ):
            pass_markers += 1

    return ok(
        f"errors={len(errors)}, fatals={len(fatals)}, "
        f"finishes={len(finishes)}, pass_markers={pass_markers}",
        log_path=str(p),
        errors=errors,
        fatals=fatals,
        finishes=finishes,
        pass_markers=pass_markers,
    )
