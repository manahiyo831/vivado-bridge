"""
vivado_bridge_client - Python client library for the vivado-bridge TCL server.

This is the single source of truth for talking to the bridge: every CLI
script (exec_tcl.py, connection_check.py, build_project.py, ...) goes
through `Client` here. Behavioural rules:

- Configuration is read from the .env file next to vivado_socket_server.tcl
  (the parent directory of this script). VIVADO_BRIDGE_HOST and
  VIVADO_BRIDGE_PORT are required; environment variables of the same name
  override .env; the keyword args to Client(...) override both.
  No default port is provided -- callers must configure once.

- Every request returns a `Response` dataclass that carries `success`
  (real bool), `error_kind` (None on success, otherwise one of
  "tcl_error" | "blocked_command" | "protocol_error" |
  "unknown_command" | "internal_error" | "client_error"), and the full
  decoded payload. Scripts inspect `error_kind` to branch, never the
  string contents of error messages.

- A single Client instance opens one short-lived TCP connection per
  request. The bridge server itself is concurrent-friendly but each
  Vivado instance only has one Tcl interpreter, so requests are
  effectively serialised on the server side regardless.

- Identity check (ping) verifies the server is actually vivado-bridge
  before the caller starts issuing TCL. This catches the case where some
  unrelated tool happened to grab the configured port.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Default per-request socket timeout in seconds. Long-running Vivado
# operations (synth_design, place_design, ...) will exceed this. Callers
# that need a longer wait pass `timeout=` explicitly. wait_for_run-style
# polling stays at the default because each individual poll is cheap.
DEFAULT_TIMEOUT = 30.0


# ---------------------------------------------------------------------------
# .env loading
# ---------------------------------------------------------------------------

def _bridge_dir() -> Path:
    """Return the directory holding vivado_socket_server.tcl and .env.

    Layout assumption: this file lives in <bridge_dir>/scripts/, so the
    bridge dir is one level up.
    """
    return Path(__file__).resolve().parent.parent


def load_env(path: Path | None = None) -> dict[str, str]:
    """Parse a simple KEY=VALUE .env file. Comments (#) and blank lines
    are ignored. Surrounding single or double quotes are stripped. Missing
    file yields {}; missing keys are simply absent from the dict.
    """
    if path is None:
        path = _bridge_dir() / ".env"
    result: dict[str, str] = {}
    if not path.exists():
        return result
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        result[key] = val
    return result


def resolve_host_port(
    host: str | None = None,
    port: int | None = None,
) -> tuple[str, int]:
    """Resolve host and port using the documented precedence:
    explicit kwarg > process env > .env file. Raises ConfigError if
    either remains unset.
    """
    env_file = load_env()

    resolved_host = (
        host
        or os.environ.get("VIVADO_BRIDGE_HOST")
        or env_file.get("VIVADO_BRIDGE_HOST")
    )
    resolved_port_str = (
        str(port) if port is not None else None
    ) or os.environ.get("VIVADO_BRIDGE_PORT") or env_file.get("VIVADO_BRIDGE_PORT")

    missing = []
    if not resolved_host:
        missing.append("VIVADO_BRIDGE_HOST")
    if not resolved_port_str:
        missing.append("VIVADO_BRIDGE_PORT")
    if missing:
        raise ConfigError(
            f"Missing required setting(s): {', '.join(missing)}.\n"
            f"Set them in {_bridge_dir() / '.env'} or as environment "
            f"variables, or pass them to Client()."
        )

    try:
        resolved_port = int(resolved_port_str)
    except ValueError:
        raise ConfigError(
            f"VIVADO_BRIDGE_PORT must be an integer, got: {resolved_port_str!r}"
        )
    if not (1 <= resolved_port <= 65535):
        raise ConfigError(
            f"VIVADO_BRIDGE_PORT out of range (1..65535): {resolved_port}"
        )

    return resolved_host, resolved_port


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BridgeError(Exception):
    """Base class for vivado-bridge client errors."""


class ConfigError(BridgeError):
    """Configuration is missing or invalid."""


class ConnectionError_(BridgeError):
    """Could not reach the bridge server."""


class IdentityError(BridgeError):
    """Server on this port is not vivado-bridge."""


# ---------------------------------------------------------------------------
# Response wrapper
# ---------------------------------------------------------------------------

@dataclass
class Response:
    """A decoded response from the bridge.

    Fields are populated from the JSON payload; `raw` keeps the full dict
    so callers can read any extra fields the server included.

    `console_lines` is populated client-side, not by the server: the
    Client tails vivado.log between exec_tcl calls and attaches every
    line Vivado wrote to the Tcl Console during this call (testbench
    `$display`, `RESULT:`, `$finish called at time ...`, WARNING /
    CRITICAL WARNING / ERROR, ...). The Tcl Console transcript is the
    only window into "what just happened in Vivado" -- those lines go
    only to the log, not into the Tcl return value, so without this
    they would be invisible to anyone driving the bridge from Python.
    See `Client._tail_log_warnings` for the filter.

    `console_warnings` is the legacy alias kept so existing operations
    code that expects the field name continues to compile; it shadows
    `console_lines` exactly.
    """
    success: bool
    error_kind: str | None
    output: str = ""
    message: str = ""
    error_info: str = ""
    error_code: str = ""
    blocked_token: str = ""
    console_lines: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def console_warnings(self) -> list[str]:
        # Legacy name; new code should read console_lines.
        return self.console_lines

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Response":
        return cls(
            success=bool(d.get("success", False)),
            error_kind=d.get("error_kind"),
            output=d.get("output", "") or "",
            message=d.get("message", "") or "",
            error_info=d.get("error_info", "") or "",
            error_code=d.get("error_code", "") or "",
            blocked_token=d.get("blocked_token", "") or "",
            console_lines=[],
            raw=d,
        )

    @classmethod
    def client_error(cls, kind: str, message: str) -> "Response":
        """Build a Response for failures that happened on the client side
        (no connection, malformed reply, ...) so callers can use the same
        shape everywhere.
        """
        return cls(
            success=False,
            error_kind=kind,
            message=message,
            console_lines=[],
            raw={"success": False, "error_kind": kind, "message": message},
        )

    def format_error(self) -> str:
        """Human-readable single-line summary of an error response.
        Returns "" for successful responses.
        """
        if self.success:
            return ""
        parts = [f"[{self.error_kind or 'error'}]"]
        if self.message:
            parts.append(self.message)
        if self.blocked_token:
            parts.append(f"(blocked: {self.blocked_token})")
        return " ".join(parts)


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class Client:
    """Small synchronous client for the vivado-bridge TCL server.

    Each call() opens a fresh connection, sends one JSON line, reads one
    JSON line back, and closes. That keeps the protocol simple and means
    a stuck request can't poison subsequent ones.

    The first ping() succeeds only if the server identifies itself as
    vivado-bridge. Use `Client.connect()` (classmethod) for the common
    "configure + identity-check" pattern.
    """

    BRIDGE_NAME = "vivado-bridge"

    # Per-category cap for the auto-tailed warnings returned by
    # drain_console_warnings. Real builds can dump 1000+ WARNING lines
    # — most of them are echoes of the same handful of root causes,
    # and shoving all of them into an AI's context window crowds out
    # the actual diagnostics. We keep the first N of each category
    # (in original order, so the root-cause lines that Vivado prints
    # first survive) and add a one-line summary noting how many were
    # suppressed and how to fetch the full log.
    DEFAULT_WARNING_CAP_PER_CATEGORY = 50

    def __init__(
        self,
        host: str | None = None,
        port: int | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        warning_cap_per_category: int | None = None,
    ) -> None:
        self.host, self.port = resolve_host_port(host, port)
        self.timeout = timeout
        self.warning_cap_per_category = (
            self.DEFAULT_WARNING_CAP_PER_CATEGORY
            if warning_cap_per_category is None
            else warning_cap_per_category
        )
        # vivado.log auto-tail state. Populated lazily on the first
        # exec_tcl that succeeds — at that point we know Vivado is
        # responsive and can ask it for `pwd` to locate the log.
        # `_log_path` stays None until lazy resolution finishes (or
        # until we give up; see _resolve_log_path).
        self._log_path: Path | None = None
        self._log_offset: int = 0
        self._log_resolution_attempted: bool = False
        self._log_resolution_error: str | None = None
        # Warnings accumulated across exec_tcl calls within a single
        # logical operation. Drained by `drain_console_warnings` once
        # the operation builds its result dict.
        self._pending_console_warnings: list[str] = []

    # -- low-level call ----------------------------------------------------

    def call(
        self,
        command: str,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> Response:
        """Send one request and return the decoded Response. Errors are
        captured into Response (with error_kind="client_error") rather
        than raised, so simple scripts can branch on `resp.success`
        without try/except. Lower-level callers that prefer exceptions
        can check error_kind and raise themselves.
        """
        req = {"command": command}
        if params is not None:
            req["params"] = params
        wire = (json.dumps(req) + "\n").encode("utf-8")
        eff_timeout = self.timeout if timeout is None else timeout

        try:
            with socket.create_connection((self.host, self.port), timeout=eff_timeout) as sock:
                sock.sendall(wire)
                buf = bytearray()
                while not buf.endswith(b"\n"):
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buf.extend(chunk)
        except ConnectionRefusedError:
            return Response.client_error(
                "client_error",
                f"Connection refused at {self.host}:{self.port}. "
                f"Is Vivado running with vivado_socket_server.tcl sourced?",
            )
        except socket.timeout:
            # A timeout here usually means one of two things:
            #   (1) The Tcl snippet we sent is genuinely long (synth, place,
            #       route, hw_target wait, ...). Bump --timeout / `timeout=`.
            #   (2) Someone is using Vivado's Tcl Console manually and is
            #       running a long command there, so the bridge can't get
            #       cycles to handle our request until they finish.
            # Either way the server is fine; we just gave up waiting.
            return Response.client_error(
                "client_error",
                f"Timed out after {eff_timeout:g}s waiting for response. "
                f"Vivado may be busy with a long-running command "
                f"(synth/impl, or a manual Tcl Console operation). "
                f"Increase the timeout, or wait and retry.",
            )
        except OSError as e:
            return Response.client_error("client_error", f"Socket error: {e}")

        if not buf:
            return Response.client_error(
                "client_error",
                "Server closed the connection without responding.",
            )

        try:
            payload = json.loads(buf.decode("utf-8").strip())
        except json.JSONDecodeError as e:
            return Response.client_error(
                "client_error",
                f"Server returned invalid JSON: {e}. Raw: {buf!r}",
            )
        if not isinstance(payload, dict):
            return Response.client_error(
                "client_error",
                f"Server returned non-object JSON: {payload!r}",
            )

        return Response.from_dict(payload)

    # -- convenience -------------------------------------------------------

    def ping(self, *, verify_identity: bool = True) -> Response:
        """Ping the server. With verify_identity (default), confirms the
        peer is vivado-bridge; if not, returns a Response with
        error_kind="identity_error".
        """
        resp = self.call("ping")
        if not resp.success:
            return resp
        if verify_identity:
            bridge = resp.raw.get("bridge")
            if bridge != self.BRIDGE_NAME:
                return Response.client_error(
                    "identity_error",
                    f"Port {self.port} is occupied by something else "
                    f"(bridge field={bridge!r}, expected {self.BRIDGE_NAME!r}).",
                )
        return resp

    def exec_tcl(self, tcl_code: str, timeout: float | None = None) -> Response:
        """Run a Tcl snippet on the bridge.

        On top of forwarding the request, the client tails the Vivado
        session's `vivado.log` and attaches every new line Vivado
        wrote during this call to `Response.console_lines`. This is
        the path through which AI / Python callers see Vivado's Tcl
        Console output — `$display`, testbench `RESULT:` lines,
        `$finish called at time ...`, WARNING / ERROR — none of which
        come back through the Tcl return value.

        The same lines are also accumulated in the Client-internal
        pending buffer used by the operations layer's
        `drain_console_warnings()` mechanism. This double-write is
        deliberate: callers using the bridge directly (e.g. raw
        `c.exec_tcl(...)` from a script) get visibility via
        `resp.console_lines`, while operations modules that call
        several exec_tcl in a row and only finalise once still get
        every line collected into the operation's `warnings` list.
        Both paths see the same data; the operations-layer drain
        clears the pending buffer, so an operation that *also* reads
        each individual `resp.console_lines` would still see only one
        copy total (one in the Response, none in the drain).
        """
        resp = self.call(
            "exec_tcl",
            {"tcl_code": tcl_code},
            timeout=timeout,
        )
        # Skip log tailing for client-side errors (no connection / bad
        # JSON / etc). The server didn't run anything in those cases,
        # so vivado.log can't have anything new from this call, and we
        # don't want to do filesystem I/O when Vivado may not even be
        # reachable.
        if resp.error_kind != "client_error":
            new_lines = self._collect_pending_warnings()
            if new_lines:
                resp.console_lines = list(new_lines)
        return resp

    # -- log tail collection ----------------------------------------------

    def _collect_pending_warnings(self) -> list[str]:
        """Tail vivado.log, append any new lines to the pending buffer,
        and return the same lines so the caller can also stash them
        per-call (e.g. on the immediate Response).

        Drives lazy resolution of the log path on first call. On
        resolution failure we record an explanatory line in the buffer
        once and then suppress further attempts (so we don't hammer
        Vivado with `pwd` queries when something is structurally wrong
        with the log path).
        """
        if not self._log_resolution_attempted:
            self._resolve_log_path()
        if self._log_path is None:
            if self._log_resolution_error:
                msg = (
                    f"[bridge] vivado.log auto-tail disabled: "
                    f"{self._log_resolution_error}"
                )
                self._pending_console_warnings.append(msg)
                self._log_resolution_error = None
                return [msg]
            return []
        try:
            new_lines = self._tail_log_warnings()
        except OSError as e:
            msg = f"[bridge] could not read {self._log_path}: {e}"
            self._pending_console_warnings.append(msg)
            return [msg]
        if new_lines:
            self._pending_console_warnings.extend(new_lines)
        return list(new_lines)

    # -- pending-warnings buffer ------------------------------------------

    # Vivado tags every diagnostic with a `[<facility> <code>]` token,
    # e.g. `[Vivado 12-584]`, `[Common 17-55]`, `[Synth 8-2716]`. We
    # use that token as the dedup key so a single root cause that
    # echoes 100 times collapses into a single line + an occurrence
    # count, matching how the Vivado IDE Messages tab folds repeats.
    # The pattern allows letters, digits, dot and underscore in the
    # facility name (Designutils, Labtoolstcl, ...) and digit-dash-
    # digit in the code.
    _MSG_ID_RE = re.compile(r"\[([A-Za-z][\w.]*\s+\d+-\d+)\]")

    def drain_console_warnings(self) -> list[str]:
        """Return and clear the warnings collected since the last drain.

        operations modules call this when they finalise a result dict
        so that any Vivado-side WARNING / CRITICAL WARNING / ERROR that
        appeared during *any* of the exec_tcl calls inside the
        operation lands in the operation's own `warnings` list. Without
        this drain, only the warnings from the last exec_tcl in a
        chain would survive (operations frequently issue several
        sub-queries per call).

        Returned-list shaping (in order of application):

        1. **ID-based dedup**: every diagnostic Vivado writes carries a
           message id like `[Vivado 12-584]`. Real builds can fire
           the same id 100+ times for what is really one root cause
           (a missing port, a bad XDC line, ...). We keep the *first*
           line for each id and tag duplicates as
           `(×N occurrences)`. Lines without a recognisable id
           (rare, but possible for stripped-down messages) are passed
           through as-is and not collapsed.
        2. **Per-category cap** (`self.warning_cap_per_category`,
           default 50): WARNING / CRITICAL WARNING / ERROR are each
           capped at N *unique* lines (post-dedup). The first N of
           each category in original order survive — Vivado emits
           root causes first, so the most useful diagnostics are the
           ones we keep.
        3. **Truncation summaries** for categories that overflowed,
           appended at the very end and pointing at
           `bridge.get_vivado_logs()` for the raw log.

        Calling this without warnings present is cheap and idempotent.

        Note: this method assumes single-threaded use of the Client.
        The bridge supports concurrent connections at the socket level
        but `_log_offset` and `_pending_console_warnings` are not
        guarded — callers using the same Client instance from multiple
        threads should add their own locking.
        """
        all_lines = list(self._pending_console_warnings)
        self._pending_console_warnings.clear()
        if not all_lines:
            return []

        cap = self.warning_cap_per_category

        def category_of(line: str) -> str | None:
            # CRITICAL WARNING starts with "CRITICAL", check before WARNING.
            if line.startswith("CRITICAL WARNING:"):
                return "CRITICAL WARNING"
            if line.startswith("WARNING:"):
                return "WARNING"
            if line.startswith("ERROR:"):
                return "ERROR"
            return None

        # --- Pass 1: dedup by message id, preserving first occurrence
        # text and original order. Lines without an id (or bridge-
        # internal `[bridge]` synthetic warnings) bypass dedup. ---
        kept_unique: list[tuple[str | None, str]] = []  # [(id_or_None, text)]
        id_to_index: dict[tuple[str, str], int] = {}    # (category, id) -> index in kept_unique
        occurrences: list[int] = []                     # parallel to kept_unique
        for line in all_lines:
            cat = category_of(line)
            m = self._MSG_ID_RE.search(line) if cat is not None else None
            if cat is None or m is None:
                # bridge-internal, or no recognisable id: pass through
                kept_unique.append((None, line))
                occurrences.append(1)
                continue
            key = (cat, m.group(1))
            if key in id_to_index:
                occurrences[id_to_index[key]] += 1
                continue
            id_to_index[key] = len(kept_unique)
            kept_unique.append((cat, line))
            occurrences.append(1)

        # --- Pass 2: per-category cap on unique lines, in original order ---
        kept: list[str] = []
        seen_unique: dict[str, int] = {"WARNING": 0, "CRITICAL WARNING": 0, "ERROR": 0}
        kept_count: dict[str, int] = {"WARNING": 0, "CRITICAL WARNING": 0, "ERROR": 0}
        suppressed_repeats: dict[str, int] = {"WARNING": 0, "CRITICAL WARNING": 0, "ERROR": 0}
        for (cat, text), occ in zip(kept_unique, occurrences):
            if cat is None:
                # pass-through line; not counted toward category caps
                kept.append(text)
                continue
            seen_unique[cat] += 1
            display = text if occ == 1 else f"{text}   (×{occ} occurrences)"
            if kept_count[cat] < cap:
                kept.append(display)
                kept_count[cat] += 1
            else:
                suppressed_repeats[cat] += occ  # whole equivalence class lost

        # --- Pass 3: per-category truncation summary lines at the end ---
        # Order: WARNING -> CRITICAL WARNING -> ERROR (severity ascending),
        # so the most serious summary is last and easiest to notice when
        # the AI scans the tail.
        for cat in ("WARNING", "CRITICAL WARNING", "ERROR"):
            total_unique = seen_unique[cat]
            if total_unique > kept_count[cat]:
                suppressed_unique = total_unique - kept_count[cat]
                # `suppressed_repeats[cat]` is the sum of occurrences
                # across the suppressed unique ids -- i.e. the raw
                # vivado.log line count that was dropped, including
                # both first occurrences and any repeats. Phrasing
                # this as "N total line(s) in those ids" avoids the
                # ambiguity of "repeated" (which could be read as
                # either "lines after the first" or "raw line total").
                kept.append(
                    f"[bridge] {suppressed_unique} more unique {cat} id(s) "
                    f"suppressed ({suppressed_repeats[cat]} total "
                    f"line(s) in those ids). Read vivado.log via "
                    f"bridge.get_vivado_logs() for the full list."
                )
        return kept

    # -- vivado.log auto-tail ----------------------------------------------

    def _resolve_log_path(self) -> None:
        """Locate vivado.log via the bridge and snapshot its current size.

        Called once per Client lifetime. We deliberately seed
        `_log_offset` with the file's *current* size so that historical
        log content (everything written before this Client connected)
        is ignored. Only output produced by this Client's exec_tcl
        calls will appear in console_warnings, which keeps the noise
        floor predictable.
        """
        self._log_resolution_attempted = True
        # Use raw call (not exec_tcl) to avoid recursion.
        r = self.call("exec_tcl", {"tcl_code": "pwd"})
        if not r.success:
            self._log_resolution_error = (
                f"could not query Vivado pwd "
                f"({r.error_kind}: {r.message})"
            )
            return
        cwd = (r.output or "").strip()
        if not cwd:
            self._log_resolution_error = "Vivado pwd returned empty"
            return
        log_path = Path(cwd) / "vivado.log"
        try:
            self._log_offset = log_path.stat().st_size if log_path.exists() else 0
        except OSError as e:
            self._log_resolution_error = f"could not stat {log_path}: {e}"
            return
        self._log_path = log_path

    # Vivado-internal noise that should not surface in operation
    # `warnings` lists. These are NOT design issues — they are
    # housekeeping / launcher / GUI-state messages that Vivado tags
    # with ERROR or WARNING severity for internal reasons but that
    # carry no actionable signal for the caller. Each entry MUST be
    # justified in the comment so we don't quietly suppress real
    # diagnostics down the line.
    _BENIGN_MSG_IDS = frozenset({
        # `Spawn failed: No error` — Vivado's parallel-launcher noise
        # printed with ERROR severity even though the run completed.
        # In practice it appears in `warnings` while
        # `diagnostics.error_count` (runme.log) stays at 0, causing
        # "ERROR: in warnings but no run errors" confusion for callers
        # who grep for the literal string ERROR:.
        "Common 17-180",
        # `Oracle tile group HSR_BOUNDARY_TOP failed to initialize`
        # — internal device-graph init noise tied to GUI device-view
        # population. Has no effect on place/route correctness;
        # observed every impl on certain Vivado versions.
        "Device 21-9320",
        # `Failed to initialize Virtual grid` — same family as
        # 21-9320, same justification.
        "Device 21-2174",
    })

    def _tail_log_warnings(self) -> list[str]:
        """Read vivado.log from `_log_offset` to end, return new lines.

        Returns essentially everything Vivado wrote to the Tcl Console
        between calls — WARNING / CRITICAL WARNING / ERROR lines plus
        any other output the user's Tcl produced (`$display`, `puts`,
        `RESULT: ...`, `$finish called at time ...`, etc.). The
        Console transcript is the AI's only window into "what just
        happened in Vivado", so dropping the non-severity lines (as
        previous versions did) silently hid testbench output and
        simulator self-reports — exactly the visibility gap that
        produced 46 ms of runaway sim before anyone noticed.

        Filters applied (kept minimal on purpose):
          - 'INFO:' lines are dropped. Vivado emits hundreds of these
            for routine progress and the noise crowds out the actual
            signal in the response. If you need them, read vivado.log
            directly via bridge.get_vivado_logs().
          - Lines mentioning the bridge's own message id
            ('[vbridge 1-' anywhere in the line) are dropped — those
            come from the bridge itself logging exec_tcl traffic and
            would otherwise echo back into every response.
          - Bare comment lines starting with '#' are dropped (Vivado's
            command-echo noise; e.g. `# set_property ...`).
          - Specific benign Vivado-internal message ids listed in
            `_BENIGN_MSG_IDS` are dropped. Each entry there is
            documented with the exact reason — these are housekeeping
            / launcher / GUI-state messages tagged with ERROR or
            WARNING severity but carrying no actionable signal. NOT
            a fallback: a small denylist of known-noisy ids, not a
            "skip anything that looks scary" heuristic.

        Anything else passes through verbatim. `drain_console_warnings`
        downstream knows how to dedup severity-tagged lines by message
        id and cap each category, while passing untagged lines straight
        through — so adding raw `$display` output here does not
        explode the response size for normal builds.

        File rotation (size shrunk below `_log_offset`) is treated as
        Vivado-restarted-with-fresh-log: offset resets to 0 and the
        whole new file is scanned.
        """
        assert self._log_path is not None
        try:
            cur_size = self._log_path.stat().st_size
        except FileNotFoundError:
            # Log was removed since resolution. Treat as "no new lines"
            # but reset offset so a freshly created log gets read from 0.
            self._log_offset = 0
            return []

        if cur_size == self._log_offset:
            return []  # no new bytes, common fast path
        if cur_size < self._log_offset:
            # Rotated / truncated -> read whole new file.
            self._log_offset = 0

        with self._log_path.open("rb") as f:
            f.seek(self._log_offset)
            chunk = f.read(cur_size - self._log_offset)
        self._log_offset = cur_size

        try:
            text = chunk.decode("utf-8", errors="replace")
        except UnicodeDecodeError:
            return []

        out: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            if not line:
                continue
            if "[vbridge 1-" in line:
                continue  # bridge's own send_msg_id chatter
            if line.startswith("INFO:"):
                continue  # Vivado's routine progress noise
            if line.startswith("#"):
                continue  # command-echo lines from Vivado's Tcl history
            # Drop messages from the curated benign-noise denylist.
            # The class-level _BENIGN_MSG_IDS comments justify each entry.
            m = self._MSG_ID_RE.search(line)
            if m and m.group(1) in self._BENIGN_MSG_IDS:
                continue
            out.append(line)
        return out

    def shutdown(self) -> Response:
        """Ask the server to stop accepting connections. Vivado itself
        keeps running.
        """
        return self.call("shutdown")

    def reload(
        self,
        server_tcl: Path | str | None = None,
        wait_seconds: float = 2.0,
        verify: bool = True,
    ) -> Response:
        """Stop the running bridge and re-source vivado_socket_server.tcl
        on the same Vivado instance, so changes you've made to the Tcl
        file take effect without you having to switch to the Tcl Console.

        Mechanism: we send a single Tcl snippet that (a) stops the
        current server, then (b) schedules a `source` of the new file
        via `after`. The `after` ensures the response makes it back to
        us before the listener closes. We never touch Vivado from the
        host side -- everything runs inside the Tcl interpreter that
        already has all the bridge procs defined.

        After this returns successfully, *this* Client object is no
        longer usable (the server it talked to has shut down). Build a
        new one with Client.connect() to talk to the reloaded server.

        Args:
            server_tcl: Absolute path to vivado_socket_server.tcl. If
                None, uses the file shipped next to this Python module.
            wait_seconds: How long to wait between stop and probing the
                new server. The reload itself takes ~0.1s; this is a
                cushion so we don't ping a half-started listener.
            verify: After waiting, ping the new server to confirm it's
                up. If False, this method returns immediately after
                sending the reload request.

        Returns a Response describing the outcome. On verify=True
        success, `raw` contains the new server's ping payload (Vivado
        version, bridge version, ...).
        """
        if server_tcl is None:
            server_tcl = _bridge_dir() / "vivado_socket_server.tcl"
        path = Path(server_tcl).resolve()
        if not path.exists():
            return Response.client_error(
                "client_error",
                f"server_tcl path does not exist: {path}",
            )

        # Tcl-friendly path. Use forward slashes, which Tcl accepts on
        # both Windows and Linux, and wrap in braces to handle spaces.
        tcl_path = path.as_posix()

        # Both stop_server and re-source must run AFTER our response is
        # flushed back over the socket -- otherwise stop_server would
        # close the very socket carrying the reply. Putting them inside
        # a single `after 100` block gives the event loop time to send
        # the reply before tearing the listener down.
        snippet = (
            f"after 100 {{::vbridge::stop_server; "
            f"after 100 {{source {{{tcl_path}}}}}}}"
        )
        resp = self.exec_tcl(snippet, timeout=10.0)
        if not resp.success:
            return resp

        if not verify:
            return resp

        # Wait for the listener to come back, then ping. We do a few
        # short retries instead of one long sleep so a fast reload
        # finishes fast. `Client.ping()` already returns a Response on
        # connection failure (no exception), so we just check success
        # and try again until the deadline.
        deadline = wait_seconds
        step = 0.2
        waited = 0.0
        while waited < deadline:
            time.sleep(step)
            waited += step
            probe = Client(host=self.host, port=self.port, timeout=2.0)
            pong = probe.ping()
            if pong.success and pong.raw.get("bridge") == self.BRIDGE_NAME:
                return pong

        return Response.client_error(
            "client_error",
            f"Server did not come back within {wait_seconds:.1f}s after "
            f"reload. Check the Tcl Console for source errors.",
        )

    # -- convenience constructor ------------------------------------------

    @classmethod
    def connect(
        cls,
        host: str | None = None,
        port: int | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> "Client":
        """Build a Client and verify the peer is vivado-bridge before
        returning it. Raises ConnectionError_ on connect failure and
        IdentityError if the peer doesn't identify correctly.
        """
        client = cls(host=host, port=port, timeout=timeout)
        resp = client.ping()
        if not resp.success:
            if resp.error_kind == "identity_error":
                raise IdentityError(resp.message)
            raise ConnectionError_(resp.message or "Failed to ping vivado-bridge")
        return client
