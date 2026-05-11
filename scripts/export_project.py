"""Export the open Vivado project as a re-creation recipe.

Drives `write_project_tcl` (and `write_bd_tcl` for any block designs)
through the bridge so that an AI or a user can commit the generated
TCL alongside HDL/XDC sources, then re-create the project on a fresh
machine with `vivado -mode batch -source ...`.

Background: a Vivado project directory is mostly *generated* state --
synth/impl runs, IP-cache contents, simulator artifacts, hardware-
manager state. Trying to track that in git pulls in hundreds of MB
or even GB and produces noisy diffs that don't actually capture the
design. The supported alternative is to track only the recipe (this
script's output), the HDL / XDC / IP `.xci` sources, and a `.gitignore`
that skips the generated directories. See `references/git_management.md`
for the full rationale and the recommended `.gitignore`.

Usage::

    python <bridge>/scripts/export_project.py <output_dir>

`output_dir` is where this script writes:

  <output_dir>/create_project.tcl              -- the project recipe
  <output_dir>/bd/<bd_name>.tcl                -- one per block design

Both are written through the bridge, so Vivado must be open with the
target project loaded and the bridge sourced. This script does not
take any project-state lock -- it just calls `write_project_tcl`
(read-only on the project) and `write_bd_tcl` (also read-only).

Notes on the recipe contents:

- We pass `-no_copy_sources` so the recipe references your real source
  paths (relative to the recipe), not copies inside the project. This
  is what makes the recipe portable across machines.
- `write_project_tcl` writes a header comment listing the source files
  it found. Those lines contain absolute paths from the host where the
  recipe was generated; the executable Tcl below them uses
  `[file normalize "$origin_dir/..."]` which is portable. A comment
  is harmless; if you'd rather not leak a username, scrub the header
  before committing.

Failure modes:

- "No project is open" -- open the project in Vivado first, then
  re-run.
- Output directory missing -- this script doesn't create
  `<output_dir>` for you; pass a path whose parent already exists.
  (Vivado errors out with `Common 17-39` otherwise.)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make `vivado_bridge_client` importable when running this as a script
# regardless of cwd, mirroring connection_check.py / reload_server.py.
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))

from vivado_bridge_client import Client  # noqa: E402


def export_project(client: Client, output_dir: Path) -> dict:
    """Write the project recipe + one TCL per block design into output_dir.

    Returns a dict with `success`, `message`, and a list of files
    written. Mirrors the operations/ result shape so callers don't
    need to special-case this script.
    """
    if not output_dir.exists():
        return {
            "success": False,
            "error_kind": "not_found",
            "message": f"Output directory does not exist: {output_dir}. "
                        "Create it first; this script does not.",
            "files": [],
        }

    # 0. Confirm a project is open.
    r = client.exec_tcl("current_project -quiet")
    if not r.success or not r.output.strip():
        return {
            "success": False,
            "error_kind": "not_found",
            "message": "No project is open in Vivado. Open a project first, "
                        "then re-run this script.",
            "files": [],
        }
    project_name = r.output.strip()

    files_written: list[str] = []

    # 1. write_project_tcl. Vivado wants forward-slash paths.
    recipe_path = (output_dir / "create_project.tcl").as_posix()
    r = client.exec_tcl(
        f"write_project_tcl -force -no_copy_sources {{{recipe_path}}}",
        timeout=120,
    )
    if not r.success:
        return {
            "success": False,
            "error_kind": r.error_kind or "tcl_error",
            "message": f"write_project_tcl failed: {r.message}",
            "error_info": r.error_info,
            "files": files_written,
        }
    files_written.append(recipe_path)

    # 2. write_bd_tcl for each block design, if any.
    bd_dir = output_dir / "bd"
    r = client.exec_tcl(
        "get_files -quiet -filter {FILE_TYPE == \"Block Designs\"}"
    )
    bd_files = (r.output or "").split() if r.success else []
    if bd_files:
        bd_dir.mkdir(parents=True, exist_ok=True)
        for bd_file in bd_files:
            bd_name = Path(bd_file).stem
            bd_tcl = (bd_dir / f"{bd_name}.tcl").as_posix()

            # open_bd_design is required before write_bd_tcl can dump
            # the design, but it's safe to call even when the BD is
            # already current (idempotent in modern Vivado).
            r = client.exec_tcl(f"open_bd_design {{{bd_file}}}", timeout=60)
            if not r.success:
                return {
                    "success": False,
                    "error_kind": r.error_kind or "tcl_error",
                    "message": f"open_bd_design failed for {bd_name}: {r.message}",
                    "error_info": r.error_info,
                    "files": files_written,
                }

            r = client.exec_tcl(
                f"write_bd_tcl -force {{{bd_tcl}}}",
                timeout=60,
            )
            if not r.success:
                return {
                    "success": False,
                    "error_kind": r.error_kind or "tcl_error",
                    "message": f"write_bd_tcl failed for {bd_name}: {r.message}",
                    "error_info": r.error_info,
                    "files": files_written,
                }
            files_written.append(bd_tcl)

    return {
        "success": True,
        "error_kind": None,
        "message": f"exported {project_name}: {len(files_written)} file(s)",
        "files": files_written,
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="Export the open Vivado project as a TCL recipe + per-BD TCL.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="See references/git_management.md for what to do with the recipe "
               "(commit it next to your HDL/XDC, ignore the rest of the "
               "project directory).",
    )
    p.add_argument("output_dir", type=Path,
                   help="Directory to write create_project.tcl (and bd/*.tcl) "
                        "into. Must already exist.")
    args = p.parse_args()

    output_dir = args.output_dir.resolve()
    print(f"Output directory: {output_dir}")

    client = Client.connect()
    result = export_project(client, output_dir)

    if not result["success"]:
        print(f"\nFAILED: [{result['error_kind']}] {result['message']}")
        if result.get("error_info"):
            print(result["error_info"])
        return 1

    print(f"\nOK: {result['message']}")
    for f in result["files"]:
        print(f"  wrote {f}")
    print()
    print("Next: review the generated TCL, then commit it (and the matching")
    print("HDL/XDC sources) per references/git_management.md.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
