"""Debug-core operations: VIO IP creation, read/write VIO probes.

Build-time:
  - `create_vio(...)` adds a VIO IP to the project from a structured
    description (output / input probe lists with widths and inits).

Runtime (after program_device):
  - `list_vios` / `list_vio_probes` enumerate cores and probes.
  - `read_vio_probe` / `read_vio_probe_int` / `write_vio_probe` /
    `write_vio_probes` drive a programmed VIO.

ILA capture lives in `operations.ila` -- see [op_ila.md] / [op_debug.md].
This module no longer carries an `ila` entry point; use
`from operations import ila` directly.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ._common import fail, from_tcl_failure, ok, query_one, tcl_str


# ---------------------------------------------------------------------------
# VIO
# ---------------------------------------------------------------------------

def list_vios(client) -> dict[str, Any]:
    """Return the names of all VIO cores currently visible on the device."""
    vios = (query_one(client, "get_hw_vios") or "").split()
    return ok(f"{len(vios)} vio(s)", vios=vios)


def list_vio_probes(client, *, vio: str | None = None) -> dict[str, Any]:
    """Enumerate probes on a VIO.

    If `vio` is None and only one VIO exists, it is used implicitly.
    Returns probe name + direction + width + radix per probe.

    Radix is the runtime *value-encoding* of the probe (HEX, BINARY,
    UNSIGNED, SIGNED). It governs how `OUTPUT_VALUE` / `INPUT_VALUE`
    literals are interpreted by Vivado, so any code that wants to
    write or read raw values must consult it (see write_vio_probe).

    Implementation note: width and radix are read directly off the
    runtime hw_probe object via `get_property WIDTH` /
    `OUTPUT_VALUE_RADIX` / `INPUT_VALUE_RADIX`. We intentionally do
    not try to recover them from the IP's CONFIG.C_PROBE_INn_WIDTH
    -- runtime probes get renamed when an ILA shares wires with a VIO
    (pitfall #6), and the IP-port-to-runtime-probe mapping is fragile.
    Reading off the runtime probe matches what `set_property` /
    `get_property` will validate against, which is what callers care
    about.
    """
    target = _resolve_single_vio(client, vio)
    if isinstance(target, dict):  # error
        return target

    names = (query_one(
        client,
        f"get_hw_probes -of_objects [get_hw_vios {target}]",
    ) or "").split()
    if not names:
        return ok(
            "no probes (is the .ltx attached? "
            "see hardware.program_device(ltx_path=...))",
            vio=target,
            probes=[],
        )

    probes: list[dict[str, Any]] = []
    for name in names:
        # TYPE is "probe_in" or "probe_out". WIDTH is integer.
        ptype = query_one(
            client,
            f"get_property TYPE [get_hw_probes {name} "
            f"-of_objects [get_hw_vios {target}]]",
        ) or ""
        width = query_one(
            client,
            f"get_property WIDTH [get_hw_probes {name} "
            f"-of_objects [get_hw_vios {target}]]",
        ) or ""
        # Vivado returns TYPE as "vio_input" / "vio_output" (sometimes
        # "probe_in" / "probe_out" in older releases). Normalise to a
        # plain "in"/"out" string but keep the raw value for debugging.
        if "input" in ptype or ptype == "probe_in":
            direction = "in"
            radix_prop = "INPUT_VALUE_RADIX"
        elif "output" in ptype or ptype == "probe_out":
            direction = "out"
            radix_prop = "OUTPUT_VALUE_RADIX"
        else:
            direction = ptype  # unknown -- expose as-is
            radix_prop = None
        radix = ""
        if radix_prop is not None:
            radix = (query_one(
                client,
                f"get_property {radix_prop} [get_hw_probes {name} "
                f"-of_objects [get_hw_vios {target}]]",
            ) or "").upper()
        probes.append({
            "name": name,
            "direction": direction,
            "type_raw": ptype,
            "width": int(width) if width.isdigit() else width,
            "radix": radix,
        })
    return ok(f"{len(probes)} probe(s) on {target}", vio=target, probes=probes)


def read_vio_probe(
    client,
    *,
    probe: str,
    vio: str | None = None,
    refresh: bool = True,
    as_int: bool = False,
) -> dict[str, Any]:
    """Read a single VIO input probe's current value.

    Args:
        probe: probe name (e.g. "probe_led"; matches the wire name fed
            into the VIO IP at instantiation).
        vio: VIO core name. None auto-selects when only one VIO exists.
        refresh: call `refresh_hw_vio` first to grab a fresh sample.
        as_int: also include an `int_value` field decoded from the raw
            string using the probe's WIDTH + INPUT_VALUE_RADIX. SIGNED
            probes are returned as Python signed ints (two's-complement
            applied). On decode failure (unexpected radix, malformed
            response) the raw `value` is still returned and a warning
            is emitted; we intentionally do not fall back to a guess.

    Failure modes:
        not_found        -- probe doesn't exist on the chosen VIO
        wrong_direction  -- probe exists but is an output (use write_vio_probe)
    """
    target = _resolve_single_vio(client, vio, probe=probe)
    if isinstance(target, dict):
        return target

    direction = _probe_direction(client, target, probe)
    if direction is None:
        return fail(
            "not_found",
            f"Probe {probe!r} not found on VIO {target!r}. "
            f"Use list_vio_probes() to see what's available.",
            vio=target,
            probe=probe,
        )
    if direction != "in":
        return fail(
            "wrong_direction",
            f"Probe {probe!r} on VIO {target!r} is an output probe; "
            f"use write_vio_probe() instead of read_vio_probe().",
            vio=target,
            probe=probe,
            direction=direction,
        )

    if refresh:
        rr = client.exec_tcl(f"refresh_hw_vio [get_hw_vios {target}]")
        if not rr.success:
            # If we couldn't refresh, the next read would return a
            # cached value -- which is exactly the "looks fine but
            # is stale" failure mode the bridge avoids elsewhere.
            return from_tcl_failure(
                rr, error_kind="refresh_failed", client=client,
                vio=target, probe=probe,
            )

    val = query_one(
        client,
        f"get_property INPUT_VALUE "
        f"[get_hw_probes {probe} -of_objects [get_hw_vios {target}]]",
    )
    # query_one returns None only when the underlying Tcl call itself
    # failed (refresh races, hw target dropped, etc.). Surface that as
    # an explicit failure rather than papering over it with value="" --
    # see pitfall #5: op-specific fields belong only on success results.
    if val is None:
        return fail(
            "tcl_error",
            f"Failed to read INPUT_VALUE for {target}/{probe}. "
            f"The probe exists but the get_property call failed; "
            f"check the bridge response or Tcl Console for details.",
            vio=target,
            probe=probe,
        )

    extra: dict[str, Any] = {}
    warnings: list[str] = []
    if as_int:
        width, radix = _probe_width_radix(
            client, target, probe, value_prop="INPUT_VALUE_RADIX",
        )
        if width is None or radix is None:
            warnings.append(
                f"as_int requested but could not read WIDTH/INPUT_VALUE_RADIX "
                f"for {probe}; raw value preserved."
            )
        else:
            decoded = _decode_vio_value(val, width, radix)
            if decoded is None:
                warnings.append(
                    f"as_int requested but could not decode {val!r} as "
                    f"{radix} (width={width}); raw value preserved."
                )
            else:
                extra["int_value"] = decoded
                extra["radix"] = radix
                extra["width"] = width
    return ok(
        f"{target}/{probe} = {val}",
        vio=target,
        probe=probe,
        value=val,
        warnings=warnings,
        **extra,
    )


def read_vio_probes_all(
    client,
    *,
    vio: str | None = None,
    refresh: bool = True,
) -> dict[str, Any]:
    """Read every input probe on a VIO. Output probes are skipped.

    Returns a dict {probe_name: value_string}. `value` strings come
    straight from Vivado (binary by default, e.g. "0", "1",
    "00000011010..."), so callers that want ints can convert themselves.
    """
    target = _resolve_single_vio(client, vio)
    if isinstance(target, dict):
        return target

    if refresh:
        rr = client.exec_tcl(f"refresh_hw_vio [get_hw_vios {target}]")
        if not rr.success:
            # If we couldn't refresh, the next read would return a
            # cached value -- which is exactly the "looks fine but
            # is stale" failure mode the bridge avoids elsewhere.
            return from_tcl_failure(
                rr, error_kind="refresh_failed", client=client,
                vio=target,
            )

    info = list_vio_probes(client, vio=target)
    if not info["success"]:
        return info

    values: dict[str, str] = {}
    failed: list[str] = []
    for p in info["probes"]:
        if p["direction"] != "in":
            continue
        v = query_one(
            client,
            f"get_property INPUT_VALUE "
            f"[get_hw_probes {p['name']} -of_objects [get_hw_vios {target}]]",
        )
        if v is None:
            # Don't silently substitute "" -- record which probe(s) we
            # could not read and fail the whole call. The caller can
            # retry per-probe with read_vio_probe if they want partial
            # results. (pitfall #5: op-specific fields only on success.)
            failed.append(p["name"])
            continue
        values[p["name"]] = v
    if failed:
        return fail(
            "tcl_error",
            f"Failed to read INPUT_VALUE for probe(s) on {target}: "
            f"{', '.join(failed)}. Use read_vio_probe() per-probe to "
            f"isolate the failure.",
            vio=target,
            failed_probes=failed,
        )
    return ok(
        f"read {len(values)} probe(s) from {target}",
        vio=target,
        values=values,
    )


def write_vio_probe(
    client,
    *,
    probe: str,
    value: int | str,
    vio: str | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Write a value to a VIO output probe.

    Args:
        probe: probe name (must be an output probe).
        value:
            - int: encoded automatically using the probe's WIDTH and
              OUTPUT_VALUE_RADIX. Negatives are represented two's-complement
              within the probe width (so -1 on a 32-bit signed probe sends
              `ffffffff`). This is the recommended path -- callers don't
              have to worry about Vivado's character-count rules.
            - str: passed through as-is. Use this only when you have an
              already-formatted Vivado literal (e.g. a binary string that
              you generated yourself). Width *and* digit count must match
              what Vivado expects for that radix, or you'll get a Designutils
              "has [N] value characters; required [M]" error.
        vio: VIO name. Auto-resolved if only one exists.
        commit: call `commit_hw_vio` after setting (default True). With
            False the value is staged but not pushed to the device,
            which is occasionally useful when writing several probes
            atomically -- but `write_vio_probes()` is usually a better
            fit for that.

    Failure modes:
        not_found        -- probe doesn't exist on the chosen VIO
        wrong_direction  -- probe exists but is an input (use read_vio_probe)
        invalid_value    -- can't encode the int (unknown radix, width
                            unreadable, value out of range, etc.)
        tcl_error        -- Vivado rejected the formatted literal
                            (typically a width mismatch when value is a str)
    """
    target = _resolve_single_vio(client, vio, probe=probe)
    if isinstance(target, dict):
        return target

    direction = _probe_direction(client, target, probe)
    if direction is None:
        return fail(
            "not_found",
            f"Probe {probe!r} not found on VIO {target!r}. "
            f"Use list_vio_probes() to see what's available.",
            vio=target,
            probe=probe,
        )
    if direction != "out":
        return fail(
            "wrong_direction",
            f"Probe {probe!r} on VIO {target!r} is an input probe; "
            f"use read_vio_probe() instead of write_vio_probe().",
            vio=target,
            probe=probe,
            direction=direction,
        )

    if isinstance(value, int) and not isinstance(value, bool):
        encoded = _encode_vio_value(client, target, probe, value)
        if isinstance(encoded, dict):
            return encoded
        literal = encoded
    else:
        # Caller-supplied literal. Trust them, let Vivado validate.
        literal = str(value)

    r = client.exec_tcl(
        f"set_property OUTPUT_VALUE {literal} "
        f"[get_hw_probes {probe} -of_objects [get_hw_vios {target}]]"
    )
    if not r.success:
        return from_tcl_failure(r, client=client)

    if commit:
        r = client.exec_tcl(
            f"commit_hw_vio [get_hw_probes {probe} "
            f"-of_objects [get_hw_vios {target}]]"
        )
        if not r.success:
            return from_tcl_failure(r, client=client)
    return ok(
        f"{target}/{probe} <- {literal}{' (committed)' if commit else ''}",
        vio=target,
        probe=probe,
        value=literal,
        committed=commit,
    )


def write_vio_probes(
    client,
    *,
    values: dict[str, int | str],
    vio: str | None = None,
) -> dict[str, Any]:
    """Write several VIO output probes and commit once.

    All probes are staged with `commit=False`, then a single
    `commit_hw_vio` is issued for the whole VIO so the device sees
    a coherent update. If any single set fails, the function aborts
    *without* committing -- the device state therefore stays whatever
    it was before the call. Earlier successful sets in the same call
    are also discarded by Vivado because no commit ran.

    This is the preferred way to drive multiple control bits together
    (e.g. mode + start + run_len), because it avoids the "set + commit
    chained via `;`" anti-pattern that silently swallows set errors
    when written by hand in raw Tcl.
    """
    target = _resolve_single_vio(client, vio, probes=list(values.keys()))
    if isinstance(target, dict):
        return target

    written: dict[str, str] = {}
    for name, raw in values.items():
        r = write_vio_probe(
            client, probe=name, value=raw, vio=target, commit=False,
        )
        if not r["success"]:
            r["message"] = (
                f"write_vio_probes aborted at {name!r}: {r['message']} "
                f"(no commit was issued; previously staged probes are discarded)"
            )
            r["failed_probe"] = name
            r["staged_before_failure"] = written
            return r
        written[name] = r["value"]

    r = client.exec_tcl(
        f"commit_hw_vio [get_hw_vios {target}]"
    )
    if not r.success:
        return from_tcl_failure(r, client=client)
    return ok(
        f"wrote {len(written)} probe(s) on {target} (committed)",
        vio=target,
        values=written,
    )


# ---------------------------------------------------------------------------
# ILA -- moved to operations.ila
# ---------------------------------------------------------------------------
# Use `from operations import ila`. This module deliberately does not
# re-export `list_ilas` etc. so there is exactly one canonical entry
# point for each ILA verb.


# ---------------------------------------------------------------------------
# Build-time: VIO IP creation
# ---------------------------------------------------------------------------

def create_vio(
    client,
    *,
    name: str = "vio_0",
    outputs: list[dict] | None = None,
    inputs: list[dict] | None = None,
    enable_activity_detection: bool = False,
    vendor: str = "xilinx.com",
    library: str = "ip",
    ip_name: str = "vio",
    version: str | None = None,
    overwrite: bool = False,
    generate_synth_checkpoint: bool = True,
) -> dict[str, Any]:
    """Add (or replace) a Xilinx VIO IP in the project.

    Wraps `create_ip` + `set_property -dict {...}` so callers describe
    the VIO with a structured Python dict instead of hand-writing the
    `CONFIG.C_*` property names. Only the build-time customization
    is done here -- you still need to instantiate the IP in HDL and
    run synth/impl/program afterwards.

    Verified on Vivado 2021.1 and 2024.1. The VIO IP's CONFIG
    schema is the same in both: per-probe `C_PROBE_OUTn_WIDTH` /
    `C_PROBE_OUTn_INIT_VAL` / `C_PROBE_INn_WIDTH`, plus a single
    global `C_EN_PROBE_IN_ACTIVITY` flag for runtime activity
    detection. There is no per-probe edge-type selection.

    Args:
        name: instance name for the IP (matches what you'll write
              in HDL, e.g. `vio_0 u_vio (...)`).
        outputs: list of probe descriptions. Each is a dict with:
                   - "width" (int, required): probe width in bits
                   - "init"  (int, optional, default 0): initial value
                              the VIO drives at power-on. Must fit
                              in `width` bits.
        inputs:  list of probe descriptions. Each is a dict with:
                   - "width"    (int, required): probe width in bits
        enable_activity_detection: when True, sets
              `CONFIG.C_EN_PROBE_IN_ACTIVITY` on the IP -- this
              enables Vivado's runtime activity reporting on every
              input probe (read at runtime via the probe's
              `ACTIVITY_VALUE` property). Per-probe edge-type
              selection (`rising` / `falling` / `either`) is NOT
              available on Vivado 2024.1's VIO IP -- the only
              user-facing knob is this single global on/off flag.
              Default False (no activity detection); enable only
              when you need to read activity at runtime.
        vendor / library / ip_name / version: which IP catalogue
              entry to instantiate. Defaults are the standard
              Xilinx VIO. `version=None` lets Vivado pick the latest
              installed version, which is what you want unless you
              are pinning to a specific tools release.
        overwrite: if True and an IP with this name already exists,
              `reset_target` + `remove_files` it first so the new
              parameters take effect. Default False (the call fails
              with `error_kind="ip_exists"` so the caller decides).
              **Disk side-effect**: Vivado's `create_ip` after
              `remove_files` reuses the IP catalogue name but writes
              the new IP into a fresh directory (`<name>_1/`,
              `<name>_2/`, ...). The original directory is left on
              disk as an empty shell. The bridge cannot delete it
              (file delete is blocked at the Tcl server). If you
              care about cleaning up, do it host-side after closing
              the project.
        generate_synth_checkpoint: passed straight to the IP's
              GENERATE_SYNTH_CHECKPOINT property. True is the safe
              default and is what enables the OOC synth flow described
              in `using_vio.md` §1.

    Returns the usual ok/fail dict plus:
        ip:                  instance name actually used.
        xci:                 absolute path to the generated `.xci`
                             (Vivado's record of the IP, useful for
                             `git_management.md` purposes).
        activity_detection:  read-back of CONFIG.C_EN_PROBE_IN_ACTIVITY
                             on the customised IP. `True` / `False`
                             when there are input probes, `None` when
                             the IP has only output probes (the
                             property does not apply). Use this to
                             confirm the IP committed to what you
                             asked for -- the realised value, not the
                             request.

    Failure modes:
        - `client_error`     -- bad probe spec (missing width, init out
          of range, unknown activity value, no probes at all).
        - `ip_exists`        -- an IP with this name already exists and
          overwrite=False.
        - `overwrite_failed` -- the overwrite sequence ran but the IP
          name is still listed in `get_ips` afterwards (see comment
          in source).
        - `tcl_error`        -- propagated from `create_ip` /
          `set_property` (e.g. wrong vendor/library/version).

    The actual OOC synthesis of the IP (so its dcp lands on disk before
    the parent `synth_design` references it) is a separate step --
    see `using_vio.md` §1 for `create_ip_run` + `launch_runs`. This
    operation only customises the IP; it does not synthesize it.
    """
    outputs = list(outputs or [])
    inputs = list(inputs or [])
    if not outputs and not inputs:
        return fail(
            "client_error",
            "create_vio needs at least one output or input probe; "
            "got both empty lists.",
        )

    # Validate probe specs early so the user gets a precise error
    # before any Tcl is sent.
    def _normalise(probes, kind):  # kind: "out" | "in"
        out_specs = []
        for i, spec in enumerate(probes):
            if "width" not in spec:
                return None, fail(
                    "client_error",
                    f"{kind}_probe[{i}] missing required 'width'",
                )
            w = int(spec["width"])
            if w < 1 or w > 256:
                return None, fail(
                    "client_error",
                    f"{kind}_probe[{i}] width out of range [1..256]: {w}",
                )
            init = int(spec.get("init", 0)) if kind == "out" else 0
            if kind == "out" and (init < 0 or init >= (1 << w)):
                return None, fail(
                    "client_error",
                    f"out_probe[{i}] init {init:#x} does not fit in "
                    f"{w} bits",
                )
            out_specs.append((w, init))
        return out_specs, None

    out_specs, err = _normalise(outputs, "out")
    if err is not None:
        return err
    in_specs, err = _normalise(inputs, "in")
    if err is not None:
        return err

    # Existence check.
    existing = (query_one(client, f"get_ips -quiet {name}") or "").strip()
    if existing:
        if not overwrite:
            return fail(
                "ip_exists",
                f"An IP named {name!r} already exists. Pass "
                f"overwrite=True to remove and re-create it.",
                ip=name,
            )
        # Full-replacement sequence. Order matters:
        # 1. delete_runs the IP's OOC synth/impl runs. While these
        #    runs exist they hold the IP file as a dependency, so the
        #    later `remove_files` silently no-ops and `create_ip` then
        #    blows up with `[Common 17-69] IP name 'foo' is already
        #    in use`.
        # 2. reset_target + export_ip_user_files clean per-IP scratch.
        # 3. remove_files on the .xci itself (read from IP_FILE) is
        #    what actually unregisters the IP. `get_files -of [get_ips
        #    $ip]` returns empty after `export_ip_user_files -reset`
        #    on Vivado 2021.1, so resolving via IP_FILE is the only
        #    path that works across versions.
        # delete_runs is best-effort (the run may not exist on a fresh
        # project), so we use -quiet and don't error out on its return.
        for run_suffix in ("_impl_1", "_synth_1"):
            client.exec_tcl(
                f"delete_runs -quiet {name}{run_suffix}"
            )
        for cmd in (
            f"reset_target -quiet all [get_ips {name}]",
            f"export_ip_user_files -of_objects [get_ips {name}] -no_script -reset -force -quiet",
        ):
            rr = client.exec_tcl(cmd)
            if not rr.success:
                # Surface the real reason rather than press on.
                return from_tcl_failure(
                    rr, error_kind="overwrite_failed", client=client,
                )
        rr = client.exec_tcl(
            f"set _xci [get_property IP_FILE [get_ips {name}]]; "
            f"if {{$_xci eq {{}}}} {{ error \"IP {name} has empty IP_FILE\" }}; "
            f"remove_files $_xci; "
            f"return $_xci"
        )
        if not rr.success:
            return from_tcl_failure(
                rr, error_kind="overwrite_failed", client=client,
            )
        # Read back to verify the IP catalog actually released the name.
        # Without this check a silent residue would re-trigger
        # [Common 17-69] on the create_ip below.
        residue = (query_one(
            client, f"get_ips -quiet {name}"
        ) or "").strip()
        if residue:
            return fail(
                "overwrite_failed",
                f"IP {name!r} still listed in get_ips after remove_files "
                f"of {rr.output.strip()!r}; cannot reuse the name.",
            )

    # create_ip
    version_clause = f"-version {version} " if version else ""
    rcr = client.exec_tcl(
        f"create_ip -name {ip_name} -vendor {vendor} "
        f"-library {library} {version_clause}"
        f"-module_name {name}"
    )
    if not rcr.success:
        return from_tcl_failure(rcr, client=client)

    # Build CONFIG dict. The VIO IP exposes per-probe
    # CONFIG.C_PROBE_INn_WIDTH plus a single global
    # CONFIG.C_EN_PROBE_IN_ACTIVITY flag for input probes; output
    # probes have CONFIG.C_PROBE_OUTn_WIDTH and
    # CONFIG.C_PROBE_OUTn_INIT_VAL. There is no per-probe
    # edge-type knob in 2021.1 or 2024.1.
    cfg = {
        "C_NUM_PROBE_OUT": len(out_specs),
        "C_NUM_PROBE_IN": len(in_specs),
    }
    for i, (w, init) in enumerate(out_specs):
        cfg[f"C_PROBE_OUT{i}_WIDTH"] = w
        cfg[f"C_PROBE_OUT{i}_INIT_VAL"] = f"0x{init:X}"
    for i, (w, _init) in enumerate(in_specs):
        cfg[f"C_PROBE_IN{i}_WIDTH"] = w
    # Always emit C_EN_PROBE_IN_ACTIVITY when there are input probes.
    # The IP's default for this property differs across Vivado releases
    # (1 on 2021.1, observed; possibly 0 on other releases). Writing
    # the user-requested value unconditionally keeps the result
    # version-independent and makes False mean False.
    if in_specs:
        cfg["C_EN_PROBE_IN_ACTIVITY"] = 1 if enable_activity_detection else 0

    # GENERATE_SYNTH_CHECKPOINT lives on the IP, not on CONFIG.
    cfg_pairs = " ".join(
        f"CONFIG.{k} {{{v}}}" for k, v in cfg.items()
    )
    rsp = client.exec_tcl(
        f"set_property -dict [list {cfg_pairs}] [get_ips {name}]"
    )
    if not rsp.success:
        return from_tcl_failure(rsp, client=client)

    rgs = client.exec_tcl(
        f"set_property GENERATE_SYNTH_CHECKPOINT "
        f"{'true' if generate_synth_checkpoint else 'false'} "
        f"[get_files [get_property IP_FILE [get_ips {name}]]]"
    )
    if not rgs.success:
        return from_tcl_failure(rgs, client=client)

    # generate_target so the .veo / .xdc are produced -- needed before
    # the parent synth references the IP.
    rgt = client.exec_tcl(
        f"generate_target all [get_files [get_property IP_FILE [get_ips {name}]]]"
    )
    if not rgt.success:
        return from_tcl_failure(rgt, client=client)

    xci = (query_one(
        client, f"get_property IP_FILE [get_ips {name}]"
    ) or "").strip()

    # Read back what the IP actually committed to, so the caller sees
    # the realised state rather than just the request. Past observations
    # have shown cases where a `set_property` looked like it succeeded
    # but the IP's CONFIG did not change; reading back is the only way
    # to detect those without a separate verify pass.
    if in_specs:
        activity_str = (query_one(
            client,
            f"get_property CONFIG.C_EN_PROBE_IN_ACTIVITY [get_ips {name}]",
        ) or "").strip()
        activity_value = activity_str == "1"
    else:
        activity_value = None

    return ok(
        f"created VIO {name} (out={len(out_specs)}, in={len(in_specs)})",
        client=client,
        ip=name,
        xci=xci,
        activity_detection=activity_value,
    )


# ---------------------------------------------------------------------------
# Build-time: ILA core insertion
# ---------------------------------------------------------------------------
#
# Hand-rolling create_debug_core + create_debug_port + connect_debug_port
# + dbg_hub clock fix + dedicated XDC is ~25 lines of Tcl with several
# footguns (see using_ila.md §10b). `create_ila_core` wraps the whole
# sequence:
#
#   - dedicated debug XDC, attached to constrs_1, so the user-authored
#     XDC (e.g. pynq_z1.xdc) stays clean
#   - target_constrs_file flipped to the dedicated XDC for the
#     duration of save_constraints, then restored
#   - dbg_hub C_CLK_INPUT_FREQ_HZ overridden from the 300 MHz default
#     to the design's actual clock frequency
#
# Pair with `delete_ila_core` for the symmetric DELETE step.

def create_ila_core(
    client,
    *,
    name: str = "u_ila_0",
    clock_net: str,
    probes: list[dict],
    depth: int = 4096,
    xdc_path: str | None = None,
    dbg_hub_clock_freq_hz: int = 125_000_000,
    dbg_hub_clock_net: str | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Insert an ILA core into the open synthesized design.

    Wraps the `create_debug_core` + per-probe `connect_debug_port` +
    dbg_hub clock-fix + dedicated XDC flow into a single call. This
    sequence is several lines of Tcl with multiple footguns (see
    using_ila.md §10b), and the helper exists so callers do not have
    to reimplement it.

    Args:
        name: instance name for the debug core. Default `u_ila_0`
            matches Vivado's default and the SKILL examples.
        clock_net: post-synthesis clock net to connect to `<name>/clk`.
            Typically `clk_IBUF` or `clk_IBUF_BUFG` (depends on the
            design). To discover the right name, point `get_pins`
            at any clocked instance in your design and ask what net
            drives its clk pin. A VIO instance is the most reliable
            target if your design has one:
                client.exec_tcl(
                    "get_nets -of_objects "
                    "[get_pins u_vio/inst/clk]"
                )
            If you don't have a VIO, any FF in your top module works
            too -- e.g. `[get_pins u_<inst>/<reg>/C]`.
        probes: list of probe descriptions, each a dict with:
            - "name"  (str, required): label that will appear in the
                       runtime probe list and CSV column headers.
            - "nets"  (str | list[str], required): one net name or a
                       list of bit-net names. Strings are split on
                       whitespace. Width is implied by `len(nets)`.
            - "width" (int, optional): if given, used as a sanity
                       check against `len(nets)`.
        depth: `C_DATA_DEPTH` -- capture buffer size. Must match what
            you'll later use for `CONTROL.DATA_DEPTH` at runtime.
        xdc_path: where to write the debug XDC. If None, defaults to
            `<project>/<top>.srcs/constrs_1/imports/debug_<name>.xdc`
            via Vivado's standard `save_constraints` placement.
            Pass an absolute path to control placement explicitly --
            the file is then created via `add_files` and registered
            as the active target_constrs_file before `save_constraints`.
        dbg_hub_clock_freq_hz: `dbg_hub` `C_CLK_INPUT_FREQ_HZ`. The
            Vivado default is 300 MHz, which is wrong on most boards.
            Default 125 MHz matches PYNQ-Z1; override for other boards.
        dbg_hub_clock_net: net to connect `dbg_hub/clk` to. None
            (default) reuses `clock_net`.
        overwrite: if True and a debug core named `name` already
            exists, delete it first via `delete_ila_core` before
            creating the new one. Default False (returns
            `error_kind="core_exists"`).

    Returns the ok/fail dict plus:
        core: instance name of the created core.
        depth: configured C_DATA_DEPTH.
        clock_net: net wired to <name>/clk (read back from get_debug_ports).
        probes: list of dicts, one per probe, with the actual port
                label, width, and net list as Vivado now records them
                via get_debug_ports. This is the read-back, not the
                request -- canonicalisation differences are visible.
        xdc_path: absolute path to the XDC file holding the debug
                  constraints (whether autogenerated or user-supplied).
        dbg_hub_clock_freq_hz: configured frequency.

    Failure modes:
        - `client_error`     -- bad `probes` spec (missing name/nets,
          width mismatch, no probes), or one of the requested nets was
          silently dropped by `get_nets` so the actual connected width
          is less than the requested width. The failure dict includes
          `requested_nets`, `connected_nets`, and `missing_nets` so
          the caller can fix the offender. This catches
          `[Chipscope 16-213]` impl-time failures at design time.
        - `core_exists`      -- a debug core with this name already
          exists and overwrite=False.
        - `not_open`         -- no synthesized design currently open.
          The caller should run `build.open_synth(client)` first.
        - `tcl_error`        -- propagated from any of the underlying
          create_debug_core / connect_debug_port / save_constraints
          calls. The `error_info` field carries Vivado's full message.

    The function does not run synth or impl; the caller does that
    afterwards (`build.implement(client)` is the usual next step).
    """
    # ----- A synth design must be open for create_debug_core -----
    # Checked BEFORE probe spec validation so the caller sees the
    # actionable "open the design first" failure even when their probe
    # spec is also wrong. Putting probe-spec checks first masks the
    # real precondition and pushes callers down a wrong-fix path.
    open_label = (query_one(client, "current_design -quiet") or "").strip()
    if not open_label:
        return fail(
            "not_open",
            "create_ila_core: no design is currently open. Run "
            "build.open_synth(client) first.",
        )

    # ----- Validate probes spec -----
    if not probes:
        return fail(
            "client_error",
            "create_ila_core: at least one probe is required.",
        )

    norm_probes: list[dict[str, Any]] = []
    for i, spec in enumerate(probes):
        if "name" not in spec:
            return fail(
                "client_error",
                f"probes[{i}] missing required 'name'.",
            )
        if "nets" not in spec:
            return fail(
                "client_error",
                f"probes[{i}] ({spec['name']!r}) missing required 'nets'.",
            )
        nets = spec["nets"]
        if isinstance(nets, str):
            net_list = nets.split()
        else:
            net_list = list(nets)
        if not net_list:
            return fail(
                "client_error",
                f"probes[{i}] ({spec['name']!r}) has empty 'nets'.",
            )
        width = len(net_list)
        if "width" in spec and int(spec["width"]) != width:
            return fail(
                "client_error",
                f"probes[{i}] ({spec['name']!r}): width={spec['width']} "
                f"does not match len(nets)={width}.",
            )
        norm_probes.append({"name": spec["name"], "nets": net_list,
                            "width": width})

    # ----- Existing core check -----
    existing = (query_one(client, f"get_debug_cores -quiet {name}") or "").strip()
    if existing:
        if not overwrite:
            return fail(
                "core_exists",
                f"A debug core named {name!r} already exists. Pass "
                f"overwrite=True to delete and recreate.",
                core=name,
            )
        rdel = delete_ila_core(client, name=name)
        if not rdel["success"]:
            rdel["message"] = (
                f"create_ila_core overwrite=True: pre-delete failed: "
                f"{rdel['message']}"
            )
            return rdel

    # ----- Switch target_constrs_file to a dedicated debug XDC -----
    # CRITICAL ordering: this happens BEFORE create_debug_core so that
    # ANY save_constraints triggered later -- whether by us on the
    # success path, by a fail-path cleanup, or by a subsequent
    # build.implement() that implicitly saves -- writes the dbg_hub /
    # debug-core stanza to our dedicated file rather than to the
    # user's authored XDC. Previously this switch happened only after
    # the per-probe verification, so a verification fail (or any other
    # mid-flow error) left target_constrs_file = user XDC with the
    # debug core already created in memory, and the next save (often
    # implicit) silently appended ~4 lines of dbg_hub config to the
    # user-maintained XDC.
    #
    # If xdc_path is None we synthesize a default location in the
    # project's constrs_1 directory; if given, we honour it verbatim.
    prior_target = (query_one(
        client, "get_property target_constrs_file [current_fileset -constrset]",
    ) or "").strip()

    if xdc_path is None:
        # Default: <project_dir>/<project_name>.srcs/constrs_1/
        #          imports/debug_<name>.xdc
        # We use Vivado's own DIRECTORY for the project so we land in
        # the canonical location rather than guessing.
        project_dir = (query_one(
            client, "get_property DIRECTORY [current_project]"
        ) or "").strip()
        project_name = (query_one(
            client, "get_property NAME [current_project]"
        ) or "").strip()
        if not project_dir or not project_name:
            return fail(
                "tcl_error",
                "create_ila_core: could not resolve project DIRECTORY/NAME "
                "to place the default debug XDC. Pass xdc_path explicitly.",
                core=name,
            )
        xdc_path = str(
            Path(project_dir) / f"{project_name}.srcs" / "constrs_1"
            / "imports" / f"debug_{name}.xdc"
        )

    # Create the file (empty) so add_files succeeds, then point
    # target_constrs_file at it. We deliberately do NOT pre-write
    # any content -- save_constraints will fill it.
    xdc_p = Path(xdc_path)
    try:
        xdc_p.parent.mkdir(parents=True, exist_ok=True)
        if not xdc_p.exists():
            xdc_p.write_text("", encoding="utf-8")
    except OSError as exc:
        return fail(
            "client_error",
            f"create_ila_core: cannot create XDC at {xdc_path!r}: {exc}",
            core=name,
        )
    # Vivado accepts forward slashes on Windows; normalise.
    xdc_tcl = tcl_str(xdc_p.resolve())
    radd = client.exec_tcl(
        f"add_files -quiet -fileset constrs_1 {{{xdc_tcl}}}"
    )
    if not radd.success:
        return from_tcl_failure(radd, client=client)
    rset_target = client.exec_tcl(
        f"set_property target_constrs_file {{{xdc_tcl}}} "
        f"[current_fileset -constrset]"
    )
    if not rset_target.success:
        return from_tcl_failure(rset_target, client=client)

    # Everything from here on can fail mid-flow and leave a partially
    # built debug core in the design. The try/finally guarantees:
    #   1. target_constrs_file is restored to `prior_target` on every
    #      return path so the user's authored XDC never absorbs debug
    #      content via a later implicit save_constraints.
    #   2. If create_debug_core succeeded but a later step failed (so
    #      the function is returning a fail dict), the half-built debug
    #      core is removed from the Tcl interpreter. Otherwise it would
    #      sit around in memory and the next save_constraints by the
    #      caller -- with target_constrs_file already restored to the
    #      user XDC by step 1 -- would flush dbg_hub stanzas straight
    #      into the user's authored XDC. (Observed: ~1 KB written into
    #      a previously-zero-byte user XDC after a probe-net validation
    #      failure.) `dbg_hub` itself is deliberately NOT touched: it's
    #      shared infrastructure and other ILA/VIO cores may depend on
    #      it.
    target_was_switched = True
    created_core = False
    finished_ok = False
    try:
        # ----- create_debug_core + per-probe ports -----
        rcr = client.exec_tcl(
            f"create_debug_core {name} ila; "
            f"set_property C_DATA_DEPTH {int(depth)} [get_debug_cores {name}]; "
            f"set_property C_TRIGIN_EN  false [get_debug_cores {name}]; "
            f"set_property C_TRIGOUT_EN false [get_debug_cores {name}]"
        )
        if not rcr.success:
            return from_tcl_failure(rcr, client=client)
        # From this point on, the core lives in the Tcl interpreter.
        # If we return a fail dict below, the finally block must wipe
        # it; if we save_constraints later, the dbg_hub stanza for it
        # will be written somewhere -- and we want that "somewhere" to
        # be our dedicated debug XDC, never the user's.
        created_core = True

        # Clock connection. probe ports are named probe0/probe1/... ;
        # probe0 is auto-created by `create_debug_core ... ila`, probe1+
        # need explicit `create_debug_port`.
        rcl = client.exec_tcl(
            f"set_property port_width 1 [get_debug_ports {name}/clk]; "
            f"connect_debug_port {name}/clk [get_nets {clock_net}]"
        )
        if not rcl.success:
            return from_tcl_failure(rcl, client=client)

        for i, p in enumerate(norm_probes):
            port = f"{name}/probe{i}"
            # Pre-flight: verify each requested net resolves on the
            # synthesized design. `get_nets -quiet <name>` returns the
            # net's canonical name when it exists and an empty string
            # otherwise -- a clean typo / non-existent-post-optimization
            # check that does not depend on the debug graph.
            #
            # We do not verify the *result* of connect_debug_port (the
            # earlier approach was to count `get_nets -of_objects
            # [get_debug_ports port]`, but on Vivado 2024.1 that query
            # returns 0 even after a successful bind that survives all
            # the way through impl -- so the check produced false
            # negatives and rejected designs that build cleanly). True
            # silent-drop binds (e.g. a net without `mark_debug`) are
            # caught by the impl-time DRC `[Chipscope 16-213] probeN
            # has K unconnected channels` error a minute or two later;
            # we cannot do better up-front without a reliable Vivado
            # API for "did this connect_debug_port actually take?".
            missing: list[str] = []
            for net in p["nets"]:
                resolved = (query_one(
                    client,
                    f"get_nets -quiet {{{net}}}",
                ) or "").strip()
                if not resolved:
                    missing.append(net)
            if missing:
                return fail(
                    "net_not_found",
                    f"create_ila_core: probe[{i}] ({p['name']!r}) "
                    f"references {len(missing)} net(s) that don't "
                    f"exist post-synth: {missing}. Common causes: "
                    f"typo in the HDL wire name; the net was "
                    f"optimized away (add `(* keep = \"true\" *)` "
                    f"or `(* mark_debug = \"true\" *)` in HDL); "
                    f"hierarchical name without `KEEP_HIERARCHY` on "
                    f"the wrapping module. Confirm with "
                    f"`get_nets <pattern>` against the synthesized "
                    f"design.",
                    core=name,
                    probe=p["name"],
                    missing_nets=missing,
                    requested_nets=list(p["nets"]),
                    client=client,
                )

            nets_tcl = " ".join(p["nets"])
            if i == 0:
                tcl = (
                    f"set_property port_width {p['width']} [get_debug_ports {port}]; "
                    f"connect_debug_port {port} [get_nets [list {nets_tcl}]]"
                )
            else:
                tcl = (
                    f"create_debug_port {name} probe; "
                    f"set_property port_width {p['width']} [get_debug_ports {port}]; "
                    f"connect_debug_port {port} [get_nets [list {nets_tcl}]]"
                )
            rp = client.exec_tcl(tcl)
            if not rp.success:
                return from_tcl_failure(rp, client=client)

        # dbg_hub clock fix. The hub is added on demand by Vivado when
        # a debug core is first created, so by this point it already
        # exists and we just override the offending defaults.
        hub_clock = dbg_hub_clock_net or clock_net
        rh = client.exec_tcl(
            f"set_property C_CLK_INPUT_FREQ_HZ {int(dbg_hub_clock_freq_hz)} "
            f"[get_debug_cores dbg_hub]; "
            f"set_property C_USER_SCAN_CHAIN 1 [get_debug_cores dbg_hub]; "
            f"connect_debug_port dbg_hub/clk [get_nets {hub_clock}]"
        )
        if not rh.success:
            return from_tcl_failure(rh, client=client)

        # ----- Read back what was actually configured -----
        # Done BEFORE save_constraints because Vivado 2021.1's
        # save_constraints flow can leave the synthesized design closed
        # under the hood, which makes `get_nets -of_objects [get_debug_ports
        # ...]` return empty afterwards. By reading here, while the design
        # is still definitely open, the connection facts come back
        # correctly on both 2021.1 and 2024.1. The properties on the
        # debug-core / debug-port objects themselves (C_DATA_DEPTH,
        # port_width) are stable across the save, but the net resolution
        # is not.
        actual_clock = (query_one(
            client,
            f"get_nets -quiet -of_objects [get_debug_ports {name}/clk]",
        ) or "").strip()

        actual_depth_s = (query_one(
            client,
            f"get_property C_DATA_DEPTH [get_debug_cores {name}]",
        ) or "").strip()
        try:
            actual_depth = int(actual_depth_s)
        except ValueError:
            actual_depth = -1

        probe_readback: list[dict[str, Any]] = []
        for i, p in enumerate(norm_probes):
            port_label = f"probe{i}"
            port_obj = f"{name}/{port_label}"
            port_width_s = (query_one(
                client,
                f"get_property port_width [get_debug_ports {port_obj}]",
            ) or "").strip()
            try:
                port_width = int(port_width_s)
            except ValueError:
                port_width = -1
            connected = (query_one(
                client,
                f"get_nets -quiet -of_objects [get_debug_ports {port_obj}]",
            ) or "").split()
            probe_readback.append({
                "name": p["name"],
                "port": port_label,
                "width": port_width,
                "nets": connected,
            })

        actual_hub_freq_s = (query_one(
            client,
            "get_property C_CLK_INPUT_FREQ_HZ [get_debug_cores dbg_hub]",
        ) or "").strip()
        try:
            actual_hub_freq = int(actual_hub_freq_s)
        except ValueError:
            actual_hub_freq = -1

        # ----- save_constraints into the dedicated XDC -----
        # target_constrs_file is already pointing at the dedicated
        # debug XDC (set up before this try block), so save here
        # writes the dbg_hub / debug-core stanza only into that file
        # and never touches the user's authored XDC.
        rsave = client.exec_tcl("save_constraints -force")
        if not rsave.success:
            return from_tcl_failure(rsave, client=client)

        # Discover where the debug XDC ended up. Vivado's autogenerated
        # path lives under .srcs/constrs_1/imports/ when no xdc_path was
        # provided; explicit xdc_path is what we set above.
        actual_xdc = (query_one(
            client,
            "get_property target_constrs_file [current_fileset -constrset]",
        ) or "").strip()

        # Success: the finally below restores prior_target so the
        # project's authored XDC remains the user-facing target. Mark
        # finished_ok so the finally's debug-core-cleanup branch is
        # skipped -- we very much want to keep the core we just built.
        result = ok(
            f"created ILA {name} ({len(probe_readback)} probe(s), "
            f"depth={actual_depth})",
            client=client,
            core=name,
            depth=actual_depth,
            clock_net=actual_clock,
            probes=probe_readback,
            xdc_path=actual_xdc,
            dbg_hub_clock_freq_hz=actual_hub_freq,
        )
        finished_ok = True
        return result
    finally:
        # 1. Restore target_constrs_file -- on success so the user XDC
        #    remains the user-facing target; on every fail return so
        #    subsequent implicit save_constraints (e.g. from
        #    build.implement) don't dump dbg_hub config into the user
        #    XDC. Restore errors are swallowed because the primary
        #    return dict is more actionable than a cleanup-of-cleanup
        #    failure.
        if target_was_switched and prior_target:
            current_target = (query_one(
                client,
                "get_property target_constrs_file [current_fileset -constrset]",
            ) or "").strip()
            if current_target != prior_target:
                client.exec_tcl(
                    f"set_property target_constrs_file {{{prior_target}}} "
                    f"[current_fileset -constrset]"
                )
        # 2. If we created a debug core but didn't reach the success
        #    return, delete it from the Tcl interpreter. Otherwise the
        #    next save_constraints anywhere in the session would flush
        #    its dbg_hub stanza into whatever target_constrs_file is
        #    currently set to -- which, post step-1 restore, is the
        #    user's authored XDC. Best-effort: any failure to delete is
        #    swallowed so the caller still sees the original failure
        #    reason. dbg_hub is intentionally left alone (shared with
        #    other debug cores).
        if created_core and not finished_ok:
            try:
                client.exec_tcl(
                    f"set _d [get_debug_cores -quiet {name}]; "
                    f"if {{[llength $_d] > 0}} {{ "
                    f"delete_debug_core $_d "
                    f"}}"
                )
            except Exception:
                pass


def delete_ila_core(
    client, *, name: str = "u_ila_0",
) -> dict[str, Any]:
    """Remove an ILA core from the design.

    Mirrors `create_ila_core`: deletes the core, drops the dedicated
    XDC from constrs_1 if one was created (detected via the
    target_constrs_file Vivado has currently set on it), and saves
    the resulting constraint set.

    Args:
        name: debug core instance name. Default `u_ila_0` matches
            `create_ila_core`'s default.

    Returns ok/fail dict plus:
        core: name of the core that was deleted.
        removed_xdc: path of the XDC file removed from constrs_1
                     (None if none was specifically removed -- e.g.
                     debug constraints lived in a hand-authored XDC
                     and were stripped from there by save_constraints).
        residual_dbg_hub: True if the dbg_hub debug core is still
                          present after delete (Vivado removes it
                          automatically only when no debug cores
                          remain). Surfaced so the caller can decide
                          whether to manually `delete_debug_core
                          dbg_hub` or leave it for the next ADD.

    Failure modes:
        - `not_found`  -- no debug core with that name exists.
        - `not_open`   -- no design currently open.
        - `tcl_error`  -- propagated from delete_debug_core /
          remove_files / save_constraints.
    """
    open_label = (query_one(client, "current_design -quiet") or "").strip()
    if not open_label:
        return fail(
            "not_open",
            "delete_ila_core: no design is currently open. Run "
            "build.open_synth(client) first.",
        )

    existing = (query_one(client, f"get_debug_cores -quiet {name}") or "").strip()
    if not existing:
        return fail(
            "not_found",
            f"No debug core named {name!r} on the open design. "
            f"Use `client.exec_tcl('get_debug_cores')` to list.",
            core=name,
        )

    # If the project has a dedicated debug XDC -- any file matching
    # the debug_<name>.xdc pattern create_ila_core writes to -- drop
    # it from the fileset BEFORE delete_debug_core so save_constraints
    # later doesn't try to rewrite the file we no longer want.
    candidate = (query_one(
        client,
        f"get_files -quiet -filter "
        f"{{NAME =~ */debug_{name}.xdc}}",
    ) or "").strip()
    removed_xdc: str | None = None
    if candidate:
        rrm = client.exec_tcl(
            f"remove_files [get_files {{{candidate}}}]"
        )
        if not rrm.success:
            return from_tcl_failure(rrm, client=client)
        removed_xdc = candidate

    rdel = client.exec_tcl(f"delete_debug_core [get_debug_cores {name}]")
    if not rdel.success:
        return from_tcl_failure(rdel, client=client)

    rsave = client.exec_tcl("save_constraints -force")
    if not rsave.success:
        return from_tcl_failure(rsave, client=client)

    # Did Vivado leave dbg_hub behind? It survives until the last
    # debug core is gone. If others still exist, dbg_hub is
    # legitimate; if not, it's residue worth flagging.
    other_cores = (query_one(client, "get_debug_cores -quiet") or "").split()
    has_dbg_hub = "dbg_hub" in other_cores
    real_remaining = [c for c in other_cores if c != "dbg_hub"]
    residual = has_dbg_hub and not real_remaining

    return ok(
        f"deleted ILA {name}"
        + (f" + removed XDC {removed_xdc}" if removed_xdc else "")
        + (" (dbg_hub residual)" if residual else ""),
        client=client,
        core=name,
        removed_xdc=removed_xdc,
        residual_dbg_hub=residual,
    )


# ---------------------------------------------------------------------------
# ILA -- runtime ops moved to operations.ila
# ---------------------------------------------------------------------------
# Use `from operations import ila`. This module deliberately does not
# re-export `list_ilas` etc. so there is exactly one canonical entry
# point for each ILA verb.


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _probe_width_radix(
    client, vio: str, probe: str, *, value_prop: str
) -> tuple[int | None, str | None]:
    """Look up a probe's WIDTH and value-encoding radix.

    `value_prop` is `"OUTPUT_VALUE_RADIX"` for output probes (drives
    write_vio_probe) or `"INPUT_VALUE_RADIX"` for inputs (drives
    read_vio_probe int decoding). Returns `(None, None)` on lookup
    failure -- callers translate that into an explicit error.
    """
    width_s = query_one(
        client,
        f"get_property WIDTH [get_hw_probes {probe} "
        f"-of_objects [get_hw_vios {vio}]]",
    )
    radix = query_one(
        client,
        f"get_property {value_prop} [get_hw_probes {probe} "
        f"-of_objects [get_hw_vios {vio}]]",
    )
    if width_s is None or radix is None:
        return None, None
    if not width_s.isdigit():
        return None, None
    return int(width_s), radix.upper()


def _encode_vio_value(
    client, vio: str, probe: str, value: int,
) -> str | dict[str, Any]:
    """Encode an int as a Vivado OUTPUT_VALUE literal for `probe`.

    Returns the literal string on success, or an error dict.

    Negative ints are interpreted as two's-complement within the
    probe's width. Values that don't fit (after that interpretation)
    are rejected with `invalid_value` rather than silently truncated --
    a wraparound write to a 16-bit DAC, for example, is almost always
    a caller bug.
    """
    width, radix = _probe_width_radix(
        client, vio, probe, value_prop="OUTPUT_VALUE_RADIX",
    )
    if width is None or radix is None:
        return fail(
            "invalid_value",
            f"Could not read WIDTH/OUTPUT_VALUE_RADIX for {vio}/{probe} "
            f"to encode int value {value!r}. Pass an already-formatted "
            f"string instead, or check that the .ltx is attached.",
            vio=vio,
            probe=probe,
        )

    mask = (1 << width) - 1
    if value < 0:
        if value < -(1 << (width - 1)):
            return fail(
                "invalid_value",
                f"Value {value} does not fit in {width}-bit signed range "
                f"(min -{1 << (width - 1)}). Refusing to wrap.",
                vio=vio, probe=probe, width=width, radix=radix,
            )
        value = value & mask
    else:
        if value > mask:
            return fail(
                "invalid_value",
                f"Value {value} exceeds {width}-bit unsigned max ({mask}). "
                f"Refusing to truncate.",
                vio=vio, probe=probe, width=width, radix=radix,
            )

    if radix == "HEX":
        digits = (width + 3) // 4
        return format(value, f"0{digits}x")
    if radix == "BINARY":
        return format(value, f"0{width}b")
    if radix in ("UNSIGNED", "SIGNED"):
        # Vivado accepts a plain decimal literal for these; for SIGNED
        # we already converted negatives via two's-complement above,
        # so unsigned-form `value` round-trips correctly.
        return str(value)
    return fail(
        "invalid_value",
        f"Unsupported OUTPUT_VALUE_RADIX {radix!r} on {vio}/{probe}. "
        f"Pass an already-formatted string literal as `value=` instead.",
        vio=vio, probe=probe, width=width, radix=radix,
    )


def _decode_vio_value(raw: str, width: int, radix: str) -> int | None:
    """Decode a raw INPUT_VALUE string using its width + radix.

    Vivado typically returns hex characters for HEX/UNSIGNED/SIGNED
    probes and a 0/1 string for BINARY probes. For SIGNED we apply
    two's-complement based on the probe width. Returns None when the
    raw form doesn't parse -- callers should leave the unparsed string
    in the result so the user can still inspect it.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        if radix == "BINARY":
            n = int(raw, 2)
        else:
            # Vivado uses hex for HEX/UNSIGNED/SIGNED values regardless
            # of decimal-looking radix names.
            n = int(raw, 16)
    except ValueError:
        return None
    if radix == "SIGNED" and width > 0 and n & (1 << (width - 1)):
        n -= 1 << width
    return n


def _probe_direction(client, vio: str, probe: str) -> str | None:
    """Return "in"/"out" for a probe, or None if the probe doesn't exist.

    Used by read_vio_probe / write_vio_probe to give a friendly
    "wrong_direction" error before Vivado raises a less helpful
    INPUT_VALUE / OUTPUT_VALUE property error.
    """
    ptype = query_one(
        client,
        f"get_property TYPE [get_hw_probes {probe} "
        f"-of_objects [get_hw_vios {vio}]]",
    )
    if not ptype:
        return None
    if "input" in ptype or ptype == "probe_in":
        return "in"
    if "output" in ptype or ptype == "probe_out":
        return "out"
    return ptype  # surface unknown raw value to caller


def _resolve_single_vio(
    client, vio: str | None, **identity: Any
) -> str | dict[str, Any]:
    """Return a VIO name string, or an error dict.

    If `vio` is given, validate it. If None, pick the only VIO; error
    out if there are zero or more than one.

    `identity` kwargs (e.g. `probe="..."`) are forwarded into every
    failure dict this helper might emit, so callers don't lose the
    "what was I trying to read/write" location info on the early-out
    paths. The "no VIOs at all" case also gets `vios=()` so the failure
    dict shape is uniform across all not-found / ambiguous branches.
    """
    vios = (query_one(client, "get_hw_vios") or "").split()
    if not vios:
        return fail(
            "not_found",
            "No VIO cores on the device. Did programming complete and "
            "did the design include a VIO IP?",
            vios=(),
            **identity,
        )
    if vio is None:
        if len(vios) > 1:
            return fail(
                "ambiguous",
                f"Multiple VIO cores present ({vios}); pass vio=<name>.",
                vios=vios,
                **identity,
            )
        return vios[0]
    if vio not in vios:
        return fail(
            "not_found",
            f"VIO {vio!r} not found. Available: {vios}",
            vios=vios,
            vio=vio,
            **identity,
        )
    return vio
