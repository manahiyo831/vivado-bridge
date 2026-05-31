#!/usr/bin/env python3
"""JSON command dispatcher for vivado-bridge operations.

Reads a single JSON command from stdin in the form
    {"op": "<module>.<function>", "params": {...}}
and dispatches it to the corresponding helper in
operations.{build,debug,hardware,ila,sim,project,bridge}. The helper's
return dict is emitted to stdout as a single line of JSON.

Use `--list` to see available op names. Use `--help` for the protocol
shape.

This is the unified entry point for AI agents driving vivado-bridge.
It complements (does not replace) the lower-level `exec_tcl.py`
escape hatch for raw Tcl.

Examples:
    echo '{"op":"project.info"}' | python vivado_op.py
    echo '{"op":"build.synthesize","params":{"jobs":8}}' | python vivado_op.py
    python vivado_op.py --list

Exit codes: 0 on helper-reported success, 1 otherwise.
"""

from __future__ import annotations

import difflib
import inspect
import json
import sys
import traceback
from pathlib import Path

# sys.path setup so `from operations import ...` and
# `from vivado_bridge_client import ...` work regardless of cwd.
# Same pattern as the other scripts/*.py: prepend the SKILL root and
# scripts/ directory based on __file__.
_SCRIPT_DIR = Path(__file__).resolve().parent
_SKILL_ROOT = _SCRIPT_DIR.parent
for _p in (str(_SCRIPT_DIR), str(_SKILL_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from operations import (  # noqa: E402
    bridge as op_bridge,
    build,
    debug,
    hardware,
    ila,
    project,
    sim,
)
from vivado_bridge_client import BridgeError, Client  # noqa: E402


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------
#
# Each entry maps "<module>.<function>" -> (callable, needs_client).
# `needs_client=True` means the dispatcher prepends a connected Client
# as the first positional argument. `needs_client=False` is for the
# two pure helpers in operations.ila that work entirely on host-side
# data (no Vivado interaction).

DISPATCH: dict[str, tuple[object, bool]] = {
    # bridge
    "bridge.get_vivado_logs":   (op_bridge.get_vivado_logs, True),
    # build
    "build.close_design":       (build.close_design, True),
    "build.find_bitstream":     (build.find_bitstream, True),
    "build.get_active_runs":    (build.get_active_runs, True),
    "build.get_run_diagnostics": (build.get_run_diagnostics, True),
    "build.get_run_log_path":   (build.get_run_log_path, True),
    "build.get_run_status":     (build.get_run_status, True),
    "build.implement":          (build.implement, True),
    "build.open_synth":         (build.open_synth, True),
    "build.summary":            (build.summary, True),
    "build.synthesize":         (build.synthesize, True),
    "build.synthesize_ip":      (build.synthesize_ip, True),
    "build.wait_for_run":       (build.wait_for_run, True),
    # debug
    "debug.create_ila_core":    (debug.create_ila_core, True),
    "debug.create_vio":         (debug.create_vio, True),
    "debug.delete_ila_core":    (debug.delete_ila_core, True),
    "debug.find_clock_net":     (debug.find_clock_net, True),
    "debug.list_vio_probes":    (debug.list_vio_probes, True),
    "debug.list_vios":          (debug.list_vios, True),
    "debug.read_vio_probe":     (debug.read_vio_probe, True),
    "debug.read_vio_probes_all": (debug.read_vio_probes_all, True),
    "debug.verify_probe_nets":  (debug.verify_probe_nets, True),
    "debug.write_vio_probe":    (debug.write_vio_probe, True),
    "debug.write_vio_probes":   (debug.write_vio_probes, True),
    # hardware
    "hardware.close_hardware_target": (hardware.close_hardware_target, True),
    "hardware.connect_hw_server":     (hardware.connect_hw_server, True),
    "hardware.get_hardware_status":   (hardware.get_hardware_status, True),
    "hardware.list_hw_devices":       (hardware.list_hw_devices, True),
    "hardware.open_hardware_device":  (hardware.open_hardware_device, True),
    "hardware.open_hardware_target":  (hardware.open_hardware_target, True),
    "hardware.open_hw_manager":       (hardware.open_hw_manager, True),
    "hardware.program_device":        (hardware.program_device, True),
    # ila
    "ila.arm":                  (ila.arm, True),
    "ila.configure":            (ila.configure, True),
    "ila.export_csv":           (ila.export_csv, True),
    "ila.get_status":           (ila.get_status, True),
    "ila.list_ila_probes":      (ila.list_ila_probes, True),
    "ila.list_ilas":            (ila.list_ilas, True),
    "ila.set_triggers":         (ila.set_triggers, True),
    "ila.wait_for_capture":     (ila.wait_for_capture, True),
    # Pure helpers -- no Vivado connection needed.
    "ila.find_column":          (ila.find_column, False),
    "ila.parse_csv":            (ila.parse_csv, False),
    # project
    "project.add_sources":      (project.add_sources, True),
    "project.info":             (project.info, True),
    # sim
    "sim.close_sim":            (sim.close_sim, True),
    "sim.get_sim_status":       (sim.get_sim_status, True),
    "sim.run":                  (sim.run, True),
    "sim.summary":              (sim.summary, True),
}


_USAGE = """\
vivado_op.py -- JSON dispatcher for vivado-bridge operations.

Protocol:
    Request  (stdin, single JSON object):
        {"op": "<module>.<function>", "params": {...kwargs...}}
        - "op"     required string, e.g. "build.synthesize".
        - "params" optional object; passed as **kwargs to the helper.

    Response (stdout, single line JSON):
        On success: the helper's return dict ({"success": true, ...}).
        On dispatcher error:
            {"success": false, "error_kind": "<kind>",
             "message": "...", "op": "<op or null>"}
        error_kind values:
            protocol_error   bad JSON / missing op / wrong shape
            unknown_op       op not in dispatch table
            client_error     bad params (unknown / missing kwarg);
                             response includes `unknown_kwargs`,
                             `missing_required`, `suggestions` (a
                             {wrong_name: closest_accepted_name | null}
                             dict), and `accepted_kwargs`
            connect_failed   Client.connect() raised
            helper_exception helper raised an unhandled exception
                             (also includes exception_type, traceback)

Exit codes:
    0  helper returned {"success": true, ...}
    1  any other case

Discovery:
    python vivado_op.py --list     list all registered op names
    python vivado_op.py --help     this message

Examples:
    echo '{"op":"project.info"}' | python vivado_op.py
    echo '{"op":"build.synthesize","params":{"jobs":8}}' | python vivado_op.py
"""


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _emit(obj: dict) -> None:
    """Write `obj` to stdout as single-line JSON with trailing newline."""
    sys.stdout.write(json.dumps(obj, default=str) + "\n")
    sys.stdout.flush()


def _dispatcher_error(
    error_kind: str,
    message: str,
    op: str | None = None,
    **extra,
) -> dict:
    out = {
        "success": False,
        "error_kind": error_kind,
        "message": message,
        "op": op,
    }
    out.update(extra)
    return out


# ---------------------------------------------------------------------------
# kwargs pre-flight (catch wrong kwarg names with suggestions)
# ---------------------------------------------------------------------------

def _validate_kwargs(
    func, params: dict, *, needs_client: bool,
) -> tuple[list[str], list[str]]:
    """Return (unknown_kwargs, missing_required_kwargs) for a helper.

    Examines `func`'s signature and compares it against the param keys
    the caller passed. The intent is to catch kwarg-name mistakes
    BEFORE they bubble up as a generic Python TypeError, and to attach
    a "did you mean ...?" suggestion to each one. This gives the AI
    caller an actionable error in one round trip instead of the
    helper_exception loop they otherwise fall into.

    `needs_client=True` means the first positional parameter is the
    `client` argument that the dispatcher itself supplies; the
    validation drops it before comparing kwargs.

    Helpers using **kwargs (var-keyword) are exempt from unknown-kwarg
    detection -- they accept anything, so we cannot judge. Required-arg
    detection still applies to such helpers.
    """
    try:
        sig = inspect.signature(func)
    except (TypeError, ValueError):
        # Builtin / C-implemented callable. Skip; let the TypeError
        # path handle any later mismatch.
        return [], []

    parameters = list(sig.parameters.values())
    if needs_client and parameters:
        parameters = parameters[1:]  # drop `client`

    accepts_var_kw = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters)

    accepted: set[str] = set()
    required: set[str] = set()
    for p in parameters:
        if p.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            accepted.add(p.name)
            if p.default is inspect.Parameter.empty:
                required.add(p.name)

    passed = set(params.keys())
    unknown: list[str] = []
    if not accepts_var_kw:
        unknown = sorted(passed - accepted)

    missing = sorted(required - passed)
    return unknown, missing


def _suggest_kwargs(unknown: list[str], accepted: list[str]) -> dict[str, str | None]:
    """For each unknown kwarg, return the closest accepted kwarg name (or None).

    `difflib.get_close_matches` ranks by SequenceMatcher ratio; cutoff
    0.5 picks up obvious typos and the recurring kwarg-shape mistakes
    (`triggers` -> `values`, `writes` -> `values`, `timeout_s` ->
    `timeout`) while staying conservative enough not to invent
    suggestions for completely unrelated names.
    """
    suggestions: dict[str, str | None] = {}
    for name in unknown:
        match = difflib.get_close_matches(name, accepted, n=1, cutoff=0.5)
        suggestions[name] = match[0] if match else None
    return suggestions


# ---------------------------------------------------------------------------
# Connection (lazy, single Client per process)
# ---------------------------------------------------------------------------

_client_cache: dict[str, Client] = {}


def _get_client() -> Client:
    """Return a connected Client, building it on first use.

    Cached so a single process only opens one bridge connection regardless
    of how many ops it dispatches in sequence (currently always 1, but
    future stdin streaming would benefit). Raises BridgeError subclasses
    on failure, which the caller maps to error_kind="connect_failed".
    """
    if "c" not in _client_cache:
        _client_cache["c"] = Client.connect()
    return _client_cache["c"]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    # Force UTF-8 stdout so helper messages with non-ASCII survive on
    # Windows default codepage consoles.
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass

    args = argv[1:]
    if args and args[0] in ("--help", "-h", "help"):
        sys.stdout.write(_USAGE)
        return 0
    if args and args[0] == "--list":
        for name in sorted(DISPATCH):
            sys.stdout.write(name + "\n")
        return 0

    # Any other CLI arg is unexpected -- the dispatcher reads its
    # request from stdin, not argv. Surface that as a protocol error
    # rather than silently ignoring.
    if args:
        _emit(_dispatcher_error(
            "protocol_error",
            f"vivado_op.py takes no positional args (got {args!r}); "
            "send the JSON request on stdin. Use --help for protocol.",
        ))
        return 1

    # No args: read JSON request from stdin. If stdin is an interactive
    # terminal (no data piped in), print usage instead of blocking on
    # input() -- the script is non-interactive by contract.
    if sys.stdin.isatty():
        sys.stdout.write(_USAGE)
        return 0

    return _run_from_stdin()


def _run_from_stdin() -> int:
    raw = sys.stdin.read()
    # PowerShell 5.1 prepends a UTF-8 BOM when piping a string into a
    # native executable. Strip it so JSON parsing doesn't fail on input
    # that's otherwise valid. Harmless for non-BOM input.
    if raw.startswith("\ufeff"):
        raw = raw.lstrip("\ufeff")
    try:
        request = json.loads(raw)
    except json.JSONDecodeError as exc:
        _emit(_dispatcher_error(
            "protocol_error",
            f"stdin is not valid JSON: {exc}",
        ))
        return 1

    if not isinstance(request, dict):
        _emit(_dispatcher_error(
            "protocol_error",
            f"request must be a JSON object, got {type(request).__name__}",
        ))
        return 1

    op = request.get("op")
    if not isinstance(op, str) or not op:
        _emit(_dispatcher_error(
            "protocol_error",
            "request is missing required string field 'op'",
        ))
        return 1

    params = request.get("params", {})
    if params is None:
        params = {}
    if not isinstance(params, dict):
        _emit(_dispatcher_error(
            "protocol_error",
            f"'params' must be a JSON object, got {type(params).__name__}",
            op=op,
        ))
        return 1

    entry = DISPATCH.get(op)
    if entry is None:
        _emit(_dispatcher_error(
            "unknown_op",
            f"unknown op {op!r}. Use --list to see available ops.",
            op=op,
        ))
        return 1

    func, needs_client = entry

    # Resolve client lazily; only pay the connect cost for ops that need it.
    if needs_client:
        try:
            client = _get_client()
        except BridgeError as exc:
            _emit(_dispatcher_error(
                "connect_failed",
                f"could not connect to vivado-bridge: {exc}",
                op=op,
                exception_type=type(exc).__name__,
            ))
            return 1
        except Exception as exc:  # noqa: BLE001 -- last-resort safety net
            _emit(_dispatcher_error(
                "connect_failed",
                f"unexpected error connecting to vivado-bridge: {exc}",
                op=op,
                exception_type=type(exc).__name__,
                traceback=traceback.format_exc(),
            ))
            return 1
        call_args: tuple = (client,)
    else:
        call_args = ()

    # Pre-flight kwarg validation. Bad kwarg names are by far the
    # most common caller mistake (the AI guesses a plausible-sounding
    # kwarg, the helper raises a generic TypeError, the AI has to
    # parse the exception to recover). Catching that here lets us
    # emit a structured client_error with a "did you mean ...?" hint
    # in one round trip.
    try:
        unknown_kwargs, missing_required = _validate_kwargs(
            func, params, needs_client=needs_client,
        )
    except Exception:  # noqa: BLE001 -- never fail dispatch on a sig probe
        unknown_kwargs, missing_required = [], []

    if unknown_kwargs or missing_required:
        try:
            sig = inspect.signature(func)
            accepted_names = [
                p.name for p in list(sig.parameters.values())[
                    1 if needs_client else 0:
                ]
                if p.kind in (
                    inspect.Parameter.POSITIONAL_OR_KEYWORD,
                    inspect.Parameter.KEYWORD_ONLY,
                )
            ]
        except (TypeError, ValueError):
            accepted_names = []

        parts: list[str] = []
        suggestions: dict[str, str | None] = {}
        if unknown_kwargs:
            suggestions = _suggest_kwargs(unknown_kwargs, accepted_names)
            for name, match in suggestions.items():
                if match:
                    parts.append(f"unknown kwarg {name!r}; did you mean {match!r}?")
                else:
                    parts.append(f"unknown kwarg {name!r}")
        if missing_required:
            parts.append(
                f"missing required kwarg(s): {missing_required}"
            )

        message = (
            f"{op}: " + "; ".join(parts) +
            f". Accepted kwargs: {accepted_names}."
        )
        _emit(_dispatcher_error(
            "client_error",
            message,
            op=op,
            unknown_kwargs=unknown_kwargs,
            missing_required=missing_required,
            suggestions=suggestions,
            accepted_kwargs=accepted_names,
        ))
        return 1

    # Invoke the helper. Only catch unhandled exceptions here -- helpers
    # report their own failures via {"success": False, ...} dicts and
    # we must NOT mask those.
    try:
        result = func(*call_args, **params)  # type: ignore[operator]
    except TypeError as exc:
        # Most common cause: bad params keys (unexpected kwarg / missing
        # required arg). Surface as helper_exception so the agent can
        # see exactly what the helper rejected. The pre-flight above
        # catches most kwarg shape problems first, so reaching here
        # usually indicates a value-level type mismatch (e.g. passing
        # a dict where a list was expected).
        _emit(_dispatcher_error(
            "helper_exception",
            f"helper {op} raised TypeError: {exc}",
            op=op,
            exception_type="TypeError",
            traceback=traceback.format_exc(),
        ))
        return 1
    except Exception as exc:  # noqa: BLE001
        _emit(_dispatcher_error(
            "helper_exception",
            f"helper {op} raised {type(exc).__name__}: {exc}",
            op=op,
            exception_type=type(exc).__name__,
            traceback=traceback.format_exc(),
        ))
        return 1

    # Some helpers (the pure ones in particular -- find_column returns
    # int|None) don't return a dict. Wrap so output is always a JSON object.
    if not isinstance(result, dict):
        result = {"success": True, "result": result}

    _emit(result)
    return 0 if result.get("success") is True else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
