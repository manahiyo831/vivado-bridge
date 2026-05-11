#!/usr/bin/env python3
"""Send a single TCL command to the running vivado-bridge server.

Usage:
    python scripts/exec_tcl.py "TCL_COMMAND"

Examples:
    python scripts/exec_tcl.py "pwd"
    python scripts/exec_tcl.py "version -short"
    python scripts/exec_tcl.py "create_project demo ./projects/demo -part xc7z020clg400-1 -force"

Configuration (host/port) is read from .env next to vivado_socket_server.tcl.
The skill ships with a working .env (127.0.0.1:53729); edit it only if
you need to change those.

Exit codes:
    0  success
    1  command was rejected by the bridge or Tcl raised an error
    2  could not reach the server / configuration is missing
"""

import argparse
import sys

from vivado_bridge_client import Client, ConfigError


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run one Tcl snippet on the vivado-bridge server.",
    )
    parser.add_argument("tcl", help="Tcl code to evaluate")
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="Seconds to wait for a response (default: 30)",
    )
    args = parser.parse_args()

    try:
        client = Client(timeout=args.timeout)
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    resp = client.exec_tcl(args.tcl)

    if resp.success:
        if resp.output:
            print(resp.output)
        return 0

    # Errors go to stderr so callers piping stdout don't get garbage.
    print(f"ERROR: {resp.format_error()}", file=sys.stderr)
    if resp.error_info:
        print(resp.error_info, file=sys.stderr)
    if resp.error_kind == "client_error":
        return 2
    return 1


if __name__ == "__main__":
    sys.exit(main())
