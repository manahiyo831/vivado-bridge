# project operations

All operations are invoked via the `vivado_op.py` JSON dispatcher.
See [SKILL.md](../SKILL.md) for the invocation pattern.

Project-level operations: read the open project's metadata, and
add files / set the top module / refresh compile order via
`project.add_sources`. Creating / deleting projects is still done
through `exec_tcl` (the typical session uses one project for a
long time, so a dedicated helper would not pay for itself).

## Common shape

All operations return a dict with `success`, `error_kind`, `message`,
`warnings` -- the same shape as the rest of the bridge's operations.
See [op_build.md](op_build.md) for details on the standard fields.

## Operations

### project.info

One-shot snapshot of the project's static metadata. Useful as the
first call in a session: lets the caller see what's open, what part
it targets, whether a board file is attached, the current top, file
counts, and run names -- without making half a dozen separate
queries.

Request:

```json
{"op": "project.info", "params": {}}
```

Response:

```json
{
  "success": true,
  "message": "project_1: part=xc7z020clg400-1, board_part=<unset>, top=led_blink, sources=2, runs=4",
  "name": "project_1",
  "directory": "D:/work/project_1",
  "part": "xc7z020clg400-1",
  "board_part": null,
  "top": "led_blink",
  "source_count": 2,
  "constraint_count": 0,
  "sim_count": 0,
  "runs": ["synth_1", "impl_1", "vio_0_synth_1", "vio_0_impl_1"],
  "warnings": []
}
```

`directory` is the absolute path to the `.xpr`'s parent directory.
`board_part` is `null` when no board file is attached.

### Field details

- `directory` is the absolute path to the directory holding the
  `.xpr`. Useful when callers need to place files alongside the
  project (e.g. `debug.create_ila_core` writes its dedicated XDC
  under `<directory>/<name>.srcs/constrs_1/imports/`).
- `board_part = null` is meaningful -- it indicates the project has
  no `BOARD_PART` set. Pure RTL designs run fine without one; designs
  that touch PS / DDR / Ethernet / SoC need a board file applied
  (see SKILL.md "Note the project's board, not just its part").
- `runs` lists *all* runs in the project, not just the active
  `synth_1` / `impl_1`. Per-IP OOC runs (e.g. `vio_0_synth_1`) and
  any extra strategies the user has set up will appear here. Use
  `build.get_active_runs` if you specifically want the active two.
- `sim_count = null` (vs. `0`) distinguishes "no sim_1 fileset
  exists in this project" from "sim_1 exists but is empty".

### Resilience

If a single property query fails (e.g. a Vivado version that doesn't
expose a particular field), the offending lookup is recorded in
`warnings` and the rest of the snapshot is still returned. The op
fails outright (`error_kind="not_found"`) only when no project is
open at all.

#### Failure modes

| `error_kind` | When |
|---|---|
| `not_found` | No project is open. Open or create a project before calling this op. |

### project.add_sources

Bundle the standard "add files + set top + update compile order"
boilerplate that begins almost every project-driven workflow. Pass
only the file lists you actually have; missing keys default to no-op.

Request:

```json
{"op": "project.add_sources", "params": {
  "hdl":     ["hdl/foo.v", "hdl/bar.v"],
  "constrs": ["xdc/top.xdc"],
  "sim":     ["sim/tb_foo.v"],
  "top":     "foo_top",
  "sim_top": "tb_foo",
  "update_order": true
}}
```

Response:

```json
{
  "success": true,
  "message": "Added 2 HDL, 1 constr, 1 sim file(s); top=foo_top; sim_top=tb_foo",
  "hdl_added":     ["hdl/foo.v", "hdl/bar.v"],
  "constrs_added": ["xdc/top.xdc"],
  "sim_added":     ["sim/tb_foo.v"],
  "top":     "foo_top",
  "sim_top": "tb_foo",
  "warnings": []
}
```

Parameters (all optional):

- `hdl`           paths added to fileset `sources_1` (RTL design).
- `constrs`       paths added to fileset `constrs_1` (XDC etc.).
- `sim`           paths added to fileset `sim_1` (testbenches /
                  sim-only models). The `sim_1` fileset is created on
                  demand if it does not already exist.
- `top`           if given, sets `top` on `current_fileset` (= the
                  synthesizable top module).
- `sim_top`       if given, sets `top` on `get_filesets sim_1` (= the
                  simulation top module).
- `update_order`  default `true`. When `true`, calls
                  `update_compile_order -fileset sources_1` (and the
                  same for `sim_1` if any sim file or `sim_top` was
                  involved) after the adds.

Paths may be absolute or relative to Vivado's current working
directory (typically the project root). They are forwarded to
`add_files` verbatim, which silently tolerates re-adds of files
already in the fileset.

#### Notes

- The op records a `warnings` entry for any **absolute** path that
  does not exist on disk at op time. Relative paths are not checked
  (Vivado resolves them against its own cwd, which the caller may not
  share). A successful op does NOT guarantee that `top` resolves to
  a real module — that check happens at elaboration / synth time.
- Re-running this op with the same file lists is safe; Vivado treats
  duplicates as no-ops.

#### Failure modes

| `error_kind` | When |
|---|---|
| `no_project`  | No project is open. Open or create a project first. |
| `tcl_failure` | A specific Tcl call failed (e.g. invalid path characters). Inspect the surrounding `message` for the offending command. |
