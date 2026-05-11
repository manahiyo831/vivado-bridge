"""Hardware Manager operations: open target, program device, status.

Wraps the typical "connect to JTAG, find target, program FPGA" flow,
with auto-detection of `.bit` and `.ltx` files from the project's impl
directory so that VIO/ILA cores light up automatically after programming.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import build as build_ops
from ._common import fail, from_tcl_failure, ok, query_one, tcl_str


# ---------------------------------------------------------------------------
# session lifecycle
# ---------------------------------------------------------------------------

def open_hw_manager(client) -> dict[str, Any]:
    """Switch Vivado into Hardware Manager mode.

    Vivado's `open_hw_manager` is idempotent (calling it when already
    open is a no-op), so we just forward and report.
    """
    r = client.exec_tcl("open_hw_manager")
    if not r.success:
        return from_tcl_failure(r, client=client)
    return ok("hw_manager open")


def connect_hw_server(client) -> dict[str, Any]:
    """Connect to the local Xilinx hw_server (port 3121).

    `connect_hw_server` itself is NOT idempotent in Vivado -- calling
    it twice produces "Disconnect server connection ... before making a
    new one". We work around that by skipping the call when a server
    connection already exists, returning `changed=False` so the caller
    can tell the difference between "freshly connected" and "already
    connected".
    """
    existing = (query_one(client, "get_hw_servers -quiet") or "").strip()
    if existing:
        return ok(
            f"already connected ({existing})",
            server=existing,
            changed=False,
        )
    r = client.exec_tcl("connect_hw_server", timeout=30)
    if not r.success:
        return from_tcl_failure(r, client=client)
    server = (query_one(client, "get_hw_servers") or "").strip()
    return ok(f"connected ({server})", server=server, changed=True)


def open_hardware_target(
    client,
    *,
    target_filter: str | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Open a JTAG target. Does NOT auto-open the Hardware Manager or
    connect to the hw_server -- call `open_hw_manager()` and
    `connect_hw_server()` first.

    Args:
        target_filter: optional substring; if multiple targets are
            connected, only the first whose name contains this substring
            is opened. With one cable this can be left None.
        force_refresh: if True and the target is already open, close
            and reopen it so Vivado re-scans the JTAG chain. Useful
            after the user power-cycled the board, or when an earlier
            session left a stale "target open / chain empty" state
            that survives a fresh `open_hw_target`. Defaults to False
            to keep the cheap idempotent fast-path; set explicitly
            when you suspect a cached chain.

    Returns the chosen target's name plus `changed` indicating whether
    anything was actually opened (False when the target was already
    open and force_refresh was not requested).

    Failure modes:
        not_found       -- no targets visible, or no target matched the filter
        ambiguous       -- multiple targets and no filter given
        tcl_error       -- Vivado refused to open (cable busy, etc.)
    """
    targets_raw = query_one(client, "get_hw_targets") or ""
    if not targets_raw:
        return fail(
            "not_found",
            "No JTAG hardware targets detected. Is the cable plugged in "
            "and the board powered? Run connect_hw_server() first if you "
            "haven't yet.",
        )
    targets = targets_raw.split()

    if target_filter:
        matched = [t for t in targets if target_filter in t]
        if not matched:
            return fail(
                "not_found",
                f"No target matched filter {target_filter!r}. "
                f"Available: {targets}",
                targets=targets,
            )
        target = matched[0]
    elif len(targets) > 1:
        return fail(
            "ambiguous",
            f"{len(targets)} targets are connected; pass target_filter=... "
            f"to choose one. Available: {targets}",
            targets=targets,
        )
    else:
        target = targets[0]

    # Vivado's `open_hw_target` fails if that target is already open, so
    # detect that case and skip the call. Reporting `changed=False` lets
    # callers distinguish between "we just opened it" and "it was open
    # already". `force_refresh=True` overrides this to force a re-scan
    # of the JTAG chain, which is the only known way to clear a cached
    # "target open but chain empty" state from an earlier session.
    current = query_one(client, "current_hw_target -quiet") or ""
    if current == target and not force_refresh:
        # Fast-path. Validate that the cached chain isn't empty: when an
        # earlier session left a stale target with no devices, the fast
        # path silently keeps that broken state and the next
        # program_device blows up with "No hw_device is open". Surface
        # that as a warning so the caller knows to retry with
        # force_refresh=True. We deliberately do NOT auto-select a
        # device when multiple are present: silent picking-the-first is
        # exactly the kind of fallback that hides "wrong board chosen"
        # bugs. open_hardware_device() is the explicit path.
        cached_devs = (query_one(client, "get_hw_devices") or "").split()
        warnings: list[str] = []
        if not cached_devs:
            warnings.append(
                f"target {target} is open but its cached JTAG chain is "
                f"empty. This usually means a previous session left a "
                f"stale state. Call "
                f"open_hardware_target(force_refresh=True) to re-scan "
                f"before attempting program_device()."
            )
        return ok(
            f"target already open ({target})",
            target=target,
            targets_available=targets,
            changed=False,
            devices=cached_devs,
            warnings=warnings,
        )

    if current == target and force_refresh:
        r = client.exec_tcl("close_hw_target", timeout=30)
        if not r.success:
            return from_tcl_failure(r, client=client)

    r = client.exec_tcl(f"open_hw_target {tcl_str(target)}", timeout=60)
    if not r.success:
        return from_tcl_failure(r, client=client)
    devices = (query_one(client, "get_hw_devices") or "").split()
    return ok(
        f"opened {target}" + (" (refreshed)" if force_refresh else "")
        + f"; {len(devices)} device(s) on chain",
        target=target,
        targets_available=targets,
        changed=True,
        devices=devices,
    )


def list_hw_devices(client) -> dict[str, Any]:
    """Return the hw_devices visible under the currently open hw_target.

    A typical Xilinx JTAG chain shows multiple devices (e.g. on Zynq the
    chain contains both `arm_dap_0` and the FPGA `xc7z020_1`). The op
    just reports what Vivado sees -- picking which one is "the right
    device" is the caller's job, via `open_hardware_device`.

    An empty `devices` list is reported with `success=True` (the query
    itself succeeded; an empty chain is a valid answer). Most often
    this state is a stale Vivado cache from an earlier session, not a
    real hardware problem -- the message points at
    `open_hardware_target(force_refresh=True)` as the first thing to
    try.

    Failure modes:
        not_found       -- no hw_target is open
    """
    if not query_one(client, "current_hw_target -quiet"):
        return fail(
            "not_found",
            "No hw_target is open. Call open_hardware_target() first.",
        )
    devices = (query_one(client, "get_hw_devices") or "").split()
    if not devices:
        return ok(
            "0 device(s): <none>. Vivado may be holding a stale chain "
            "from a previous session; try open_hardware_target("
            "force_refresh=True) before suspecting cable / power.",
            devices=[],
        )
    return ok(
        f"{len(devices)} device(s): {' '.join(devices)}",
        devices=devices,
    )


def open_hardware_device(
    client,
    *,
    device_filter: str | None = None,
) -> dict[str, Any]:
    """Make a specific hw_device the `current_hw_device` for subsequent
    program/refresh operations. Wraps `current_hw_device [get_hw_devices ...]`.

    Args:
        device_filter: optional substring; selects the first hw_device
            whose name contains this substring. With one device on the
            chain this can be left None.

    Failure modes:
        not_found       -- no hw_target open, no devices on the chain,
                            or no device matched the filter
        ambiguous       -- multiple devices and no filter given
        tcl_error       -- Vivado refused the selection
    """
    if not query_one(client, "current_hw_target -quiet"):
        return fail(
            "not_found",
            "No hw_target is open. Call open_hardware_target() first.",
        )

    devices = (query_one(client, "get_hw_devices") or "").split()
    if not devices:
        return fail(
            "not_found",
            "The open hw_target has no hw_devices. This is most often a "
            "stale Vivado-side state from an earlier session, not a real "
            "hardware problem -- try open_hardware_target(force_refresh="
            "True) to re-scan the JTAG chain. If devices still don't "
            "appear after that, then suspect cable / power / mode-pin "
            "issues on the board.",
        )

    if device_filter:
        matched = [d for d in devices if device_filter in d]
        if not matched:
            return fail(
                "not_found",
                f"No hw_device matched filter {device_filter!r}. "
                f"Available: {devices}",
                devices=devices,
            )
        chosen = matched[0]
    elif len(devices) > 1:
        return fail(
            "ambiguous",
            f"{len(devices)} devices on this target; pass device_filter=... "
            f"to choose one. Available: {devices}",
            devices=devices,
        )
    else:
        chosen = devices[0]

    r = client.exec_tcl(f"current_hw_device [get_hw_devices {tcl_str(chosen)}]")
    if not r.success:
        return from_tcl_failure(r, client=client)

    actual = query_one(client, "current_hw_device") or ""
    return ok(
        f"selected {actual}",
        device=actual,
        devices_available=devices,
    )


def close_hardware_target(client) -> dict[str, Any]:
    """Close the currently open hw_target. No-op if nothing is open."""
    if not query_one(client, "current_hw_target -quiet"):
        return ok("no hw_target open", changed=False)
    r = client.exec_tcl("close_hw_target")
    if not r.success:
        return from_tcl_failure(r, client=client)
    return ok("closed hw_target", changed=True)


def get_hardware_status(client) -> dict[str, Any]:
    """Snapshot of the Hardware Manager: server, target, device, debug cores.

    Useful as a probe before doing anything destructive. Returns:
        connected           -- bool, hw_server connected
        target              -- current_hw_target string or None
        device              -- current_hw_device string or None
        part                -- device PART or None
        is_programmed       -- bool, True iff the FPGA's DONE pin is high
        program_file        -- last bit set with set_property PROGRAM.FILE,
                                or None
        vios                -- list of hw_vio names
        ilas                -- list of hw_ila names
    """
    server = query_one(client, "get_hw_servers -quiet") or ""
    target = query_one(client, "current_hw_target -quiet") or ""
    device = query_one(client, "current_hw_device -quiet") or ""
    part: str | None = None
    is_programmed: bool | None = None
    program_file: str | None = None

    if device:
        part = query_one(client, "get_property PART [current_hw_device]")
        # DONE pin in CONFIG_STATUS reflects "bitstream loaded" for 7-series
        # devices. The query may fail on parts that don't expose this
        # register (e.g. some ARM DAPs), so treat None as "unknown".
        done = query_one(
            client,
            "get_property REGISTER.CONFIG_STATUS.BIT14_DONE_PIN [current_hw_device]",
        )
        if done == "1":
            is_programmed = True
        elif done == "0":
            is_programmed = False
        # else leave None (unknown)
        program_file = query_one(
            client, "get_property PROGRAM.FILE [current_hw_device]"
        ) or None

    vios = (query_one(client, "get_hw_vios") or "").split()
    ilas = (query_one(client, "get_hw_ilas") or "").split()
    return ok(
        f"target={target or 'none'} device={device or 'none'} "
        f"programmed={is_programmed} vios={len(vios)} ilas={len(ilas)}",
        connected=bool(server),
        server=server,
        target=target or None,
        device=device or None,
        part=part,
        is_programmed=is_programmed,
        program_file=program_file,
        vios=vios,
        ilas=ilas,
    )


# ---------------------------------------------------------------------------
# programming
# ---------------------------------------------------------------------------

def program_device(
    client,
    *,
    bit_path: str | None = None,
    ltx_path: str | None = None,
    refresh: bool = True,
    auto_attach_probes: bool = True,
) -> dict[str, Any]:
    """Program the open FPGA with the project's bitstream.

    Both `bit_path` and `ltx_path` are deliberately separate parameters
    so callers (humans and AI) think about probes explicitly. If either
    is None and the project has an impl_1 run with output products, we
    auto-detect: <impl_dir>/*.bit and <impl_dir>/*.ltx.

    Behaviour matrix:
        - design has VIO/ILA + ltx given/found  -> program with probes
        - design has VIO/ILA + no ltx           -> program, return WARNING
        - design has no VIO/ILA                  -> program; ltx ignored
        - no .bit at all                         -> failure

    Args:
        bit_path: explicit path to a .bit. None = auto-detect.
        ltx_path: explicit path to a .ltx. None = auto-detect.
        refresh: refresh_hw_device after programming so probes register.
        auto_attach_probes: if False, skip setting PROBES.FILE even when
            an ltx is available (rarely useful, but lets you reproduce
            the GUI's "Program without probes" flow).

    Returns:
        success, bit_path, ltx_path, vio_count, ila_count, warnings.
    """
    # Need an open device before we can program anything.
    device = query_one(client, "current_hw_device -quiet")
    if not device:
        return fail(
            "not_found",
            "No hw_device is open. Call open_hardware_target() first.",
        )

    # Auto-detect missing paths from impl_1.
    auto_info = build_ops.find_bitstream(client) if (bit_path is None or ltx_path is None) else None
    if auto_info is not None and not auto_info["success"]:
        # impl_1 not found; only fatal if bit_path is missing too.
        if bit_path is None:
            return fail(
                "not_found",
                "Cannot auto-detect bitstream (impl_1 not available); "
                "pass bit_path explicitly.",
            )
    if bit_path is None and auto_info is not None:
        bit_path = auto_info.get("bit_path")
    if ltx_path is None and auto_info is not None:
        ltx_path = auto_info.get("ltx_path")

    if not bit_path or not Path(bit_path).exists():
        return fail(
            "not_found",
            f"Bitstream not found: {bit_path!r}. Run implement() first.",
            bit_path=bit_path,
        )

    # Inspect the design for debug cores so we can emit a useful warning.
    # cells of the form */*_VIO/* or *_ILA/* exist after synthesis; the
    # easier check is to count what get_hw_vios will show after refresh,
    # but that requires programming first. So we look at the .ltx file:
    # if it exists, debug cores were generated.
    has_debug_cores = bool(ltx_path) and Path(ltx_path).exists()

    warnings: list[str] = []

    # Attach probes file BEFORE program so the post-program refresh has it.
    if has_debug_cores and auto_attach_probes:
        r = client.exec_tcl(
            f"set_property PROBES.FILE {tcl_str(ltx_path)} [current_hw_device]"
        )
        if not r.success:
            return from_tcl_failure(r, client=client)
        # FULL_PROBES.FILE controls what the Hardware Manager GUI
        # shows in its dashboards. The bridge does not need this for
        # programming or for VIO/ILA Tcl access -- those work off
        # PROBES.FILE alone -- so a failure here is non-fatal. We do
        # NOT silently swallow it: the failure goes into `warnings`
        # so a user wondering why dashboards are empty can see it.
        rfp = client.exec_tcl(
            f"set_property FULL_PROBES.FILE {tcl_str(ltx_path)} [current_hw_device]"
        )
        if not rfp.success:
            warnings.append(
                f"FULL_PROBES.FILE attach failed (GUI dashboards may "
                f"not show probes): {rfp.format_error()}"
            )

    # Set bitstream and program.
    r = client.exec_tcl(
        f"set_property PROGRAM.FILE {tcl_str(bit_path)} [current_hw_device]"
    )
    if not r.success:
        return from_tcl_failure(r, client=client)

    r = client.exec_tcl("program_hw_devices [current_hw_device]", timeout=180)
    if not r.success:
        return from_tcl_failure(r, client=client)

    if refresh:
        rr = client.exec_tcl(
            "refresh_hw_device [current_hw_device]", timeout=60
        )
        if not rr.success:
            # Programming itself succeeded -- refusing the whole
            # operation now would be misleading. Surface the refresh
            # failure as a warning so the caller knows the post-program
            # debug-core counts below may be stale.
            warnings.append(
                f"refresh_hw_device after programming failed; "
                f"VIO/ILA counts below may be stale: {rr.format_error()}"
            )

    # Post-program inspection: count debug cores actually live.
    vios = (query_one(client, "get_hw_vios") or "").split()
    ilas = (query_one(client, "get_hw_ilas") or "").split()

    if (vios or ilas) and not has_debug_cores:
        warnings.append(
            f"Design has {len(vios)} VIO and {len(ilas)} ILA core(s) but "
            f"no .ltx (probes file) was attached. The Hardware Manager "
            f"will show the cores but won't know probe names. "
            f"Run again with ltx_path=... or rebuild so impl_1 emits a .ltx."
        )

    return ok(
        f"programmed {Path(bit_path).name}, vios={len(vios)}, ilas={len(ilas)}",
        client=client,
        bit_path=bit_path,
        ltx_path=ltx_path if has_debug_cores else None,
        vio_count=len(vios),
        ila_count=len(ilas),
        warnings=warnings,
    )
