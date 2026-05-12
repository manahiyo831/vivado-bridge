# project operations

All operations are invoked via the `vivado_op.py` JSON dispatcher.
See [SKILL.md](../SKILL.md) for the invocation pattern.

Read-only metadata snapshot of the open Vivado project. This module
does not edit projects -- creating, adding sources, or switching the
top module are tasks the user typically does in the GUI before
sourcing the bridge. If you need them in code, drop through the
`exec_tcl.py` escape hatch.

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
