"""Build-flow operations: synthesize, implement, observe runs.

These operate on the project's *active* runs -- whichever run is set as
current synthesis / implementation in the open project (typically
synth_1 / impl_1 in fresh projects, but possibly synth_2 / impl_3 if
the user has been experimenting with multiple strategies).

`current_run -synthesis` / `current_run -implementation` is what we
trust. Vivado returns the project's default run there even on freshly
created projects. If those queries come back empty, something is wrong
(no project open, or a corrupted project) and we report it as
not_found rather than silently picking another run -- masking that
state would hide real problems.

Multi-run users who need to drive a *non-active* run should call
`client.exec_tcl(...)` directly.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from ._common import attach_console_warnings, fail, from_tcl_failure, ok, query_one


# ---------------------------------------------------------------------------
# Timing summary parsing
# ---------------------------------------------------------------------------

_TIMING_HEADER_RE = re.compile(
    r"^\s*WNS\s*\(ns\)\s+TNS\s*\(ns\)", re.IGNORECASE,
)


def _parse_timing_summary(rpt_path: Path) -> dict[str, Any] | None:
    """Pull WNS / TNS from a `*_timing_summary_routed.rpt` file.

    Returns None when the file isn't present or the expected header
    can't be located -- we deliberately do not guess at which numbers
    are timing slack vs hold/pulse-width when the format shifts. Callers
    surface the failure as a warning ("could not parse timing summary")
    rather than substituting a fake 0.

    Vivado 2024.1 lays out the "Design Timing Summary" with a header:

        WNS(ns)  TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints  ...
        -------  -------  ---------------------  -------------------
         -2.715  -49.520                     12                  124  ...

    We grab the first non-empty, non-separator row after the header.
    """
    if not rpt_path.exists():
        return None
    try:
        text = rpt_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if not _TIMING_HEADER_RE.match(line):
            continue
        for data in lines[i + 1:]:
            stripped = data.strip()
            if not stripped:
                continue
            if set(stripped) <= set("- "):
                continue
            tokens = stripped.split()
            if len(tokens) < 2:
                return None
            try:
                wns = float(tokens[0])
                tns = float(tokens[1])
            except ValueError:
                return None
            return {"wns": wns, "tns": tns}
        return None
    return None


def _timing_summary_for_run_dir(run_dir: Path) -> dict[str, Any]:
    """Look up the routed timing summary for an impl run directory.

    Always returns a dict with the same keys; values are None when the
    report isn't available yet. The `met_timing` field is None (not
    False) when we couldn't read the report -- "missing" and "failed"
    are different states, and we do not want to falsely report a
    closed-timing build as failing or vice versa.
    """
    candidates = sorted(run_dir.glob("*_timing_summary_routed.rpt"))
    rpt_path = candidates[0] if candidates else None
    if rpt_path is None:
        return {
            "timing_report_path": None,
            "wns": None,
            "tns": None,
            "met_timing": None,
        }
    parsed = _parse_timing_summary(rpt_path)
    if parsed is None:
        return {
            "timing_report_path": str(rpt_path),
            "wns": None,
            "tns": None,
            "met_timing": None,
        }
    return {
        "timing_report_path": str(rpt_path),
        "wns": parsed["wns"],
        "tns": parsed["tns"],
        "met_timing": parsed["wns"] >= 0 and parsed["tns"] >= 0,
    }


# ---------------------------------------------------------------------------
# active-run resolution
# ---------------------------------------------------------------------------

def _active_synth_run(client) -> str | None:
    """Return the name of the project's active synthesis run.

    Returns None if `current_run -synthesis` is empty -- that means no
    project is open or no synthesis run exists. We deliberately do not
    fall back to "first run we can find"; callers should surface the
    empty state as not_found.
    """
    name = query_one(client, "current_run -synthesis")
    return name or None


def _active_impl_run(client) -> str | None:
    """Return the name of the project's active implementation run, or
    None (see `_active_synth_run` for the no-fallback rationale).
    """
    name = query_one(client, "current_run -implementation")
    return name or None


def summary(client) -> dict[str, Any]:
    """One-shot snapshot of build state. Useful as the first call in a
    session, when you want to know "is this project ready to program?"
    without making three or four queries yourself.

    Returns active synth/impl run names, their statuses, the bitstream
    paths if any, and a top-line "ready_to_program" boolean.
    """
    active = get_active_runs(client)
    if not active["success"]:
        return active
    out: dict[str, Any] = {
        "synth_run": active.get("synth_run"),
        "impl_run": active.get("impl_run"),
    }

    if active.get("synth_run"):
        s = get_run_status(client, kind="synthesis")
        out["synth_status"] = s.get("status")
        out["synth_complete"] = bool(s.get("is_complete"))
        out["synth_failed"] = bool(s.get("is_failed"))
    else:
        out["synth_status"] = None
        out["synth_complete"] = False
        out["synth_failed"] = False

    if active.get("impl_run"):
        i = get_run_status(client, kind="implementation")
        out["impl_status"] = i.get("status")
        out["impl_complete"] = bool(i.get("is_complete"))
        out["impl_failed"] = bool(i.get("is_failed"))
    else:
        out["impl_status"] = None
        out["impl_complete"] = False
        out["impl_failed"] = False

    bs = find_bitstream(client)
    out["bit_path"] = bs.get("bit_path") if bs["success"] else None
    out["ltx_path"] = bs.get("ltx_path") if bs["success"] else None
    out["bit_exists"] = bool(bs.get("bit_exists"))
    out["ltx_exists"] = bool(bs.get("ltx_exists"))

    # Timing closure status. Pulled from the routed timing summary, not
    # from STATUS strings, because Vivado happily marks an impl run
    # "Complete" *and* generates a bitstream even when WNS is negative.
    # Callers should branch on `met_timing` separately from
    # `impl_complete`; the build can succeed (bitstream exists) and
    # still be timing-broken on hardware.
    impl_dir = bs.get("impl_dir") if bs["success"] else None
    if impl_dir:
        timing = _timing_summary_for_run_dir(Path(impl_dir))
    else:
        timing = {
            "timing_report_path": None,
            "wns": None,
            "tns": None,
            "met_timing": None,
        }
    out.update(timing)

    out["ready_to_program"] = bool(
        out["impl_complete"] and not out["impl_failed"] and out["bit_exists"]
    )

    if out["met_timing"] is True:
        timing_str = f"timing met (WNS={out['wns']:.3f})"
    elif out["met_timing"] is False:
        timing_str = f"TIMING FAILED (WNS={out['wns']:.3f}, TNS={out['tns']:.3f})"
    else:
        timing_str = "timing=?"
    msg = (
        f"synth={out['synth_status'] or 'none'}, "
        f"impl={out['impl_status'] or 'none'}, "
        f"bit={'yes' if out['bit_exists'] else 'no'}, "
        f"{timing_str}, "
        f"ready_to_program={out['ready_to_program']}"
    )
    return ok(msg, client=client, **out)


def get_active_runs(client) -> dict[str, Any]:
    """Tell the caller which runs the build operations will act on.

    Useful as a sanity check before launching synthesis / implementation
    in a project with multiple runs configured.
    """
    synth = _active_synth_run(client)
    impl = _active_impl_run(client)
    if not synth and not impl:
        return fail(
            "not_found",
            "Project has no synthesis or implementation runs. "
            "Is a project open?",
        )
    return ok(
        f"active synth={synth or 'none'}, impl={impl or 'none'}",
        synth_run=synth,
        impl_run=impl,
    )


# ---------------------------------------------------------------------------
# status helpers
# ---------------------------------------------------------------------------

def _resolve_run(
    client,
    *,
    kind: str | None,
    run: str | None,
) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Translate (kind=, run=) into a concrete run name.

    Exactly one of `kind` or `run` must be given. Returns
    (run_name, kind_label, error_dict). On success error_dict is None.
    `kind_label` is "synthesis" / "implementation" when resolved by
    kind, or None when the caller passed an explicit run name (we
    don't try to classify arbitrary IP / OOC runs into a kind).
    """
    if (kind is None) == (run is None):
        return None, None, fail(
            "bad_arg",
            "Pass exactly one of kind=<'synthesis'|'implementation'> "
            "or run=<run_name>.",
        )
    if run is not None:
        return run, None, None
    if kind == "synthesis":
        name = _active_synth_run(client)
    elif kind == "implementation":
        name = _active_impl_run(client)
    else:
        return None, None, fail(
            "bad_arg",
            f"kind must be 'synthesis' or 'implementation', got {kind!r}",
        )
    if not name:
        return None, None, fail(
            "not_found",
            f"No active {kind} run in the current project.",
        )
    return name, kind, None


def get_run_status(
    client,
    *,
    kind: str | None = None,
    run: str | None = None,
) -> dict[str, Any]:
    """Read STATUS / PROGRESS / NEEDS_REFRESH for a specific run.

    Pass exactly one of:
      kind="synthesis" | "implementation"  -- resolves to the project's
          current_run -synthesis / -implementation (typically synth_1 /
          impl_1).
      run="<name>"                         -- any run in the project,
          including per-IP OOC runs like "vio_0_synth_1".

    Cheap call; safe to poll. Returned fields:
      run               -- the run name resolved (e.g. "impl_1")
      kind              -- the kind label when resolved by kind=,
                            otherwise None (explicit run= bypasses it)
      status            -- e.g. "synth_design Complete!", "Running route_design..."
      progress          -- "100%" / "20%" / ...
      needs_refresh     -- "0" / "1" (string from Vivado)
      is_complete       -- bool, True iff status contains "complete" (case-insensitive)
      is_failed         -- bool, True iff status contains "error" or "failed"
    """
    name, kind_label, err = _resolve_run(client, kind=kind, run=run)
    if err is not None:
        return err

    status = query_one(client, f"get_property STATUS [get_runs {name}]")
    if status is None:
        return fail("not_found", f"Run '{name}' not found in current project.")

    progress = query_one(client, f"get_property PROGRESS [get_runs {name}]") or ""
    needs_refresh = query_one(client, f"get_property NEEDS_REFRESH [get_runs {name}]") or ""

    lower = status.lower()
    return ok(
        f"{name}: {status} ({progress})",
        kind=kind_label,
        run=name,
        status=status,
        progress=progress,
        needs_refresh=needs_refresh,
        is_complete=("complete" in lower),
        is_failed=("error" in lower or "failed" in lower),
    )


def wait_for_run(
    client,
    *,
    kind: str | None = None,
    run: str | None = None,
    timeout: float = 1800.0,
    poll: float = 30.0,
    log: bool = True,
) -> dict[str, Any]:
    """Poll a run until it completes, fails, or the timeout expires.

    Pass exactly one of `kind` or `run` -- see `get_run_status` for the
    resolution rules. The explicit `run=` form is what you want for IP
    OOC runs (e.g. `vio_0_synth_1`); they are not exposed by
    `current_run -synthesis`.

    Vivado's `wait_on_run` blocks the Tcl interpreter and would freeze
    the whole bridge for the duration of the build, so we poll instead.
    """
    name, _kind_label, err = _resolve_run(client, kind=kind, run=run)
    if err is not None:
        return err

    deadline = time.time() + timeout
    last_progress = None
    while True:
        status = get_run_status(client, run=name)
        if not status["success"]:
            return status
        if log and status["progress"] != last_progress:
            last_progress = status["progress"]
            print(f"  [{name}] {status['status']:<35s} {status['progress']}")
        if status["is_complete"]:
            return ok(f"{name} complete", run=name, status=status["status"])
        if status["is_failed"]:
            return fail(
                "run_failed",
                f"{name} failed: {status['status']}",
                run=name,
                status=status["status"],
            )
        if time.time() >= deadline:
            return fail(
                "timeout",
                f"{name} did not finish within {timeout:.0f}s "
                f"(last status: {status['status']}).",
                run=name,
                status=status["status"],
            )
        time.sleep(poll)


# ---------------------------------------------------------------------------
# launchers
# ---------------------------------------------------------------------------

def _attach_diagnostics(client, result: dict[str, Any], *, kind: str) -> dict[str, Any]:
    """Mix diagnostic counts into a launcher result.

    Adds a `diagnostics` sub-dict and, if there are any critical
    warnings or errors, surfaces a one-line note in `warnings` so the
    caller doesn't have to inspect `diagnostics` to notice. The
    `success` flag itself is left to the caller -- Vivado already flags
    error-level run failures via STATUS, which `wait_for_run` picks up.
    """
    diag_resp = get_run_diagnostics(client, kind=kind)
    if not diag_resp["success"]:
        # Diagnostics lookup failed for some reason (no log yet, etc.).
        # Don't lose that, but don't poison the launcher result either.
        result["diagnostics"] = {
            "available": False,
            "message": diag_resp.get("message", ""),
        }
        return result

    diag = {
        "available": True,
        "error_count": diag_resp["error_count"],
        "critical_warning_count": diag_resp["critical_warning_count"],
        "warning_count": diag_resp["warning_count"],
        "first_errors": diag_resp["first_errors"],
        "first_critical_warnings": diag_resp["first_critical_warnings"],
        "first_warnings": diag_resp["first_warnings"],
        "log_path": diag_resp["log_path"],
    }
    result["diagnostics"] = diag

    # Surface the first concrete line of each severity in `warnings` so
    # the caller doesn't have to inspect the diagnostics dict to notice
    # what broke. Anything beyond the preview is in the diagnostics
    # block / in the log on disk.
    notes: list[str] = []
    if diag["error_count"]:
        head = diag["first_errors"][0] if diag["first_errors"] else "(no preview)"
        notes.append(
            f"{diag['error_count']} ERROR line(s); first: {head}"
        )
    if diag["critical_warning_count"]:
        head = (diag["first_critical_warnings"][0]
                if diag["first_critical_warnings"] else "(no preview)")
        notes.append(
            f"{diag['critical_warning_count']} CRITICAL WARNING(s); first: {head}"
        )
    if diag["warning_count"]:
        head = diag["first_warnings"][0] if diag["first_warnings"] else "(no preview)"
        notes.append(
            f"{diag['warning_count']} WARNING(s); first: {head}"
        )
    if notes:
        notes.append(f"(full log: {diag['log_path']})")
        existing = result.get("warnings") or []
        result["warnings"] = list(existing) + notes
    # Drain Vivado console warnings the client accumulated across the
    # synth/impl-driven exec_tcl calls. Without this, an end-user
    # WARNING that fired during the run (e.g. a clock-pin mismatch)
    # would only land on the individual exec_tcl Response that
    # triggered it and never on the launcher's result dict.
    return attach_console_warnings(result, client)


def _attach_timing_summary(
    client, result: dict[str, Any], *, run: str,
) -> dict[str, Any]:
    """Mix WNS / TNS / met_timing into an implementation launcher result.

    Important: timing failure does NOT flip `success` to False. A
    bitstream produced by a build with negative WNS still loads onto
    hardware -- it just won't run reliably at the target clock. The
    caller decides what to do (re-pipeline, drop the clock, accept the
    risk for a quick demo). We surface `met_timing=False` plus a
    prominent `warnings` entry, and leave the success/failure axis
    aligned with "did the launch produce its expected output".
    """
    run_dir = query_one(client, f"get_property DIRECTORY [get_runs {run}]")
    if not run_dir:
        result["wns"] = None
        result["tns"] = None
        result["met_timing"] = None
        result["timing_report_path"] = None
        return result
    timing = _timing_summary_for_run_dir(Path(run_dir))
    result.update(timing)

    notes: list[str] = []
    if timing["timing_report_path"] is None and result.get("is_complete"):
        notes.append(
            "could not locate *_timing_summary_routed.rpt in the impl dir; "
            "WNS/TNS not available"
        )
    elif timing["wns"] is None and timing["timing_report_path"] is not None:
        notes.append(
            f"could not parse timing summary at {timing['timing_report_path']}; "
            "WNS/TNS not available"
        )
    elif timing["met_timing"] is False:
        notes.append(
            f"TIMING FAILED: WNS={timing['wns']:.3f}ns, "
            f"TNS={timing['tns']:.3f}ns. The bitstream still exists, but "
            f"the design will not run reliably at the target clock. "
            f"Re-pipeline, lower the clock, or inspect "
            f"{timing['timing_report_path']}."
        )
    if notes:
        existing = result.get("warnings") or []
        result["warnings"] = list(existing) + notes
    return result


def _ip_runs_needing_synth(client) -> list[str]:
    """Return IP names whose OOC synth run is not yet complete.

    Mirrors what the Vivado GUI does on "Run Synthesis": before the
    parent `synth_1` is launched, any IP in the project that has
    `GENERATE_SYNTH_CHECKPOINT=true` and a synth run that is not
    `synth_design Complete!` (or whose run object does not yet exist
    via `create_ip_run`) needs to be OOC-synthesized first. Without
    this step the parent synth either spins up an implicit IP synth
    (slow, hard to attribute) or, worse, drops the IP as a black box
    and silently produces an unconnected design.

    Returns IP instance names (e.g. "vio_0"); not run names.
    """
    ips_raw = (query_one(client, "get_ips -quiet") or "").strip()
    if not ips_raw:
        return []
    needing: list[str] = []
    for ip in ips_raw.split():
        gen_ckpt = (query_one(
            client,
            f"get_property GENERATE_SYNTH_CHECKPOINT "
            f"[get_files -quiet [get_property IP_FILE [get_ips {ip}]]]",
        ) or "").strip().lower()
        if gen_ckpt not in ("1", "true"):
            continue  # IP is set to be globally synth'd by the parent
        run_name = f"{ip}_synth_1"
        run_status = (query_one(
            client,
            f"get_property STATUS [get_runs -quiet {run_name}]",
        ) or "").strip()
        # No run object yet -- create_ip_run hasn't been called.
        if not run_status:
            needing.append(ip)
            continue
        # Run exists but not complete.
        # "Using cached IP results" means Vivado has a fresh OOC dcp
        # and will reuse it without re-running synth -- this is a
        # success state for our purposes, so don't queue a re-synth.
        # Without this, we ask Vivado to launch a run it has no
        # intention of actually starting, and the inline poll in
        # `synthesize_ip` then hangs waiting for "Complete" until the
        # 600s timeout fires.
        status_lower = run_status.lower()
        if "complete" in status_lower or "cached" in status_lower:
            continue
        needing.append(ip)
    return needing


def synthesize_ip(
    client,
    *,
    ip: str,
    jobs: int = 4,
    timeout: float = 600.0,
    reset: bool = True,
) -> dict[str, Any]:
    """Out-of-context synthesize a single IP and wait for completion.

    Used by `synthesize(auto_synth_ips=True)` (the default) and exposed
    here so callers can drive the OOC synth path manually too. Handles
    the `create_ip_run` precondition that catches AI assistants out --
    `launch_runs <ip>_synth_1` fails with `[Common 17-162]` if the
    run object hasn't been created yet, even though the GUI does it
    transparently.

    Args:
        ip: IP instance name (e.g. "vio_0").
        jobs: parallel jobs for the OOC synth.
        timeout: total seconds to wait.
        reset: call `reset_run` first if the run already exists. False
            if you know it has not been launched yet (saves one Tcl
            round trip).

    Returns the usual ok/fail dict plus:
        ip:        the IP name.
        run:       the OOC synth run name (e.g. "vio_0_synth_1").
        status:    the final run STATUS string.
        elapsed_s: wall-clock seconds.
    """
    import time
    start_t = time.time()
    run_name = f"{ip}_synth_1"
    run_status = (query_one(
        client, f"get_property STATUS [get_runs -quiet {run_name}]",
    ) or "").strip()
    if not run_status:
        # Need create_ip_run first. The GUI does this implicitly.
        rcr = client.exec_tcl(f"create_ip_run [get_ips {ip}]", timeout=60)
        if not rcr.success:
            return from_tcl_failure(rcr, client=client)
    elif reset:
        rr = client.exec_tcl(f"reset_run {run_name}", timeout=60)
        if not rr.success:
            return from_tcl_failure(rr, client=client)

    rl = client.exec_tcl(
        f"launch_runs {run_name} -jobs {jobs}", timeout=120,
    )
    if not rl.success:
        return from_tcl_failure(rl, client=client)

    # Inline poll (avoid wait_for_run's synth_1/impl_1 assumptions).
    # "Using cached IP results" is also a terminal state: Vivado
    # decided the existing OOC dcp is fresh and skipped the run.
    # Without this guard the loop would hang until `timeout` fires.
    deadline = start_t + timeout
    final_status = ""
    while time.time() < deadline:
        s = (query_one(
            client, f"get_property STATUS [get_runs {run_name}]",
        ) or "").strip()
        final_status = s
        s_lower = s.lower()
        if (
            "complete" in s_lower
            or "cached" in s_lower
            or "error" in s_lower
            or "aborted" in s_lower
        ):
            break
        time.sleep(2.0)
    else:
        return fail(
            "timeout",
            f"OOC synth of {ip} did not complete within {timeout:.0f}s "
            f"(last status: {final_status!r}).",
            client=client,
            ip=ip, run=run_name, status=final_status,
            elapsed_s=time.time() - start_t,
        )

    if "ERROR" in final_status or "Aborted" in final_status:
        return fail(
            "run_failed",
            f"OOC synth of {ip} ended with status {final_status!r}.",
            client=client,
            ip=ip, run=run_name, status=final_status,
            elapsed_s=time.time() - start_t,
        )
    return ok(
        f"{ip} OOC synth complete ({final_status})",
        client=client,
        ip=ip, run=run_name, status=final_status,
        elapsed_s=time.time() - start_t,
    )


def synthesize(
    client,
    *,
    jobs: int = 8,
    timeout: float = 1800.0,
    reset: bool = True,
    wait: bool = True,
    auto_synth_ips: bool = True,
) -> dict[str, Any]:
    """Reset and launch the active synthesis run, optionally waiting.

    Mirrors what the Vivado GUI does on "Run Synthesis":
      - if `auto_synth_ips=True` (default), any IP in the project
        with `GENERATE_SYNTH_CHECKPOINT=true` whose OOC synth has not
        completed is synthesized first via `synthesize_ip`. This
        catches the create_ip_run / launch_runs <ip>_synth_1 sequence
        that the Vivado GUI handles transparently and that AI
        assistants (and humans) routinely miss when driving Vivado
        from Tcl.
      - then the parent `synth_1` is reset (if reset=True) and
        launched.

    On wait=True the returned dict includes `diagnostics` (counts and
    a small preview of ERROR / CRITICAL WARNING / WARNING in runme.log
    plus its path) so the caller can decide what, if anything, to
    inspect further. Critical issues are also summarised in `warnings`.

    Top-module change detection: Vivado will silently change the
    project's `top` property if it can't find the configured top in
    the source files (e.g. if the user renamed the module without
    updating the top setting). We compare the top before and after
    the run and add a `warnings` entry if it changed -- otherwise
    a successful synth on the *wrong* design would look identical to
    a successful synth on the right one.

    Args:
        jobs: parallel jobs for `launch_runs`.
        timeout: total seconds to wait if wait=True.
        reset: call `reset_run` first (default True; required if the run
            was previously completed).
        wait: poll until the run completes / fails / times out. If False,
            return immediately after launching (and skip diagnostics).
        auto_synth_ips: pre-synthesize any IP whose OOC dcp is missing
            before launching the parent synth (see above). Set False
            if you want to reproduce the bare `launch_runs synth_1`
            behaviour exactly (e.g. for debugging the IP synth flow
            itself).
    """
    run = _active_synth_run(client)
    if not run:
        return fail("not_found", "No active synthesis run in the current project.")

    top_before = query_one(client, "get_property top [current_fileset]") or ""

    # Pre-step: out-of-context synth any IPs that need it. The GUI
    # does this transparently; the Tcl `launch_runs synth_1` does not.
    ip_warnings: list[str] = []
    if auto_synth_ips:
        needing = _ip_runs_needing_synth(client)
        for ip in needing:
            r_ip = synthesize_ip(client, ip=ip, jobs=max(jobs // 2, 2))
            if not r_ip["success"]:
                # Surface the IP-level failure verbatim; the parent
                # synth would have failed (or silently dropped the IP)
                # anyway. Better to stop here with a clear message
                # naming the IP than to continue and confuse triage.
                return fail(
                    r_ip.get("error_kind") or "ip_synth_failed",
                    f"OOC synth of IP {ip!r} failed before parent "
                    f"synth: {r_ip['message']}",
                    client=client,
                    ip=ip,
                    ip_run=r_ip.get("run"),
                    ip_status=r_ip.get("status"),
                )
            ip_warnings.append(
                f"[bridge] auto-synthesized IP {ip} "
                f"({r_ip.get('elapsed_s', 0):.1f}s)"
            )

    if reset:
        r = client.exec_tcl(f"reset_run {run}", timeout=60)
        if not r.success:
            return from_tcl_failure(r, client=client)

    r = client.exec_tcl(f"launch_runs {run} -jobs {jobs}", timeout=120)
    if not r.success:
        return from_tcl_failure(r, client=client)

    if not wait:
        out = ok(f"{run} launched", run=run, launched=True, waited=False)
        if ip_warnings:
            out["warnings"] = ip_warnings + (out.get("warnings") or [])
        return out

    waited = wait_for_run(client, kind="synthesis", timeout=timeout)
    waited["launched"] = True
    waited["waited"] = True
    if ip_warnings:
        # Prepend so the auto-IP-synth notes are visible above the
        # parent-synth diagnostics; helps callers see which IPs got
        # built ahead of time.
        waited["warnings"] = ip_warnings + (waited.get("warnings") or [])

    # Detect silent top mutation. Reading `top` is cheap, and if Vivado
    # picked a different module on us we want that surfaced loudly.
    top_after = query_one(client, "get_property top [current_fileset]") or ""
    if top_before and top_after and top_before != top_after:
        existing = waited.get("warnings") or []
        existing.append(
            f"Project 'top' changed during synthesis: "
            f"{top_before!r} -> {top_after!r}. Vivado does this silently "
            f"when the configured top isn't found in the sources. The "
            f"build succeeded but you may have synthesized a different "
            f"design than you expected."
        )
        waited["warnings"] = existing
        waited["top_before"] = top_before
        waited["top_after"] = top_after

    return _attach_diagnostics(client, waited, kind="synthesis")


def implement(
    client,
    *,
    jobs: int = 8,
    timeout: float = 3600.0,
    reset: bool = True,
    wait: bool = True,
    generate_bitstream: bool = True,
) -> dict[str, Any]:
    """Reset and launch the active implementation run.

    Same diagnostic-attaching behaviour as `synthesize` -- on wait=True
    the result carries a `diagnostics` block and surfaces any critical
    log lines via `warnings`.

    Pre-flight: if the upstream synthesis run is in an Error/Failed
    state, Vivado refuses to launch impl with a `[Common 17-70]`
    Tcl error. We detect that ahead of time and return
    `error_kind="upstream_failed"` so the caller gets a guided
    "synthesize first" message instead of a raw stack trace.
    """
    run = _active_impl_run(client)
    if not run:
        return fail("not_found", "No active implementation run in the current project.")

    synth_run = _active_synth_run(client)
    if synth_run:
        synth_status = query_one(
            client, f"get_property STATUS [get_runs {synth_run}]"
        ) or ""
        lower = synth_status.lower()
        if "error" in lower or "failed" in lower:
            return fail(
                "upstream_failed",
                f"Upstream synthesis run '{synth_run}' is in a failed state "
                f"({synth_status}). Run synthesize(reset=True) first, then "
                f"call implement() again.",
                upstream_run=synth_run,
                upstream_status=synth_status,
            )

    if reset:
        r = client.exec_tcl(f"reset_run {run}", timeout=60)
        if not r.success:
            return from_tcl_failure(r, client=client)

    to_step = "write_bitstream" if generate_bitstream else "route_design"
    r = client.exec_tcl(
        f"launch_runs {run} -to_step {to_step} -jobs {jobs}",
        timeout=120,
    )
    if not r.success:
        return from_tcl_failure(r, client=client)

    if not wait:
        return ok(f"{run} launched", run=run, launched=True, waited=False)

    waited = wait_for_run(client, kind="implementation", timeout=timeout)
    waited["launched"] = True
    waited["waited"] = True
    waited = _attach_diagnostics(client, waited, kind="implementation")
    return _attach_timing_summary(client, waited, run=run)


# ---------------------------------------------------------------------------
# log / error inspection
# ---------------------------------------------------------------------------

def get_run_log_path(client, *, kind: str = "implementation") -> dict[str, Any]:
    """Resolve the `runme.log` path for the active synth/impl run.

    The file itself can be huge; this operation just gives you the path
    so you can read or grep it host-side.
    """
    if kind == "synthesis":
        run = _active_synth_run(client)
    elif kind == "implementation":
        run = _active_impl_run(client)
    else:
        return fail("bad_arg", f"kind must be 'synthesis' or 'implementation', got {kind!r}")
    if not run:
        return fail("not_found", f"No active {kind} run.")
    run_dir = query_one(client, f"get_property DIRECTORY [get_runs {run}]")
    if not run_dir:
        return fail("not_found", f"Cannot resolve directory for run '{run}'.")
    log_path = Path(run_dir) / "runme.log"
    return ok(
        f"{run} log path",
        run=run,
        run_dir=str(Path(run_dir)),
        log_path=str(log_path),
        log_exists=log_path.exists(),
    )


def _scan_diagnostics(log_path: Path, *, sample_size: int = 5) -> dict[str, Any]:
    """Tally ERROR / CRITICAL WARNING / WARNING lines in a runme.log,
    plus capture the first few of each as a *preview* sample.

    We never return the full list -- a small typo in a constraints file
    can produce hundreds of cascading entries, which would blow out a
    caller's context window. The preview is enough for at-a-glance
    triage; detail consumers Read / Grep the log path themselves.

    Each preview line is truncated to 250 characters so a runaway log
    line doesn't consume the response.
    """
    result: dict[str, Any] = {
        "errors": 0,
        "critical_warnings": 0,
        "warnings": 0,
        "first_errors": [],
        "first_critical_warnings": [],
        "first_warnings": [],
    }
    if not log_path.exists():
        return result

    max_line_len = 250
    for raw in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.rstrip()
        # Match Vivado's actual severity prefixes at the start of the
        # line. Prefix-anchored matching avoids picking up substrings
        # like "CRITICAL_WARNING" mentioned inside an INFO line, which
        # an unanchored "in line" check would misclassify.
        if line.startswith("ERROR:"):
            result["errors"] += 1
            if len(result["first_errors"]) < sample_size:
                result["first_errors"].append(line[:max_line_len])
        elif line.startswith("CRITICAL WARNING:"):
            result["critical_warnings"] += 1
            if len(result["first_critical_warnings"]) < sample_size:
                result["first_critical_warnings"].append(line[:max_line_len])
        elif line.startswith("WARNING:"):
            result["warnings"] += 1
            if len(result["first_warnings"]) < sample_size:
                result["first_warnings"].append(line[:max_line_len])
    return result


def get_run_diagnostics(
    client,
    *,
    kind: str = "implementation",
    sample_size: int = 5,
) -> dict[str, Any]:
    """Count ERROR / CRITICAL WARNING / WARNING lines in `runme.log`,
    plus return up to `sample_size` of each as a preview.

    Returns counts and previews plus the log path. The preview keeps the
    response small while letting the caller see *what* went wrong
    without opening the log. For full detail, `Read` / `Grep` the
    `log_path` host-side.
    """
    info = get_run_log_path(client, kind=kind)
    if not info["success"]:
        return info
    run = info["run"]
    log_path = Path(info["log_path"])

    if not log_path.exists():
        return ok(
            f"{run}: no log yet",
            run=run,
            log_path=str(log_path),
            log_exists=False,
            error_count=0,
            critical_warning_count=0,
            warning_count=0,
            first_errors=[],
            first_critical_warnings=[],
            first_warnings=[],
        )

    diag = _scan_diagnostics(log_path, sample_size=sample_size)
    msg = (
        f"{run}: errors={diag['errors']}, "
        f"critical_warnings={diag['critical_warnings']}, "
        f"warnings={diag['warnings']}"
    )
    return ok(
        msg,
        run=run,
        log_path=str(log_path),
        log_exists=True,
        error_count=diag["errors"],
        critical_warning_count=diag["critical_warnings"],
        warning_count=diag["warnings"],
        first_errors=diag["first_errors"],
        first_critical_warnings=diag["first_critical_warnings"],
        first_warnings=diag["first_warnings"],
    )


# ---------------------------------------------------------------------------
# bitstream
# ---------------------------------------------------------------------------

def find_bitstream(client) -> dict[str, Any]:
    """Locate the latest .bit and .ltx (if any) for the active impl run.

    Returns {bit_path, ltx_path, bit_exists, ltx_exists}. ltx_path is
    None if no .ltx was generated (which is fine for designs without
    VIO/ILA). The intent is to give `program_device` and other
    operations a single source of truth for these paths.
    """
    run = _active_impl_run(client)
    if not run:
        return fail("not_found", "No active implementation run.")
    impl_dir = query_one(client, f"get_property DIRECTORY [get_runs {run}]")
    if not impl_dir:
        return fail("not_found", f"Cannot resolve directory for {run}.")
    d = Path(impl_dir)
    bits = sorted(d.glob("*.bit"))
    ltxs = sorted(d.glob("*.ltx"))
    bit_path = str(bits[0]) if bits else None
    ltx_path = str(ltxs[0]) if ltxs else None
    return ok(
        f"bit={'yes' if bit_path else 'no'}, ltx={'yes' if ltx_path else 'no'}",
        impl_run=run,
        impl_dir=str(d),
        bit_path=bit_path,
        ltx_path=ltx_path,
        bit_exists=bit_path is not None,
        ltx_exists=ltx_path is not None,
    )

# ---------------------------------------------------------------------------
# Synthesized-design lifecycle helpers
# ---------------------------------------------------------------------------

def open_synth(client, *, run: str | None = None) -> dict[str, Any]:
    """Open the synthesized design (`open_run`) so design queries / debug
    insertion / `report_*` work on it.

    `create_debug_core` and friends require an open synthesized design.
    The Vivado Tcl is `open_run synth_1 -name synth_1`; this wrapper
    handles the active-run lookup so callers do not need to know the
    name unless they want a specific one.

    Args:
        run: synth run name (e.g. "synth_1"). When None (default), the
            active synth run is used. When the run is already open,
            this is a no-op.

    Returns the usual ok/fail dict plus:
        run: the run that was opened.
    """
    target = run or _active_synth_run(client)
    if not target:
        return fail("not_found", "No active synthesis run to open.", run=run)
    # Validate the run name up-front so a typo is reported as `not_found`
    # rather than leaking through as Vivado's raw "Invalid option value"
    # tcl_error from open_run. Vivado lists existing runs with get_runs.
    if run is not None:
        existing = (query_one(client, "get_runs -quiet") or "").split()
        if target not in existing:
            return fail(
                "not_found",
                f"Run {target!r} not found. Available: {existing}",
                run=target,
                runs=existing,
            )
    # Skip if already open under the same name.
    cur = (query_one(client, "current_run -synthesis -quiet") or "").strip()
    open_label = (query_one(client, "current_design -quiet") or "").strip()
    if cur == target and open_label:
        return ok(f"{target} already open as {open_label!r}", run=target)
    r = client.exec_tcl(f"open_run {target} -name {target}")
    if not r.success:
        return from_tcl_failure(r, client=client, run=target)
    return ok(f"opened {target}", client=client, run=target)


def close_design(client) -> dict[str, Any]:
    """Close the currently open design (synthesized or implemented).

    Pair with `open_synth` after `create_debug_core` / `save_constraints`
    so the next `synth_design` / `launch_runs synth_1` does not collide
    with an already-open design.

    Idempotent: if no design is open the call still returns success
    with `was_open=False`, matching `open_synth`'s "no-op if already
    open" behaviour. This makes "guarantee no design is open" callable
    in one line without the caller having to pre-check.
    """
    open_label = (query_one(client, "current_design -quiet") or "").strip()
    if not open_label:
        return ok("no design was open", client=client, was_open=False)
    r = client.exec_tcl("close_design")
    if not r.success:
        return from_tcl_failure(r, client=client)
    return ok("design closed", client=client, was_open=True)

