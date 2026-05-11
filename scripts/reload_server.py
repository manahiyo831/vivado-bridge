#!/usr/bin/env python3
"""Reload the Tcl bridge inside the running Vivado.

Sends `::vbridge::stop_server` followed by `source vivado_socket_server.tcl`
to the running bridge, then verifies the new server is listening. Vivado
itself stays open. Use this when you've edited vivado_socket_server.tcl
and want the new version active without switching to the Tcl Console.

Usage:
    python scripts/reload_server.py [--server-tcl PATH] [--wait SEC] [--no-verify]

Exit codes:
    0  reload succeeded and the new server answered ping
    1  reload was sent but the new server didn't come back in time
    2  client/config error (can't reach existing server)
"""

import argparse
import sys
from pathlib import Path

from vivado_bridge_client import Client, ConfigError


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--server-tcl",
        type=Path,
        default=None,
        help="Path to vivado_socket_server.tcl to source "
             "(default: the file next to this script's parent).",
    )
    p.add_argument(
        "--wait",
        type=float,
        default=2.0,
        help="Seconds to wait for the new server to come up (default: 2.0).",
    )
    p.add_argument(
        "--no-verify",
        action="store_true",
        help="Don't ping the new server after reload; return as soon as the "
             "reload request is acknowledged.",
    )
    args = p.parse_args()

    try:
        client = Client()
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"Reloading bridge on {client.host}:{client.port} ...")
    resp = client.reload(
        server_tcl=args.server_tcl,
        wait_seconds=args.wait,
        verify=not args.no_verify,
    )

    if not resp.success:
        print(f"FAIL: {resp.format_error()}", file=sys.stderr)
        if resp.error_info:
            print(resp.error_info, file=sys.stderr)
        return 1

    if args.no_verify:
        print("Reload request sent. (--no-verify, not pinging new server.)")
        return 0

    # Verified path: resp.raw is the new server's ping payload.
    print("OK")
    print(f"  Bridge        : {resp.raw.get('bridge')} v{resp.raw.get('version')}")
    print(f"  Vivado        : {resp.raw.get('vivado')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
