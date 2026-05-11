#!/usr/bin/env python3
"""Verify that the vivado-bridge server is reachable and identifying
itself correctly. Prints Vivado version and current Tcl pwd.

Use this when starting work to confirm the bridge is alive before
running heavier scripts.

Exit codes:
    0  bridge is reachable and identified
    1  bridge responded but identity check failed (wrong app on the port)
    2  could not reach the server / config missing
"""

import sys

from vivado_bridge_client import Client, ConfigError


def main() -> int:
    try:
        client = Client()
    except ConfigError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2

    print(f"Connecting to vivado-bridge at {client.host}:{client.port} ...")
    resp = client.ping()

    if not resp.success:
        if resp.error_kind == "identity_error":
            print(f"FAIL: {resp.message}", file=sys.stderr)
            return 1
        print(f"FAIL: {resp.format_error()}", file=sys.stderr)
        return 2

    print("OK")
    print(f"  Bridge        : {resp.raw.get('bridge')} v{resp.raw.get('version')}")
    print(f"  Vivado        : {resp.raw.get('vivado')}")

    # Best-effort: also report current Tcl pwd. Failure here is informational.
    pwd_resp = client.exec_tcl("pwd")
    if pwd_resp.success:
        print(f"  Tcl pwd       : {pwd_resp.output}")
    else:
        print(f"  Tcl pwd       : (failed: {pwd_resp.format_error()})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
