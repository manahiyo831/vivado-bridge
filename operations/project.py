"""Project operations: metadata snapshot and source-file management.

The original assumption was "the user opens a project in the GUI;
the bridge only reads it." In practice, Python-driven trials run the
same `add_files` + `set_property top` + `update_compile_order`
boilerplate at the start of every project, so `add_sources` is
included here as a thin wrapper to remove that recurring friction.

Creating / deleting projects is still left to `exec_tcl`; the
typical session uses one project for a long time, so the up-front
`create_project` cost is paid once and a dedicated op would not
pay for itself.
"""

from __future__ import annotations

import os
from typing import Any

from ._common import fail, ok, query_one, tcl_str


def info(client) -> dict[str, Any]:
    """One-shot snapshot of the open project's static metadata.

    Useful as the first call in a session: lets the caller see at a
    glance which project is open, what part it targets, whether a
    board file is attached, what the current top is, and how many
    runs / sources are present -- without making half a dozen
    individual exec_tcl calls.

    Returned fields:
      name              -- current_project string
      directory         -- get_property DIRECTORY [current_project]
                            (absolute path to the .xpr's containing
                             directory). Useful for callers that
                             need to place files alongside the
                             project (e.g. dedicated debug XDCs).
      part              -- get_property PART
      board_part        -- get_property BOARD_PART, or None when unset.
                            None is a meaningful signal: PS / DDR /
                            Ethernet designs need a board file; pure
                            RTL designs typically don't.
      top               -- get_property top on current_fileset, or None
      source_count      -- file count in fileset sources_1
      constraint_count  -- file count in fileset constrs_1
      sim_count         -- file count in fileset sim_1, or None when
                            the simulation fileset doesn't exist
      runs              -- list of all run names in the project (not
                            just the active synth_1 / impl_1; includes
                            per-IP OOC runs and any extra strategies
                            the user may have set up)

    Failure modes:
      not_found  -- no project is open. Open or create one before
                    calling this op.

    Any individual property query that fails is recorded in the
    `warnings` list rather than aborting the whole snapshot, so the
    caller still gets the parts that did succeed.
    """
    name = query_one(client, "current_project -quiet")
    if not name:
        return fail(
            "not_found",
            "No project is open. Open or create a project before calling project.info().",
        )

    warnings: list[str] = []

    def _q(tcl: str, label: str) -> str | None:
        v = query_one(client, tcl)
        if v is None:
            warnings.append(f"Could not read {label} ({tcl!r})")
        return v

    directory = _q(
        "get_property DIRECTORY [current_project]", "DIRECTORY",
    ) or None
    part = _q("get_property PART [current_project]", "PART")
    board_part = _q("get_property BOARD_PART [current_project]", "BOARD_PART") or None
    top = _q("get_property top [current_fileset]", "top") or None

    source_count = _count_files(client, "sources_1", warnings)
    constraint_count = _count_files(client, "constrs_1", warnings)

    sim_fileset = query_one(client, "get_filesets -quiet sim_1") or ""
    sim_count = _count_files(client, "sim_1", warnings) if sim_fileset else None

    runs_raw = query_one(client, "get_runs")
    if runs_raw is None:
        warnings.append("Could not read run list (get_runs)")
        runs: list[str] = []
    else:
        runs = runs_raw.split()

    msg = (
        f"{name}: part={part or '?'}, board_part={board_part or '<unset>'}, "
        f"top={top or '<unset>'}, sources={source_count}, runs={len(runs)}"
    )
    return ok(
        msg,
        name=name,
        directory=directory,
        part=part,
        board_part=board_part,
        top=top,
        source_count=source_count,
        constraint_count=constraint_count,
        sim_count=sim_count,
        runs=runs,
        warnings=warnings,
    )


def _count_files(client, fileset: str, warnings: list[str]) -> int:
    """Count files in a fileset via `llength`. Records a warning and
    returns 0 if Vivado refused the query (e.g. fileset missing).
    """
    raw = query_one(
        client,
        f"llength [get_files -quiet -of_objects [get_filesets {fileset}]]",
    )
    if raw is None or raw == "":
        warnings.append(f"Could not read file count for fileset '{fileset}'")
        return 0
    try:
        return int(raw)
    except ValueError:
        warnings.append(f"Unexpected file count for fileset '{fileset}': {raw!r}")
        return 0


def add_sources(
    client,
    *,
    hdl: list[str] | None = None,
    constrs: list[str] | None = None,
    sim: list[str] | None = None,
    top: str | None = None,
    sim_top: str | None = None,
    update_order: bool = True,
) -> dict[str, Any]:
    """Add files to the open project's filesets and (optionally) set the top.

    Bundles the four-step boilerplate that every project-driven
    workflow repeats:
        add_files -fileset sources_1 ...
        add_files -fileset constrs_1 ...
        add_files -fileset sim_1     ...
        set_property top <top>       [current_fileset]
        set_property top <sim_top>   [get_filesets sim_1]
        update_compile_order -fileset sources_1
        update_compile_order -fileset sim_1

    All file lists are optional; pass only the ones you need. Paths
    may be absolute or relative to Vivado's current working directory
    (typically the project root). They are forwarded to `add_files`
    verbatim, which accepts either form and tolerates duplicates
    silently (a re-add of an already-present file is a no-op).

    Parameters:
      hdl           paths to add to fileset sources_1 (RTL design files).
      constrs       paths to add to fileset constrs_1 (XDC etc.).
      sim           paths to add to fileset sim_1 (testbenches and
                    sim-only models). The sim_1 fileset is created on
                    demand by Vivado the first time you reference it,
                    so this works even on a fresh project.
      top           if given, sets `top` on current_fileset (= the
                    synthesizable top module name, e.g. "foo_top").
      sim_top       if given, sets `top` on get_filesets sim_1 (= the
                    simulation top module, e.g. "tb_foo").
      update_order  default True. When True, calls
                    `update_compile_order -fileset sources_1` and the
                    same for sim_1 after the adds so Vivado's compile
                    order reflects the new files. Set False only if
                    you want to control ordering manually.

    Returned fields:
      hdl_added     list of paths actually passed to add_files for sources_1
      constrs_added same for constrs_1
      sim_added     same for sim_1
      top           the value passed in, echoed back, or None
      sim_top       the value passed in, echoed back, or None
      warnings      file paths that did not exist on disk at op time
                    (Vivado would still accept them as "to-be-created"
                    references, but this almost always indicates a typo;
                    surfacing it as a warning catches the typical case
                    without rejecting unusual ones). Plus anything the
                    Tcl Console emitted during the adds (drained by the
                    standard warnings-attach mechanism).

    Failure modes:
      no_project       no project is open. Open or create one first.
      tcl_failure      a specific Tcl call failed (e.g. invalid path
                       characters, set_property top on a missing module
                       name — the latter is reported by Vivado at
                       elaboration / synth time, not add_files time, so
                       a successful add_sources does NOT guarantee the
                       top resolves).
    """
    if not query_one(client, "current_project -quiet"):
        return fail(
            "no_project",
            "No project is open. Open or create a project before calling project.add_sources().",
        )

    hdl_list = list(hdl or [])
    constrs_list = list(constrs or [])
    sim_list = list(sim or [])

    warnings: list[str] = []

    def _existence_check(paths: list[str], label: str) -> None:
        for p in paths:
            if not os.path.isabs(p):
                continue
            if not os.path.exists(p):
                warnings.append(f"{label} path does not exist on disk: {p}")

    _existence_check(hdl_list, "hdl")
    _existence_check(constrs_list, "constrs")
    _existence_check(sim_list, "sim")

    def _add(fileset: str, paths: list[str]) -> None:
        if not paths:
            return
        quoted = " ".join(tcl_str(p) for p in paths)
        client.exec_tcl(f"add_files -quiet -fileset {fileset} {quoted}")

    _add("sources_1", hdl_list)
    _add("constrs_1", constrs_list)
    if sim_list:
        sim_fileset_exists = query_one(client, "get_filesets -quiet sim_1") or ""
        if not sim_fileset_exists:
            client.exec_tcl("create_fileset -simset sim_1")
        _add("sim_1", sim_list)

    if top is not None:
        client.exec_tcl(f"set_property top {tcl_str(top)} [current_fileset]")
    if sim_top is not None:
        client.exec_tcl(
            f"set_property top {tcl_str(sim_top)} [get_filesets sim_1]"
        )

    if update_order:
        client.exec_tcl("update_compile_order -fileset sources_1")
        if sim_list or sim_top is not None:
            client.exec_tcl("update_compile_order -fileset sim_1")

    msg = (
        f"Added {len(hdl_list)} HDL, {len(constrs_list)} constr, "
        f"{len(sim_list)} sim file(s)"
    )
    if top is not None:
        msg += f"; top={top}"
    if sim_top is not None:
        msg += f"; sim_top={sim_top}"

    return ok(
        msg,
        client=client,
        hdl_added=hdl_list,
        constrs_added=constrs_list,
        sim_added=sim_list,
        top=top,
        sim_top=sim_top,
        warnings=warnings,
    )
