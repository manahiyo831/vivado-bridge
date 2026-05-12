# vivado-bridge

[日本語版 README はこちら](README.ja.md)

A SKILL for driving Xilinx Vivado from Claude Code. A small Tcl socket
server runs inside Vivado; from then on Claude Code can issue any Tcl
command against that Vivado instance.

Vivado runs in project mode (its normal GUI). HDL files and other
sources are expected to be edited directly by Claude Code on the host
side, not through the bridge.

**This SKILL was itself built with Claude Code. Use it at your own risk.**

## Highlights

- **Open and editable** — the SKILL is plain `SKILL.md` + Python +
  Tcl that you can read and modify. Tweak it to fit your environment
  and workflow rather than treating it as a black box.
- **Simple wire protocol** — the Tcl server (the Vivado side) just
  evaluates the commands it receives. All command construction lives
  on the Python side, so any Tcl snippet you can write, you can run.
- **Sensible safety defaults** — `.env` pins the bridge to localhost,
  and the Tcl server refuses a small set of dangerous commands
  (`exec`, destructive `file` subcommands).

## What you can do with it

The combination of Claude Code and Tcl is open-ended, but a few
concrete examples to set expectations:

- Standard Vivado workflows: HDL authoring through bitstream, with
  VIO / ILA bring-up.
- Writing testbenches, running them, and checking the results.
- Reviewing HDL files and proposing improvements.
- Analysing reports (timing failures and friends) and suggesting
  fixes.
- Git hygiene around Vivado projects.

## Operations at a glance

The SKILL exposes **45 high-level operations** through a single CLI
entry point: `scripts/vivado_op.py`. Send a JSON request on stdin,
get a JSON response on stdout. The categories:

| Category    | What it covers |
|---|---|
| `project.*`  | Project metadata snapshot |
| `build.*`    | Launching and observing synth/impl runs, bitstream lookup, timing-summary parsing |
| `hardware.*` | Hardware Manager, JTAG target control, device programming |
| `debug.*`    | VIO probe enumeration and read/write, with atomic batch writes; build-time VIO/ILA core helpers |
| `ila.*`      | ILA configure / set_triggers / arm / wait / export CSV / parse |
| `sim.*`      | xsim driver with bounded run loop, simulate.log summary |
| `bridge.*`   | Locating `vivado.log` / `vivado.jou` |

Full detail lives in `references/op_*.md`. Enumerate every registered
op with `python scripts/vivado_op.py --list`.

For raw Tcl when no op covers what you need, `scripts/exec_tcl.py`
is the escape hatch.

## Tested environment

- **Claude Code**  
  Verified on the Max plan. The free or Pro plans may hit usage
  limits quickly during real work.
  Opus 4.7 in Auto mode works comfortably for autonomous runs.

- **Python 3.9+**  
  Standard library only. The screenshot feature needs extra packages
  (see below).

- **Vivado 2024.1 and 2021.1**  
  Verified on both. Other versions are likely to work, though minor
  parameter differences may show up — Claude Code can usually patch
  those for you. The SKILL assumes you have working Vivado/FPGA
  experience and is not aimed at first-time users.

- **Windows 11**  
  Linux / WSL might work but are unverified. The screenshot feature
  is Windows-only.

- **FPGA board: PYNQ-Z1**  
  Other boards should work fine; just ask Claude Code to set up the
  board configuration.

## Getting started

### Install

The SKILL is plain files; place the `vivado-bridge` directory under
Claude Code's skills folder. The usual location is:

  ```text
  <project>/.claude/skills/vivado-bridge/
  ```

**Via git (recommended)**:

```bash
cd <project>/.claude/skills/
git clone https://github.com/manahiyo831/vivado-bridge.git
```

**Manual**: download the ZIP from GitHub and extract its contents to
`<project>/.claude/skills/vivado-bridge/`.

The default `.env` (`127.0.0.1:53729`) is set up for the common case
of one Vivado on the same machine, so it should work as-is.

The screenshot feature is optional and needs two extra packages
(`pywin32`, `Pillow`); install them only if you want Claude Code to
be able to capture the Vivado main window:

```bash
pip install -r requirements-screenshot.txt
```

### Usage

1. Launch Vivado and open a project (existing or new — either is fine).
2. Use the same working directory in Claude Code as your Vivado
   project so that Claude Code can find the project files easily.
   Start Claude Code, then type `/vivado-bridge` to invoke the SKILL.
3. The SKILL runs a connection check. If the Tcl server isn't up yet,
   it will ask you to start it; paste the suggested command into
   Vivado's Tcl Console and run it.
4. When the Tcl server starts, you'll see a banner like this in the
   Tcl Console:

   ```
   ============================================================
   vivado-bridge v0.1.0 started
     Listening on : 127.0.0.1:53729
     Working dir  : ...
   ============================================================
   ```

**Note on first launch**: because the bridge uses TCP, the OS may
show a security warning. Allow it.

That's it. From here, just talk to Claude Code in natural language —
"check the connection", "show the current project state", and so on.


## Known issues

There's no automatic handling for Vivado dialog boxes that pop up on
build errors. In practice the Tcl server keeps responding and Claude
Code's work doesn't stall, but the cleanest approach is to dismiss
the dialog yourself and tell Claude Code that you did so.

We have occasionally seen Vivado's Tcl interpreter become unresponsive
during a session, after which the bridge stops returning replies.
Reproducing this on demand has been hard; the underlying cause is
still under investigation and not yet resolved. If you hit this,
the only known recovery is to force-quit Vivado from Task Manager
and restart it.
