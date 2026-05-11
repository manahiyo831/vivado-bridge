"""Bridge / Vivado-process introspection operations.

Things that are about Vivado itself (rather than projects, hardware, or
debug cores). Right now this is just "where are the per-session log
files Vivado writes to?", but the module is the right home for any
future "what is the bridge / Vivado seeing right now?" helpers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._common import fail, ok, query_one


def get_vivado_logs(client) -> dict[str, Any]:
    """Return the paths of Vivado's per-session log and journal files.

    Vivado writes everything that appears in the Tcl Console -- INFO,
    WARNING, ERROR, command echoes -- to `vivado.log` in its current
    working directory. It also keeps a tighter record of just the Tcl
    commands the session executed in `vivado.jou`. Reading these files
    is the closest thing to "see what's on the Tcl Console" you can do
    from outside Vivado.

    Treat the returned `log_path` as effectively a transcript of the
    Tcl Console for this Vivado session. The two views may not be
    100% bit-identical (Vivado decides which messages to mirror where),
    but in practice they line up closely enough for diagnosis.

    Returns:
        log_path        absolute path to vivado.log (may be huge -- read
                        with Read/Grep host-side, don't slurp it all)
        log_exists      bool
        log_size        bytes, or 0 if the file is missing
        jou_path        absolute path to vivado.jou (Tcl commands only)
        jou_exists      bool
        jou_size        bytes
        cwd             Vivado's current working directory (where the
                        files live)
    """
    cwd = query_one(client, "pwd")
    if not cwd:
        return fail(
            "tcl_error",
            "Could not query Vivado pwd; can't locate vivado.log/jou.",
        )
    base = Path(cwd)
    log_path = base / "vivado.log"
    jou_path = base / "vivado.jou"

    log_exists = log_path.exists()
    jou_exists = jou_path.exists()
    log_size = log_path.stat().st_size if log_exists else 0
    jou_size = jou_path.stat().st_size if jou_exists else 0

    return ok(
        f"log={'yes' if log_exists else 'no'}({log_size}B) "
        f"jou={'yes' if jou_exists else 'no'}({jou_size}B) at {cwd}",
        cwd=str(base),
        log_path=str(log_path),
        log_exists=log_exists,
        log_size=log_size,
        jou_path=str(jou_path),
        jou_exists=jou_exists,
        jou_size=jou_size,
    )
