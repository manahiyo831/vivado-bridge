"""Shared helpers used by the operations modules.

Kept private (underscore prefix) -- not part of the public API.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

# Make `vivado_bridge_client` importable when an operation script is run
# as `python -m operations.build` or imported from outside the package.
_BRIDGE_DIR = Path(__file__).resolve().parent.parent
_SCRIPTS_DIR = _BRIDGE_DIR / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))


def _drain_console_warnings(client) -> list[str]:
    """Pull any pending Vivado console warnings off the client.

    Used by `ok` / `fail` / `from_tcl_failure` so that operations
    automatically surface WARNING / CRITICAL WARNING / ERROR lines that
    Vivado wrote to its log during the operation. Without this, those
    messages would only appear on the *individual* exec_tcl Response
    that triggered them, not on the operation's final result dict.

    Safe on clients that haven't loaded the helper (older custom
    clients, mocks, etc.) — we simply skip.
    """
    if client is None:
        return []
    drain = getattr(client, "drain_console_warnings", None)
    if not callable(drain):
        return []
    return list(drain())


def attach_console_warnings(result: dict[str, Any], client) -> dict[str, Any]:
    """Drain any pending Vivado console warnings into `result['warnings']`.

    Long-running operations (build, program_device, ILA capture, sim
    run) call this just before returning their result dict so that any
    WARNING / CRITICAL WARNING / ERROR Vivado wrote during *any* of
    their internal exec_tcl calls is visible to the AI/user — not just
    the warnings from the final exec_tcl on the path.

    Mutates the result dict in place AND returns it (for fluent style).
    Idempotent on result dicts that already have a `warnings` list.
    Safe to call when no warnings are pending; in that case it's a
    no-op.
    """
    pending = _drain_console_warnings(client)
    if not pending:
        return result
    existing = result.get("warnings")
    if existing is None:
        result["warnings"] = pending
    else:
        result["warnings"] = list(existing) + pending
    return result


def ok(message: str = "", *, client: Any = None, **fields: Any) -> dict[str, Any]:
    """Build a success result dict. `warnings` defaults to [].

    Pass `client=` to auto-merge any Vivado console warnings the client
    accumulated during the operation. Operations that don't pass it
    still work, but warnings observed in their underlying exec_tcl
    calls will not surface on the result dict.
    """
    warnings = list(fields.pop("warnings", []))
    warnings.extend(_drain_console_warnings(client))
    out: dict[str, Any] = {
        "success": True,
        "error_kind": None,
        "message": message,
        "warnings": warnings,
    }
    out.update(fields)
    return out


def fail(
    error_kind: str,
    message: str,
    *,
    client: Any = None,
    **fields: Any,
) -> dict[str, Any]:
    """Build a failure result dict. Caller can stash `error_info` etc.

    Pass `client=` to auto-merge any Vivado console warnings the client
    accumulated during the operation (same rationale as `ok`).
    """
    warnings = list(fields.pop("warnings", []))
    warnings.extend(_drain_console_warnings(client))
    out: dict[str, Any] = {
        "success": False,
        "error_kind": error_kind,
        "message": message,
        "warnings": warnings,
    }
    out.update(fields)
    return out


def from_tcl_failure(
    resp,
    *,
    error_kind: str = "tcl_error",
    client: Any = None,
    **identity: Any,
) -> dict[str, Any]:
    """Translate a failed `Client.exec_tcl` Response into a result dict.

    Preserves Tcl's stack trace so the caller can still surface it.
    The console lines Vivado wrote alongside the failure are picked
    up via the client's pending-warnings drain (inside `fail`); we do
    not also copy `resp.console_lines` here, because the same lines
    are present in both places and would double-count otherwise.

    `identity` kwargs (e.g. `run="synth_1"`, `probe="led"`) are
    forwarded into the fail dict so callers don't lose the "what was
    I trying to do" context when the underlying Tcl call failed.
    """
    return fail(
        error_kind=resp.error_kind or error_kind,
        message=resp.message or "Tcl command failed",
        error_info=resp.error_info,
        error_code=resp.error_code,
        blocked_token=resp.blocked_token,
        client=client,
        **identity,
    )


def tcl_str(value: Any) -> str:
    """Render a Python value as a Tcl literal safe for our use cases.

    We do NOT need general-purpose Tcl quoting here -- the bridge passes
    strings via JSON, so curly braces and newlines come through cleanly.
    For the few cases where we need to interpolate paths or names into a
    Tcl command, this just stringifies and converts backslashes to
    forward slashes (Tcl accepts forward slashes on Windows too).
    """
    s = str(value)
    return s.replace("\\", "/")


def query_one(client, tcl: str, *, timeout: float | None = None) -> str | None:
    """Run a single Tcl query and return its stripped output.

    Return value semantics (deliberate three-way distinction):
        None  -- the Tcl call itself failed (Tcl error, bridge error,
                 timeout). The caller has no value to work with and
                 should propagate this as a failure (see the VIO
                 helpers in operations.debug for the pattern).
        ""    -- the Tcl call succeeded and Vivado returned an empty
                 string. This is a real, observable value -- many
                 Vivado properties legitimately read back as "" when
                 unset (e.g. `get_property BOARD_PART [current_project]`
                 on a part-only project). Treating it as failure would
                 lose information and trigger spurious errors.
        "..." -- normal stripped output.

    Callers MUST distinguish None vs "". Using `if not val:` collapses
    them and is almost always a bug -- prefer explicit `if val is None`.
    """
    r = client.exec_tcl(tcl, timeout=timeout)
    if not r.success:
        return None
    return r.output.strip() if r.output else ""
