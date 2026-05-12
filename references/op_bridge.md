# bridge operations

Bridge / Vivado-process introspection. One operation: locate the
per-session log files Vivado writes.

All operations are invoked via the `vivado_op.py` JSON dispatcher.
See [SKILL.md](../SKILL.md) for the invocation pattern.

## Common shape

All operations return a dict with `success`, `error_kind`, `message`,
`warnings`. On `tcl_error` failures the result also carries
`error_info` and `error_code`. Operation-specific fields are listed
below.

## Operations

### bridge.get_vivado_logs

Return the paths of Vivado's session log files.

**Request:**
```json
{"op": "bridge.get_vivado_logs"}
```

**Response:**
```json
{
  "success": true,
  "cwd": "C:/Users/<user>/AppData/Roaming/Xilinx/Vivado",
  "log_path": ".../vivado.log", "log_exists": true, "log_size": 183268,
  "jou_path": ".../vivado.jou", "jou_exists": true, "jou_size": 1465
}
```

`vivado.log` is, in practice, **a transcript of the Tcl Console** for
this session: every INFO / WARNING / ERROR line that appears in the
Console gets written here as well, including the bridge's own
`exec_tcl: ...` log lines. Reading it is the closest thing you can do
to "see what's on the Tcl Console" without sitting in front of Vivado.

Caveats:

- The match is *not guaranteed* to be 100% bit-identical -- Vivado
  decides which messages to mirror where, and could in principle log
  something to the file that doesn't appear in the Console (or vice
  versa). The two are documented here as "effectively equivalent" but
  if you're chasing a subtle bug, treat that equivalence as an
  observation, not a contract.
- The file can be **large** (megabytes after a long session). Don't
  read the whole thing at once; use your host-side `Read` (with
  offset / limit) or `Grep` (with a pattern). The operation only
  returns the path, never the contents.
- `vivado.jou` is a tighter record of just the Tcl commands executed
  this session. Useful when you want a clean playback of "what
  happened" without all the INFO/WARNING noise.
- Both files live in Vivado's current working directory (`pwd`).
  That's typically `C:\Users\<you>\AppData\Roaming\Xilinx\Vivado` on
  Windows but moves when the user runs `cd` in the Tcl Console.

## Typical flow

```bash
# 1. Get the log path
echo '{"op":"bridge.get_vivado_logs"}' | python vivado_op.py
# → response includes log_path

# 2. Then on the host side, Read or Grep the file at log_path as needed:
#    Grep -n "ERROR" <log_path>
#    Read <log_path> with offset=last_known_line limit=200
```

The bridge intentionally only returns the *path*. Reading the contents
is done by the host-side tools so the agent's context window controls
how much of the log is pulled in.
