# bridge operations

`from operations import bridge`

Bridge / Vivado-process introspection. Right now this is just one
operation: locate the per-session log files Vivado writes.

## Common shape

All operations return a dict with `success`, `error_kind`, `message`,
`warnings`. On `tcl_error` failures the result also carries
`error_info` and `error_code`. Operation-specific fields are listed
below.

## Operations

### get_vivado_logs

Return the paths of Vivado's session log files.

```python
bridge.get_vivado_logs(client)
# {
#   'success': True,
#   'cwd': 'C:/Users/<user>/AppData/Roaming/Xilinx/Vivado',
#   'log_path': '.../vivado.log', 'log_exists': True, 'log_size': 183268,
#   'jou_path': '.../vivado.jou', 'jou_exists': True, 'jou_size':   1465,
# }
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

```python
from vivado_bridge_client import Client
from operations import bridge

c = Client.connect()
info = bridge.get_vivado_logs(c)
print(info["log_path"])
# Then in your assistant: Read or Grep that file as needed.
#   Grep -n "ERROR" vivado.log
#   Read vivado.log  offset=last_known_line  limit=200
```
