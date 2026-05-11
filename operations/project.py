"""Project metadata operations: read-only snapshot of the open project.

This module is the read-only counterpart to the bridge's other "what's
the world look like right now?" calls (`build.summary`,
`hardware.get_hardware_status`). It does not modify the project --
creating projects, adding sources, switching the top module, and
similar editing tasks are deliberately left to the user (or to a raw
`exec_tcl` call when scripted), because the typical workflow is "the
user opens a project in the GUI, then sources the bridge".

If you find yourself wanting `project.add_sources()` or
`project.create()`, you're probably on a path the skill isn't
designed for -- either drop through to `client.exec_tcl(...)` for the
specific Tcl you need, or step back and have the user set up the
project the way they want.
"""

from __future__ import annotations

from typing import Any

from ._common import fail, ok, query_one


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
