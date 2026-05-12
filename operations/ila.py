"""ILA capture operations: configure, arm, wait, export.

The Vivado Tcl surface for ILA capture has a few sharp edges that
trip up everyone the first time:

- `wait_on_hw_ila -timeout N` has been observed to interpret `N` in
  units other than seconds (a "5" wait blocked for ~5 minutes in one
  session). We poll `STATUS.CORE_STATUS` ourselves with a real Python
  deadline instead.
- `CONTROL.CAPTURE_MODE` / `CONTROL.TRIGGER_CONDITION` / `TRIGGER_MODE`
  are read-only on cores synthesized without storage qualification or
  with a single trigger probe, even though Vivado's docs list them as
  configurable. The minimal write set that survives all variants is
  `CONTROL.TRIGGER_POSITION` + `CONTROL.DATA_DEPTH`. Anything else is
  accepted optionally and silently skipped on read-only cores via
  explicit allow-list checks (no `catch` swallowing).
- `implement_debug_core` is *not* needed when `mark_debug` constraints
  are already in `constrs_1`. Vivado picks them up automatically during
  `opt_design`. Calling it explicitly tends to outlast the bridge's
  default exec_tcl timeout. This module never calls it.

The capture flow this module exposes:

    ila.configure(client, depth=4096, trigger_position=16)
    ila.set_triggers(client, values={"dbg_start": "rising"})
    ila.arm(client)
    ila.wait_for_capture(client, timeout=5.0)
    ila.export_csv(client, path="capture.csv")

For the *design-time* picture (mark_debug, debug XDC, the auto-start
generator pattern that lets ILA arm before stimulus fires), see
`references/using_ila.md`.
"""

from __future__ import annotations

from pathlib import Path
import time
from typing import Any

from ._common import fail, from_tcl_failure, ok, query_one


# ---------------------------------------------------------------------------
# enumeration
# ---------------------------------------------------------------------------

def list_ilas(client) -> dict[str, Any]:
    """Return the names of all ILA cores currently visible on the device."""
    ilas = (query_one(client, "get_hw_ilas") or "").split()
    return ok(f"{len(ilas)} ila(s)", ilas=ilas)


def list_ila_probes(
    client, *, ila: str | None = None,
) -> dict[str, Any]:
    """Enumerate probes on an ILA, returning structured per-probe info.

    Companion to `debug.list_vio_probes`: callers can iterate the
    returned `probes` list and read `name` / `width` / `port` /
    `is_trigger` / `is_data` per entry instead of parsing the flat
    `get_hw_probes` Tcl output by hand.

    If `ila` is None and only one ILA exists, it is used implicitly.

    Returns the usual ok/fail dict plus:
        ila: the ILA core name probed.
        probes: list of dicts, each with:
            - name (str)        e.g. "count" / "en" / "rst"
            - port (str)        the underlying probe port
                                 (e.g. "probe0" / "probe1")
            - width (int)
            - is_trigger (bool) whether IS_TRIGGER is set
            - is_data    (bool) whether IS_DATA is set

    Failure modes:
      - not_found    -- no ILAs on device, or named ILA missing.
      - ambiguous    -- ila=None but multiple ILAs are present.
      - tcl_error    -- propagated from `get_hw_probes` /
                        `get_property`.
    """
    target = _resolve_single_ila(client, ila)
    if isinstance(target, dict):
        return target

    raw = (query_one(
        client, f"get_hw_probes -of [get_hw_ilas {target}]"
    ) or "").strip()
    if not raw:
        return ok(f"{target}: 0 probes", ila=target, probes=[])

    full_names = raw.split()
    probes: list[dict[str, Any]] = []
    for full in full_names:
        # `get_hw_probes` returns the probe path as it appears on the
        # hw_ila object. For most probes that is just the bare name
        # (`count`, `valid_in`); when the probe was wired to a net
        # inside a sub-instance during ILA insertion, the path keeps
        # the hierarchy (`u_fir/valid_out`). Keep the full string —
        # downstream `get_hw_probes <name> -of_objects [get_hw_ilas
        # <ila>]` lookups need the exact same string the hw_probe was
        # created with. Stripping the path with rsplit was an earlier
        # mistake that silently broke set_triggers on hierarchical
        # probes (the rewrite of the IS_TRIGGER set with a `-XX`
        # don't-care found no probe to write to).
        short = full
        # The actual property names on a runtime hw_probe:
        #   WIDTH                  -- bit width (NOT PORT_WIDTH; that
        #                             property does not exist on
        #                             hw_probe objects)
        #   PROBE_PORT             -- the underlying probe port number
        #                             (e.g. 0 for probe0). NOT
        #                             HW_PROBE_PORT (does not exist).
        #   PROBE_PORT_BIT_COUNT   -- bits assigned to this probe on
        #                             that port (when a probe maps to
        #                             a slice rather than the whole
        #                             port).
        #   IS_TRIGGER / IS_DATA   -- bool capabilities
        # The earlier names (PORT_WIDTH / HW_PROBE_PORT) were guesses
        # that get_property silently returned empty for, leading to
        # bogus width=-1 / port='' results.
        wid_s = (query_one(
            client,
            f"get_property WIDTH [get_hw_probes {full}]",
        ) or "").strip()
        port_num = (query_one(
            client,
            f"get_property PROBE_PORT [get_hw_probes {full}]",
        ) or "").strip()
        is_trig = (query_one(
            client,
            f"get_property IS_TRIGGER [get_hw_probes {full}]",
        ) or "").strip().lower() in ("1", "true")
        is_data = (query_one(
            client,
            f"get_property IS_DATA [get_hw_probes {full}]",
        ) or "").strip().lower() in ("1", "true")
        try:
            wid = int(wid_s)
        except ValueError:
            wid = -1
        # Render port as `probeN` to match the IP-side naming users
        # see when they wrote the design (e.g. `connect_debug_port
        # u_ila/probe1`). PROBE_PORT alone is just the integer.
        port_label = f"probe{port_num}" if port_num else ""
        probes.append({
            "name": short,
            "port": port_label,
            "width": wid,
            "is_trigger": is_trig,
            "is_data": is_data,
        })
    return ok(
        f"{target}: {len(probes)} probe(s)",
        client=client,
        ila=target,
        probes=probes,
    )


def _resolve_single_ila(
    client, ila: str | None, **identity: Any
) -> str | dict[str, Any]:
    """Return an ILA name string, or an error dict.

    `identity` kwargs (e.g. `probe="..."`) are forwarded into every
    failure dict so callers don't lose the "what was I doing" context
    on the early-out paths. The "no ILAs at all" case also gets
    `ilas=()` for shape uniformity across the not-found / ambiguous
    branches.
    """
    ilas = (query_one(client, "get_hw_ilas") or "").split()
    if not ilas:
        return fail(
            "not_found",
            "No ILA cores on the device. Did programming complete and "
            "did the design include an ILA core (mark_debug + debug XDC)?",
            ilas=(),
            **identity,
        )
    if ila is None:
        if len(ilas) > 1:
            return fail(
                "ambiguous",
                f"Multiple ILA cores present ({ilas}); pass ila=<name>.",
                ilas=ilas,
                **identity,
            )
        return ilas[0]
    if ila not in ilas:
        return fail(
            "not_found",
            f"ILA {ila!r} not found. Available: {ilas}",
            ilas=ilas,
            ila=ila,
            **identity,
        )
    return ila


# ---------------------------------------------------------------------------
# configure
# ---------------------------------------------------------------------------

def configure(
    client,
    *,
    depth: int | None = None,
    trigger_position: int | None = None,
    ila: str | None = None,
) -> dict[str, Any]:
    """Configure the ILA's capture window.

    Only the two universally-writable controls are exposed:

        depth (CONTROL.DATA_DEPTH)
            How many samples the core records per arm. Must be one of
            the values the IP was synthesized with -- typically powers
            of two up to the configured maximum (often 1024 or 4096).
        trigger_position (CONTROL.TRIGGER_POSITION)
            Where in the buffer the trigger sample lands. 0 puts every
            sample after the trigger (useful for "fire and forget"
            captures); a value near the buffer middle keeps history
            on both sides.

    Properties that can be read-only depending on the core's synthesis
    options (CAPTURE_MODE, TRIGGER_CONDITION, TRIGGER_MODE) are not
    exposed here. If you need them, set them via `client.exec_tcl(...)`
    after calling this function and let Vivado raise the error -- we
    won't paper over a rejected write with a `catch`.
    """
    target = _resolve_single_ila(client, ila)
    if isinstance(target, dict):
        return target

    set_lines: list[str] = []
    if depth is not None:
        set_lines.append(
            f"set_property CONTROL.DATA_DEPTH {int(depth)} "
            f"[get_hw_ilas {target}]"
        )
    if trigger_position is not None:
        set_lines.append(
            f"set_property CONTROL.TRIGGER_POSITION {int(trigger_position)} "
            f"[get_hw_ilas {target}]"
        )

    for line in set_lines:
        r = client.exec_tcl(line)
        if not r.success:
            return from_tcl_failure(r, client=client)

    return ok(
        f"configured {target} "
        f"(depth={depth if depth is not None else 'unchanged'}, "
        f"trigger_position={trigger_position if trigger_position is not None else 'unchanged'})",
        ila=target,
        depth=depth,
        trigger_position=trigger_position,
    )


# ---------------------------------------------------------------------------
# trigger compare -- set_triggers (the only public trigger-set verb)
# ---------------------------------------------------------------------------
#
# Background: TRIGGER_COMPARE_VALUE persists on a probe across
# `run_hw_ila` calls. With the default CONTROL.TRIGGER_CONDITION =
# GLOBAL_AND, every probe's compare value is AND'ed into the trigger
# condition. Stale conditions on probes the caller forgot about
# silently prevent triggering, with no error message anywhere.
#
# `set_triggers(values=..., clear_others=True)` closes that footgun:
# callers can express "I want exactly these conditions and nothing
# else" in one call. The previous single-probe `set_trigger` was
# removed -- it offered no protection against the AND footgun and
# duplicated functionality already provided here.
#
# Result-shape note: like the rest of operations.*, this returns the
# common ok/fail dict (success / error_kind / message / warnings).
# On success it reads back the actual on-core compare values via
# get_property rather than echoing the requested ones, so the caller
# can see what Vivado actually canonicalised the literals to.

def _probe_dontcare_value(width: int) -> str:
    """Return a TRIGGER_COMPARE_VALUE literal that matches every input.

    Uses Vivado's per-bit don't-care character `X`. We intentionally
    avoid integer literals like `eqN'h0` here -- those would *match
    zero only*, which is the opposite of don't-care.
    """
    if width < 1:
        # Defensive: `list_ila_probes` returns -1 when WIDTH isn't an
        # integer string. We can't construct a valid literal in that
        # case; the caller should surface the underlying lookup issue.
        raise ValueError(
            f"_probe_dontcare_value: invalid width {width}"
        )
    return f"eq{width}'b" + ("X" * width)


def _expand_shorthand(value: Any, width: int) -> str:
    """Translate a user-supplied shorthand value into a Vivado literal.

    - bool True/False expand to all-1s / all-0s of the probe's width.
    - int values expand to `eqN'h<hex>` (or 'b for 1-bit).
    - "rising" / "falling" / "both" / "either" map to the edge
      characters R/F/B (1-bit only; multi-bit edges are not supported
      by Vivado anyway).
    - "X" / "x" / "*" map to all-don't-care.
    - Any other str (e.g. already a Vivado literal `eq8'hA5`) is
      passed through verbatim.

    Raises ValueError on unsupported combinations (e.g. "rising" on
    a multi-bit probe) so the caller can surface a precise message
    -- no silent fallbacks.
    """
    if isinstance(value, bool):
        bit = "1" if value else "0"
        return f"eq{width}'b" + (bit * width)
    if isinstance(value, int):
        if value < 0 or value >= (1 << width):
            raise ValueError(
                f"value {value} does not fit in {width} bits"
            )
        # Hex is the most compact and matches what Vivado prefers
        # when canonicalising back from get_property.
        digits = (width + 3) // 4
        return f"eq{width}'h" + format(value, f"0{digits}x")
    if isinstance(value, str):
        v = value
        if v in ("rising", "falling", "both", "either"):
            if width != 1:
                raise ValueError(
                    f"edge shorthand {v!r} only valid on 1-bit probes "
                    f"(got width={width}). Use a Vivado literal "
                    f"(e.g. \"eq{width}'h<value>\") instead."
                )
            return {"rising": "eq1'bR", "falling": "eq1'bF",
                    "both": "eq1'bB", "either": "eq1'bB"}[v]
        if v in ("X", "x", "*"):
            return _probe_dontcare_value(width)
        # Pass through. Vivado will reject malformed literals on
        # set_property and we surface that verbatim.
        return v
    raise ValueError(
        f"unsupported value type {type(value).__name__}: {value!r}"
    )


def set_triggers(
    client,
    *,
    values: dict[str, Any],
    clear_others: bool = True,
    ila: str | None = None,
) -> dict[str, Any]:
    """Atomically set per-probe trigger conditions on an ILA.

    The fix for the trigger-AND footgun (see the section comment above
    this function). Lets the caller say "trigger when direction rises
    and mode_select is high, ignore everything else" in one call:

        ila.set_triggers(client, values={
            "direction":   "rising",
            "mode_select": True,
        })

    Args:
        values: dict {probe_name: compare_value}. Each value can be:
            - a Vivado literal string (e.g. "eq8'h2A") -- passed through
            - True / False                 -- all-1s / all-0s
            - int                          -- hex literal of probe width
            - "rising" / "falling" / "both" / "either" -- 1-bit edges
            - "X" / "x" / "*"              -- don't-care
        clear_others: if True (default), every probe NOT in `values`
            is reset to don't-care first, so only the listed conditions
            constrain the trigger. If False, only the listed probes
            are written and other probes' previous compare values
            persist. Default True is the recommended choice -- it's
            why this function exists.
        ila: ILA core name. Auto-resolved if only one exists.

    Returns ok/fail dict plus:
        ila: ILA name acted on.
        trigger_condition: current CONTROL.TRIGGER_CONDITION.
            Reported as GLOBAL_AND on Vivado 2024.1 and as AND on
            Vivado 2021.1 -- behaviour is identical, only the
            property string differs across releases.
        set:       dict {probe: literal} of probes the caller named,
                   with the value Vivado read back after set_property.
        cleared:   dict {probe: literal} of probes reset to don't-care
                   (only populated when clear_others=True).
        unchanged: list of probe names whose compare value was left as
                   it was (only populated when clear_others=False).

    Failure modes:
        not_found  -- one or more `values` keys are not probes on the
                      ILA. The function fails BEFORE issuing any
                      set_property, so the on-core trigger state is
                      unchanged. The result includes:
                          unmatched: [name, ...]
                          available_probes: [name, ...]
                      so the caller can correct typos.
        ambiguous  -- ila=None but multiple ILAs are present.
        tcl_error  -- propagated from get_property / set_property
                      (e.g. width mismatch on a literal you supplied).
        client_error -- shorthand value rejected (e.g. "rising" on a
                      multi-bit probe).
    """
    target = _resolve_single_ila(client, ila, probes=list(values.keys()))
    if isinstance(target, dict):
        return target

    info = list_ila_probes(client, ila=target)
    if not info["success"]:
        return info
    probes_by_name = {p["name"]: p for p in info["probes"]}

    # Validate that every `values` key actually exists on the ILA
    # BEFORE touching any compare register. We do not partial-apply.
    requested = list(values.keys())
    unmatched = [n for n in requested if n not in probes_by_name]
    if unmatched:
        return fail(
            "not_found",
            f"set_triggers: probe(s) not on ILA {target!r}: "
            f"{', '.join(unmatched)}. "
            f"No trigger compare values were modified.",
            ila=target,
            unmatched=unmatched,
            available_probes=sorted(probes_by_name.keys()),
        )

    # Reject probes that exist but aren't trigger-capable, also up-front.
    not_trigger = [
        n for n in requested
        if not probes_by_name[n].get("is_trigger", False)
    ]
    if not_trigger:
        return fail(
            "client_error",
            f"set_triggers: probe(s) not trigger-capable on "
            f"{target!r}: {', '.join(not_trigger)}. "
            f"No trigger compare values were modified.",
            ila=target,
            not_trigger_capable=not_trigger,
        )

    # Pre-expand all requested literals so we can fail before issuing
    # any Tcl on a bad shorthand (e.g. "rising" on a multi-bit probe).
    expanded: dict[str, str] = {}
    for name, raw in values.items():
        try:
            expanded[name] = _expand_shorthand(
                raw, probes_by_name[name]["width"]
            )
        except ValueError as exc:
            return fail(
                "client_error",
                f"set_triggers: cannot expand value for {name!r}: {exc}. "
                f"No trigger compare values were modified.",
                ila=target, probe=name,
            )

    cleared: dict[str, str] = {}
    set_done: dict[str, str] = {}
    unchanged: list[str] = []

    if clear_others:
        # Reset every trigger-capable probe NOT in the requested set
        # before applying the requested ones. Doing the reset first
        # means a failure midway leaves the ILA fully reset (safer
        # than partial old-and-new state).
        for p in info["probes"]:
            if not p.get("is_trigger", False):
                continue
            if p["name"] in expanded:
                continue
            if p["width"] < 1:
                return fail(
                    "tcl_error",
                    f"Could not read WIDTH for probe {p['name']!r} "
                    f"on {target}; refusing to construct a "
                    f"don't-care literal.",
                    ila=target, probe=p["name"],
                )
            literal = _probe_dontcare_value(p["width"])
            r = client.exec_tcl(
                f"set_property TRIGGER_COMPARE_VALUE {literal} "
                f"[get_hw_probes {p['name']} "
                f"-of_objects [get_hw_ilas {target}]]"
            )
            if not r.success:
                return from_tcl_failure(r, client=client)
            actual = query_one(
                client,
                f"get_property TRIGGER_COMPARE_VALUE [get_hw_probes "
                f"{p['name']} -of_objects [get_hw_ilas {target}]]",
            )
            cleared[p["name"]] = actual if actual is not None else literal
    else:
        for p in info["probes"]:
            if not p.get("is_trigger", False):
                continue
            if p["name"] not in expanded:
                unchanged.append(p["name"])

    # Apply requested values.
    for name, literal in expanded.items():
        r = client.exec_tcl(
            f"set_property TRIGGER_COMPARE_VALUE {literal} "
            f"[get_hw_probes {name} "
            f"-of_objects [get_hw_ilas {target}]]"
        )
        if not r.success:
            return from_tcl_failure(r, client=client)
        actual = query_one(
            client,
            f"get_property TRIGGER_COMPARE_VALUE [get_hw_probes "
            f"{name} -of_objects [get_hw_ilas {target}]]",
        )
        set_done[name] = actual if actual is not None else literal

    cond = (query_one(
        client,
        f"get_property CONTROL.TRIGGER_CONDITION [get_hw_ilas {target}]",
    ) or "").strip()

    parts = [f"set {len(set_done)} trigger(s) on {target}"]
    if cleared:
        parts.append(f"cleared {len(cleared)} other probe(s)")
    if unchanged:
        parts.append(f"left {len(unchanged)} probe(s) unchanged")
    msg = "; ".join(parts)

    return ok(
        msg,
        client=client,
        ila=target,
        trigger_condition=cond,
        set=set_done,
        cleared=cleared,
        unchanged=unchanged,
    )


# ---------------------------------------------------------------------------
# arm + wait
# ---------------------------------------------------------------------------

def arm(client, *, ila: str | None = None) -> dict[str, Any]:
    """Arm the ILA -- equivalent to clicking "Run trigger".

    Returns immediately; the core then waits in hardware for its
    trigger condition. Use `wait_for_capture` to block until data is
    ready (or a timeout expires).
    """
    target = _resolve_single_ila(client, ila)
    if isinstance(target, dict):
        return target
    r = client.exec_tcl(f"run_hw_ila [get_hw_ilas {target}]")
    if not r.success:
        return from_tcl_failure(r, client=client)
    return ok(f"armed {target}", ila=target)


def get_status(
    client, *, ila: str | None = None,
) -> dict[str, Any]:
    """Read the live status of an ILA core.

    The property is `STATUS.CORE_STATUS` on the hw_ila object -- not
    `CORE_STATUS` (which doesn't exist and earlier versions of this
    helper used by mistake, costing a real debug session).

    Observed on Vivado 2024.1 / PYNQ-Z1:
        "IDLE"                  -- not armed (or already drained)
        "WAITING FOR TRIGGER"   -- armed, no trigger seen yet
        "FULL"                  -- buffer full, ready to upload

    Older Vivado (and Vivado docs) also describe values like
    "Pre-Trigger" / "Post-Trigger" / "Idle  Has Data" with mixed case.
    The string is unstructured and varies between versions; we return
    it raw and *also* a normalised lowercase form in `status_lower`
    so callers can match without worrying about case. We also pull
    `STATUS.SAMPLE_COUNT` as `sample_count` -- it's the most reliable
    "did anything actually get captured" signal across versions.
    """
    target = _resolve_single_ila(client, ila)
    if isinstance(target, dict):
        return target
    raw = query_one(
        client,
        f"get_property STATUS.CORE_STATUS [get_hw_ilas {target}]",
    )
    if raw is None:
        return fail(
            "tcl_error",
            f"Could not read STATUS.CORE_STATUS for {target}.",
            ila=target,
        )
    sample_count_raw = query_one(
        client,
        f"get_property STATUS.SAMPLE_COUNT [get_hw_ilas {target}]",
    )
    try:
        sample_count = (
            int(sample_count_raw) if sample_count_raw is not None
            and sample_count_raw != "" else None
        )
    except ValueError:
        sample_count = None
    return ok(
        f"{target}: {raw}" + (
            f" (samples={sample_count})" if sample_count is not None else ""
        ),
        ila=target,
        status=raw,
        status_lower=raw.lower(),
        sample_count=sample_count,
    )


def wait_for_capture(
    client,
    *,
    timeout: float = 10.0,
    poll: float = 0.2,
    ila: str | None = None,
) -> dict[str, Any]:
    """Block (in Python, not Tcl) until the ILA reports a full capture.

    Polls `STATUS.CORE_STATUS` every `poll` seconds for up to `timeout`
    seconds. We deliberately do not use `wait_on_hw_ila`: its `-timeout`
    argument has unreliable units in some Vivado versions, and worse,
    it blocks Vivado's Tcl interpreter, which in turn blocks the bridge.

    Completion criteria (case-insensitive on the status string, and we
    also gate on `STATUS.SAMPLE_COUNT` so we don't fire on a momentary
    "Idle" the ILA reports after auto-draining a previous capture):

        - "FULL" / "Full"            -- buffer full
        - "Has Data" / "HAS DATA"    -- older Vivado wording for the same
        - "IDLE"/"Idle" with sample_count > 0 -- ILA already drained,
          but a real capture is sitting in hw_ila_data ready to upload

    On timeout we return `error_kind="timeout"` and *do not* reset the
    ILA -- the user might want to inspect why it never triggered, or
    extend the timeout and call again. If you do want to reset between
    attempts, issue `reset_hw_ila` via `client.exec_tcl(...)` yourself.
    """
    target = _resolve_single_ila(client, ila)
    if isinstance(target, dict):
        return target

    deadline = time.time() + timeout
    last_status = ""
    last_samples: int | None = None
    while True:
        s = get_status(client, ila=target)
        if not s["success"]:
            return s
        last_status = s["status"]
        last_samples = s.get("sample_count")

        upper = last_status.upper().strip()
        # Direct full / has-data signals (case-insensitive).
        if upper.startswith("FULL") or "HAS DATA" in upper:
            return ok(
                f"{target} captured ({last_status}, samples={last_samples})",
                ila=target,
                status=last_status,
                sample_count=last_samples,
            )
        # Idle race: some configurations bounce back to IDLE between
        # arming and the moment we poll, but the underlying buffer is
        # already filled. SAMPLE_COUNT > 0 is a reliable tell-tale.
        if upper == "IDLE" and last_samples is not None and last_samples > 0:
            return ok(
                f"{target} captured (IDLE with samples={last_samples})",
                ila=target,
                status=last_status,
                sample_count=last_samples,
            )
        if time.time() >= deadline:
            return fail(
                "timeout",
                f"{target} did not fill its capture buffer within "
                f"{timeout:.1f}s (last status: {last_status!r}, "
                f"samples={last_samples}). Check that the trigger "
                f"condition is reachable and that the design is "
                f"actually clocking.",
                ila=target,
                status=last_status,
                sample_count=last_samples,
            )
        time.sleep(poll)


# ---------------------------------------------------------------------------
# export
# ---------------------------------------------------------------------------

def export_csv(
    client,
    *,
    path: str | Path,
    ila: str | None = None,
) -> dict[str, Any]:
    """Upload the latest capture and write it to `path` as CSV.

    Wraps `upload_hw_ila_data` + `write_hw_ila_data -csv_file`. The
    parent directory is created if missing -- the bridge blocks
    `file mkdir` from Tcl, but Python's host-side `Path.mkdir` is
    fine. We do `mkdir(parents=True, exist_ok=True)` so a fresh
    `results/` directory doesn't trip up the call.

    Path quoting: paths can contain spaces (Windows `C:/Program Files/...`).
    A naïve `{csv_str}` brace wrap breaks if the path itself contains
    unbalanced braces, so we use Tcl's `[list ...]` constructor to
    have Tcl quote the path for us.

    We pass the data object via `[get_hw_ila_datas -of_objects ...]`
    rather than relying on the string returned by `upload_hw_ila_data`
    -- that string is e.g. `hw_ila_data_1`, but if the previous session
    left numbered duplicates around it can suddenly become
    `hw_ila_data_2`, and the bare-name capture is fragile. The
    of_objects form always points at the capture we just uploaded.
    """
    target = _resolve_single_ila(client, ila)
    if isinstance(target, dict):
        return target

    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    r = client.exec_tcl(
        f"upload_hw_ila_data [get_hw_ilas {target}]"
    )
    if not r.success:
        # Failure path also carries csv_path so callers that uncritically
        # do `r['csv_path']` get a value (still want to guard on success
        # before reading it, but a missing key was an extra footgun).
        out = from_tcl_failure(r, client=client)
        out["ila"] = target
        out["csv_path"] = str(out_path)
        return out

    csv_str = str(out_path).replace("\\", "/")
    # `[list <path>]` lets Tcl handle whatever quoting the path needs.
    r = client.exec_tcl(
        f"write_hw_ila_data -force "
        f"-csv_file [lindex [list {{{csv_str}}}] 0] "
        f"[lindex [get_hw_ila_datas -of_objects [get_hw_ilas {target}]] end]"
    )
    if not r.success:
        out = from_tcl_failure(r, client=client)
        out["ila"] = target
        out["csv_path"] = str(out_path)
        return out
    if not out_path.exists():
        return fail(
            "tcl_error",
            f"write_hw_ila_data reported success but {out_path} is missing.",
            ila=target,
            csv_path=str(out_path),
        )
    return ok(
        f"{target} -> {out_path}",
        client=client,
        ila=target,
        csv_path=str(out_path),
        bytes=out_path.stat().st_size,
    )


# ---------------------------------------------------------------------------
# CSV parsing (host-side; no Vivado interaction)
# ---------------------------------------------------------------------------

def parse_csv(
    csv_path: str | Path,
    *,
    signed_columns: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Parse a Vivado-exported ILA CSV into header + decoded rows.

    Vivado's ILA CSV layout (Vivado 2024.1):

        Sample in Buffer,Sample in Window,TRIGGER,<probe1>[N:0],<probe2>,...
        Radix - UNSIGNED,UNSIGNED,UNSIGNED,HEX,HEX,...
        0,0,0,0001fffe,0,...
        ...

    Returns a dict with:
        columns      -- column names with bit-slice suffix preserved
                        (e.g. 'sp_obs[31:0]'). Use `find_column(columns, ...)`
                        to look up by base name.
        radix        -- per-column radix from the second header row.
        rows         -- list of dicts: {column_name -> int}. Hex/binary
                        strings are decoded; the unsigned counter columns
                        come back as ints. Multi-bit columns named in
                        `signed_columns={'fir_out': 16, 'sp_obs': 32}`
                        are sign-extended within the given width;
                        any other multi-bit column is treated as unsigned.

    The function is intentionally Vivado-free: callers can run it on a
    CSV after the bridge connection is gone, on a different machine,
    etc. It uses only the standard library.
    """
    import csv as _csv

    p = Path(csv_path)
    if not p.exists():
        # Surface this as a normal failure dict rather than letting
        # `p.open()` raise FileNotFoundError. Every other operation in
        # the bridge returns a result dict on failure (per pitfall #5);
        # parse_csv is callable without a Vivado connection so it
        # would otherwise be the only operation that throws.
        return fail(
            "not_found",
            f"CSV file does not exist: {p}",
            csv_path=str(p),
        )
    rows_out: list[dict[str, int | None]] = []
    decode_failures: list[tuple[int, str, str]] = []  # (row_idx, col, token)
    # Track signed_columns names that didn't match any CSV column. We
    # only need to compute this once per parse, after columns is known
    # (we set this below the columns-detection block).
    signed_columns_skipped: list[str] = []
    with p.open() as f:
        rdr = _csv.reader(f)
        columns: list[str] | None = None
        radix: list[str] = []
        sample_idx = -1
        for row in rdr:
            if not row:
                continue
            first = row[0].strip()
            if columns is None:
                if first.startswith("Sample in Buffer"):
                    columns = [c.strip() for c in row]
                continue
            if first.startswith("Radix"):
                radix = [c.strip() for c in row]
                continue
            if not first or not first[0].isdigit():
                continue
            sample_idx += 1
            rec: dict[str, int | None] = {}
            for i, raw in enumerate(row):
                name = columns[i] if i < len(columns) else f"col{i}"
                col_radix = radix[i] if i < len(radix) else "HEX"
                token = raw.strip()
                if not token:
                    # Empty cell: this is rare in Vivado output but
                    # plausible at end-of-line. Mark as None rather than
                    # silently inserting 0; callers can decide whether
                    # to treat that as a failure.
                    rec[name] = None
                    decode_failures.append((sample_idx, name, ""))
                    continue
                value = _decode_token(token, col_radix)
                if value is None:
                    # Could not parse the cell -- typically xsim 'x'/'X'
                    # uninitialised values, or an unexpected radix from
                    # a future Vivado version. Surface this in warnings
                    # so the caller doesn't quietly compute on a 0 that
                    # came from an undecoded sample.
                    rec[name] = None
                    decode_failures.append((sample_idx, name, token))
                else:
                    rec[name] = value
            # Apply signed extension where requested. Bit-slice suffix
            # is stripped so callers pass plain probe names. Names that
            # don't match any column are recorded into
            # `signed_columns_skipped` so a typo doesn't fail silently.
            if signed_columns:
                for name, width in signed_columns.items():
                    col = find_column(columns, name) if columns else None
                    if col is None:
                        continue
                    actual = columns[col]
                    v = rec.get(actual)
                    if v is None:
                        continue
                    if v & (1 << (width - 1)):
                        rec[actual] = v - (1 << width)
            rows_out.append(rec)

    if columns is None:
        return fail(
            "parse_failed",
            f"No 'Sample in Buffer' header row found in {p}.",
            csv_path=str(p),
        )
    # Compute which signed_columns names didn't match any column in the
    # parsed CSV header. find_column matches base names (stripping the
    # `[N:0]` slice suffix), so a typo or a renamed probe will surface
    # here rather than silently failing to apply the signed transform.
    if signed_columns:
        for name in signed_columns:
            if find_column(columns, name) is None:
                signed_columns_skipped.append(name)
    warnings: list[str] = []
    if signed_columns_skipped:
        warnings.append(
            f"signed_columns entries with no matching CSV column: "
            f"{signed_columns_skipped}. The values for those names "
            f"are returned as unsigned (treat as a typo if the column "
            f"was supposed to exist)."
        )
    if decode_failures:
        # Include up to 5 samples for at-a-glance triage; the full list
        # is in `decode_failures` for callers that want to act on it.
        sample = decode_failures[:5]
        sample_str = ", ".join(
            f"row{r} col={c!r} token={t!r}" for r, c, t in sample
        )
        warnings.append(
            f"{len(decode_failures)} cell(s) could not be decoded "
            f"(stored as None). First: {sample_str}. Common cause: "
            f"xsim 'x'/'X' uninitialised values from BRAM that wasn't "
            f"zero-initialised; see using_simulation.md."
        )
    return ok(
        f"parsed {len(rows_out)} sample(s) from {p.name}"
        + (f" ({len(decode_failures)} undecoded cell(s))"
           if decode_failures else ""),
        csv_path=str(p),
        columns=columns,
        radix=radix,
        rows=rows_out,
        decode_failures=decode_failures,
        signed_columns_skipped=signed_columns_skipped,
        warnings=warnings,
    )


def find_column(columns: list[str], target: str) -> int | None:
    """Look up a column by base probe name.

    Vivado writes column names like `sp_obs[31:0]`, but callers think
    in terms of the bare wire name `sp_obs`. This helper handles the
    bit-slice suffix transparently. Returns the index of the first
    column whose name is `target` or starts with `target[`. None
    if no column matches.
    """
    for i, c in enumerate(columns):
        if c == target or c.startswith(target + "["):
            return i
    return None


def _decode_token(token: str, radix: str) -> int | None:
    """Decode an ILA CSV cell using its radix.

    The CSV's radix row uses entries like `Radix - UNSIGNED`,
    `UNSIGNED`, `HEX`, `BINARY`, `SIGNED`. We accept either form (with
    or without the `Radix - ` prefix). Returns None for tokens that
    don't parse; callers can decide whether to treat that as an error.
    """
    radix = radix.upper().replace("RADIX -", "").strip()
    try:
        if radix == "BINARY":
            return int(token, 2)
        if radix == "UNSIGNED":
            # Vivado writes UNSIGNED counters in decimal.
            return int(token)
        # HEX and SIGNED come back as hex digit strings.
        return int(token, 16)
    except ValueError:
        return None
