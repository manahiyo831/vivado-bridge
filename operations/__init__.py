"""High-level operations for vivado-bridge.

Each module in this package exposes a small set of Python functions that
take a `Client` and orchestrate one or more Tcl calls behind the scenes.
The operations return plain Python dicts -- never serialized JSON --
with a uniform shape:

    {
        "success": bool,            # mandatory
        "warnings": list[str],      # mandatory; empty list if none
        "error_kind": str | None,   # set when success is False
        "message": str,             # one-line summary
        ...                          # operation-specific fields
    }

Design rules:

- Operations target the project's *default* runs (synth_1 / impl_1).
  Multi-run setups go through `client.exec_tcl(...)` directly. This
  keeps the API surface small and avoids tempting AI agents into
  spawning extra runs by mistake.
- Heavy report contents are NEVER returned inline. Operations either
  return parsed metadata (when small) or just the file path; the caller
  reads the file with their own host-side tools (Read / Grep).
- Tcl errors are not re-raised; they are captured into the returned
  dict so callers can branch on `success` without try/except.
"""

from . import build, hardware, debug, bridge, project, ila, sim

__all__ = ["build", "hardware", "debug", "bridge", "project", "ila", "sim"]
