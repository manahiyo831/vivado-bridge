# ============================================================
# vivado-bridge: TCL Socket Server
# ============================================================
# A small bridge that lets external Python clients drive Vivado
# by sending TCL snippets over a TCP socket.
#
# Design principles:
#   - Minimal TCL: only one real command (exec_tcl). Logic lives in Python.
#   - Local-by-default: bind 127.0.0.1 unless user explicitly opts in.
#   - Robust I/O: line-delimited JSON via tcllib's json package.
#   - No silent failures: every code path returns a structured response.
#   - Safe-by-default: refuse exec / destructive file ops at the bridge.
#
# How to use (from Vivado Tcl Console):
#     source /path/to/vivado_socket_server.tcl <port> ?<bind_addr>?
# Example:
#     source D:/.../vivado_socket_server.tcl 53729
#     source D:/.../vivado_socket_server.tcl 53729 0.0.0.0
#
# Port and bind address come from .env next to this script (the skill ships
# with sane defaults: 127.0.0.1:53729). Override on the source line above
# if you need a different port for a second Vivado instance.
# ============================================================

# Inbound JSON parsing uses tcllib's `json` package (Vivado ships it).
# Outbound JSON building is done by a tiny hand-rolled encoder below,
# because `json::write` is not always available alongside `json` in
# Vivado's bundled tcllib.
package require json

namespace eval ::vbridge {
    variable server_socket    ""
    variable server_port      ""
    variable server_host      ""
    variable client_sockets   {}
    variable version          "0.1.0"
}

# ============================================================
# Logging
# ============================================================
# All operational logs go through ::vbridge::_log so they reliably
# appear in the Vivado Tcl Console.
#
# Why send_msg_id instead of puts:
#   The Vivado Tcl Console only captures stdout from the synchronous
#   command-execution context. `puts` issued from a fileevent or a
#   timer callback is silently dropped. Vivado's own logging primitive,
#   `send_msg_id`, surfaces from all contexts (verified on Vivado 2024.1).
#
# No fallback on purpose: if send_msg_id fails it means the message id
# is malformed. The right response is to fix the id, not to mask the
# failure with a puts that may or may not show up. We let the caller's
# `catch` propagate so the bug surfaces.
#
# Message id format requirement: Vivado's `set_msg_config` /
# `reset_msg_count` reject ids whose subsystem alias contains '-'
# (Common 17-237). Keep the alias single-token (e.g. "vbridge").
proc ::vbridge::_log {msg} {
    send_msg_id "vbridge 1-1" INFO $msg
}

proc ::vbridge::_log_warn {msg} {
    send_msg_id "vbridge 1-2" WARNING $msg
}

proc ::vbridge::_log_err {msg} {
    send_msg_id "vbridge 1-3" ERROR $msg
}

# ============================================================
# Suppress noisy GUI dialogs that would otherwise block automation.
# WebTalk and backup files are disabled so post-synth/impl prompts
# do not pop up while a script is running.
#
# No silent fallback: each tweak is wrapped in catch only because
# the surrounding command may legitimately not exist on a given
# Vivado version (e.g. a param removed/renamed). We log the outcome
# either way so a missing knob is visible in the Console rather
# than disappearing into the void.
# ============================================================
proc ::vbridge::_set_param_logged {name value} {
    # Existence check first: a param that isn't user-definable on this
    # Vivado version should be surfaced as a skip, not a silent error.
    # `list_param` only returns user-definable params, so we probe with
    # `get_param` and treat its failure as "not present".
    if {[catch {get_param $name} _]} {
        _log "skip set_param $name (not present in this Vivado)"
        return
    }
    if {[catch {set_param $name $value} err]} {
        _log_warn "set_param $name $value failed: $err"
        return
    }
    _log "set_param $name = $value"
}

proc ::vbridge::quiet_vivado {} {
    if {[catch {config_webtalk -user off} err]} {
        _log_warn "config_webtalk -user off failed: $err"
    } else {
        _log "config_webtalk -user off"
    }
    _set_param_logged general.maxBackup 0
}

# ============================================================
# Server lifecycle
# ============================================================
proc ::vbridge::start_server {port {bind_addr "127.0.0.1"}} {
    variable server_socket
    variable server_port
    variable server_host

    # Refuse to start twice on the same interpreter.
    if {$server_socket ne ""} {
        _log "server already running on $server_host:$server_port"
        puts "  Use ::vbridge::stop_server first if you want to restart."
        return 0
    }

    if {[catch {socket -server ::vbridge::on_connect -myaddr $bind_addr $port} sock]} {
        # Port-in-use is the most common failure here. Help the user fix it.
        puts "============================================================"
        _log_err "FAILED to bind $bind_addr:$port"
        puts "  Reason: $sock"
        puts ""
        puts "Possible causes:"
        puts "  - Another Vivado already runs the bridge on this port"
        puts "  - Another application is listening on $port"
        puts "  - A previous server didn't shut down cleanly"
        puts ""
        puts "Fix: pick a different port and re-source, e.g."
        puts "  source <path>/vivado_socket_server.tcl [expr {$port + 1}]"
        puts "============================================================"
        return 0
    }

    set server_socket $sock
    set server_port   $port
    set server_host   $bind_addr

    puts "============================================================"
    puts "vivado-bridge v$::vbridge::version started"
    puts "  Listening on : $bind_addr:$port"
    puts "  Working dir  : [pwd]"
    if {$bind_addr eq "0.0.0.0"} {
        puts ""
        puts "  WARNING: bound to 0.0.0.0 -- this server has NO authentication"
        puts "           and accepts arbitrary TCL. Use only on trusted networks."
    }
    puts "============================================================"

    quiet_vivado
    _install_exit_hook
    return 1
}

proc ::vbridge::stop_server {} {
    variable server_socket
    variable client_sockets

    foreach c $client_sockets { catch {close $c} }
    set client_sockets {}

    if {$server_socket ne ""} {
        catch {close $server_socket}
        set server_socket ""
    }
    catch {
        trace remove execution exit enter [list ::vbridge::_on_vivado_exit]
    }
    _log "server stopped"
}

# Vivado closes its GUI but the process can linger because the listening
# socket and any client fileevent handlers keep Tcl's event loop alive.
# Hook `exit` so that whenever Vivado tears the interpreter down we close
# our sockets first, letting the process actually terminate.
#
# `trace add execution exit enter` fires before `exit` runs, regardless
# of whether `exit` was issued from the GUI close, File→Exit menu, or a
# Tcl Console `exit` call. We wrap stop_server in catch so that even if
# something inside it fails, we never block Vivado's shutdown path.
#
# Idempotent: a second source of this script tries to add the same trace,
# which Tcl rejects (trace already exists). The catch swallows that case.
proc ::vbridge::_install_exit_hook {} {
    catch {
        trace add execution exit enter [list ::vbridge::_on_vivado_exit]
    }
}

proc ::vbridge::_on_vivado_exit {args} {
    catch { ::vbridge::stop_server }
}

# ============================================================
# Connection handling
# ============================================================
proc ::vbridge::on_connect {sock addr port} {
    variable client_sockets

    # Connect/disconnect events are intentionally NOT logged. Each
    # exec_tcl call opens a fresh short-lived socket, so logging here
    # would triple every command line in the Console for no useful
    # signal. Errors during configuration are still logged below.
    lappend client_sockets $sock

    # Line-buffered, non-blocking, UTF-8. Each request is one JSON line.
    # -translation lf keeps the wire format consistent across OSes.
    if {[catch {
        fconfigure $sock -buffering line -blocking 0 \
                          -encoding utf-8 -translation lf
        fileevent  $sock readable [list ::vbridge::on_readable $sock]
    } err]} {
        _log_err "failed to configure client socket: $err"
        catch {close $sock}
        ::vbridge::_forget_client $sock
    }
}

proc ::vbridge::_forget_client {sock} {
    variable client_sockets
    set idx [lsearch $client_sockets $sock]
    if {$idx >= 0} {
        set client_sockets [lreplace $client_sockets $idx $idx]
    }
}

proc ::vbridge::on_readable {sock} {
    # Outer catch: nothing inside this proc may leak an exception, otherwise
    # the fileevent loop would be silently disabled.
    if {[catch {::vbridge::_on_readable_impl $sock} err opts]} {
        _log_err "on_readable crashed: $err"
        puts "  $::errorInfo"
        catch {
            send_response $sock [build_error "internal_error" \
                "on_readable crashed: $err" \
                [dict get $opts -errorinfo]]
        }
        catch {close $sock}
        ::vbridge::_forget_client $sock
    }
}

proc ::vbridge::_on_readable_impl {sock} {
    if {[eof $sock]} {
        # See on_connect: regular disconnects are too noisy to log.
        catch {close $sock}
        ::vbridge::_forget_client $sock
        return
    }

    if {[catch {gets $sock line} nread]} {
        _log_err "socket read error: $nread"
        catch {close $sock}
        ::vbridge::_forget_client $sock
        return
    }
    # gets returns -1 with no error if a complete line isn't ready yet.
    if {$nread < 0} { return }
    if {$line eq ""} { return }

    # From here every code path MUST end in a send_response so the client
    # never hangs waiting for a reply.
    if {[catch {::vbridge::handle_line $line} resp opts]} {
        # Defensive: handle_line itself raised. Build a generic error.
        set einfo ""
        catch { set einfo [dict get $opts -errorinfo] }
        send_response $sock [build_error "internal_error" \
            "Unhandled exception in handle_line: $resp" $einfo]
        return
    }
    send_response $sock $resp
}

# ============================================================
# Request dispatch
# ============================================================
proc ::vbridge::handle_line {line} {
    if {[catch {::json::json2dict $line} req]} {
        return [build_error "protocol_error" \
            "Failed to parse JSON: $req" ""]
    }

    if {![dict exists $req command]} {
        return [build_error "protocol_error" \
            "Missing required field: command" ""]
    }
    set cmd [dict get $req command]

    set params [dict create]
    if {[dict exists $req params]} {
        # If params is present but not a dict-shaped object, treat as protocol error.
        if {[catch {dict size [dict get $req params]} _]} {
            return [build_error "protocol_error" \
                "Field 'params' must be a JSON object" ""]
        }
        set params [dict get $req params]
    }

    switch -- $cmd {
        "ping"      { return [cmd_ping     $params] }
        "exec_tcl"  { return [cmd_exec_tcl $params] }
        "shutdown"  { return [cmd_shutdown $params] }
        default {
            return [build_error "unknown_command" \
                "Unknown command: $cmd" ""]
        }
    }
}

# ============================================================
# Commands
# ============================================================

# ping: liveness + identity probe. Lets the Python client confirm it
# is talking to a real vivado-bridge (not some other app on the port).
proc ::vbridge::cmd_ping {params} {
    variable version
    set vivado_ver "unknown"
    catch { set vivado_ver [version -short] }

    return [build_ok [dict create \
        pong         "pong" \
        bridge       "vivado-bridge" \
        version      $version \
        vivado       $vivado_ver \
    ]]
}

# exec_tcl: evaluate the given TCL code in the global scope and return
# its result. Blocks dangerous commands at the bridge layer.
#
# Hang-prevention notes:
#   This proc cannot, by itself, prevent a slow Vivado command (e.g. a long
#   synthesis run, or a wait_on_run) from blocking. Avoid those at the call
#   site; they should be driven by the polling helpers in scripts/. Anything
#   that reaches `uplevel` here is run synchronously in Vivado's main thread
#   and will block the whole GUI until it returns.
proc ::vbridge::cmd_exec_tcl {params} {
    if {![dict exists $params tcl_code]} {
        return [build_error "protocol_error" \
            "Missing required parameter: tcl_code" ""]
    }
    set tcl_code [dict get $params tcl_code]

    if {[string trim $tcl_code] eq ""} {
        return [build_error "protocol_error" \
            "Parameter 'tcl_code' is empty" ""]
    }

    # Block dangerous commands BEFORE evaluating. The check is intentionally
    # simple (first-token inspection); it's an "oops" guardrail, not sandboxing.
    set blocked [is_blocked $tcl_code]
    if {[lindex $blocked 0] eq "blocked"} {
        return [build_blocked [lindex $blocked 1] [lindex $blocked 2]]
    }

    _log "exec_tcl: $tcl_code"

    # Record start time so we can flag long-running commands. Vivado's Tcl
    # is single-threaded: while we're inside `uplevel` here, the Tcl
    # Console is also blocked. Logging the duration on completion lets a
    # user who sees the GUI "freeze" check the Console afterwards and
    # understand it was the bridge running their long command.
    set start_ms [clock milliseconds]

    # uplevel #0 evaluates in the global scope so users can refer to global
    # state (current project, opened hw_target, etc.) the way the Vivado
    # Tcl Console does.
    set rc [catch {uplevel #0 $tcl_code} result opts]
    set elapsed_ms [expr {[clock milliseconds] - $start_ms}]

    # Surface long executions with a warning so it's visible in the
    # Console log even amid the regular INFO stream. 5s is a soft
    # threshold tuned for "noticeably blocking" interactive use; tweak
    # if it's too chatty.
    if {$elapsed_ms >= 5000} {
        set elapsed_s [format "%.1f" [expr {$elapsed_ms / 1000.0}]]
        _log_warn "exec_tcl finished after ${elapsed_s}s (Tcl Console was blocked during this time): [string range $tcl_code 0 80]"
    }

    # Tcl return codes:
    #   0 TCL_OK        -- normal completion
    #   1 TCL_ERROR     -- real Tcl error
    #   2 TCL_RETURN    -- a `return ...` evaluated at top level; the value
    #                      is in $result and is what the caller wants
    #   3 TCL_BREAK     -- only meaningful inside a loop; at top level it's
    #                      a misuse, so treat as error
    #   4 TCL_CONTINUE  -- same as BREAK at top level
    # We treat 0 and 2 as success (snippets like `return $x` are common and
    # idiomatic for sending values back through the bridge). 1, 3, 4 are
    # surfaced as tcl_error.
    if {$rc == 0 || $rc == 2} {
        # `result` may be empty (many Vivado commands return ""). That's
        # still a successful response -- never silently drop it.
        return [build_ok [dict create output $result]]
    }

    set einfo ""
    set ecode ""
    catch { set einfo [dict get $opts -errorinfo] }
    catch { set ecode [dict get $opts -errorcode] }
    return [build_tcl_error $result $einfo $ecode]
}

# shutdown: stop accepting new connections, close existing ones, free port.
# We schedule the actual stop with `after idle` so the response goes out
# before the listener closes.
proc ::vbridge::cmd_shutdown {params} {
    after idle ::vbridge::stop_server
    return [build_ok [dict create message "Server is shutting down. Vivado remains running."]]
}

# ============================================================
# Command guard
# ============================================================
# Refuses commands that should not flow through the bridge:
#   - exec (any OS process invocation)
#   - file <destructive subcommand>
#
# Read-only `file` subcommands (exists, dirname, join, etc.) are allowed
# because Vivado scripts use them constantly.
#
# Limitations:
#   - First-token check only. Workarounds like {eval "ex" "ec ..."} or
#     re-aliasing exec to another name are not caught. Local-trust model.
proc ::vbridge::is_blocked {tcl_code} {
    set trimmed [string trim $tcl_code]
    if {$trimmed eq ""} { return [list ok "" ""] }

    # Tokenize cautiously — malformed input must not crash the guard.
    if {[catch {lindex $trimmed 0} first]} {
        return [list ok "" ""]
    }

    if {$first eq "exec"} {
        return [list blocked "exec" \
            "Command 'exec' is blocked by vivado-bridge. Run OS processes via Claude Code host tools (Bash/PowerShell), not through the TCL bridge."]
    }

    if {$first eq "file"} {
        if {[catch {lindex $trimmed 1} sub]} { set sub "" }
        # Destructive / side-effecting `file` subcommands.
        set destructive {delete rename copy mkdir attributes link tempfile}
        if {[lsearch -exact $destructive $sub] >= 0} {
            return [list blocked "file $sub" \
                "Command 'file $sub' is blocked by vivado-bridge. Modify files via Claude Code host tools (Edit/Write/Bash) instead."]
        }
    }

    return [list ok "" ""]
}

# ============================================================
# Response builders
# All responses share the schema:
#   { "success": <bool>, "error_kind": <string|null>, ...payload... }
# Outbound JSON is built by hand because `json::write` is not always
# bundled alongside `json` in Vivado. The encoder only needs to handle
# strings, booleans and null — that's the entire response surface.
# ============================================================

# Escape a Tcl string so it is safe inside a JSON string literal.
# Handles: backslash, double-quote, control chars (\b \f \n \r \t),
# and any other char < 0x20 via \uXXXX.
proc ::vbridge::_json_escape {s} {
    set out ""
    set len [string length $s]
    for {set i 0} {$i < $len} {incr i} {
        set ch [string index $s $i]
        scan $ch %c code
        switch -- $ch {
            "\\" { append out "\\\\" }
            "\"" { append out "\\\"" }
            "\b" { append out "\\b" }
            "\f" { append out "\\f" }
            "\n" { append out "\\n" }
            "\r" { append out "\\r" }
            "\t" { append out "\\t" }
            default {
                if {$code < 0x20} {
                    append out [format "\\u%04x" $code]
                } else {
                    append out $ch
                }
            }
        }
    }
    return $out
}

proc ::vbridge::_json_str  {s} { return "\"[_json_escape $s]\"" }
proc ::vbridge::_json_bool {b} { return [expr {$b ? "true" : "false"}] }
proc ::vbridge::_json_null {}  { return "null" }

# Build a JSON object from a flat dict of name -> already-JSON-encoded value.
# Keys are emitted in dict insertion order (Tcl dicts preserve it).
proc ::vbridge::_json_obj {kv} {
    set parts {}
    dict for {k v} $kv {
        lappend parts "[_json_str $k]:$v"
    }
    return "\{[join $parts ","]\}"
}

proc ::vbridge::build_ok {extra_dict} {
    set kv [dict create \
        success    [_json_bool 1] \
        error_kind [_json_null]]
    dict for {k v} $extra_dict {
        dict set kv $k [_json_str $v]
    }
    return [_json_obj $kv]
}

proc ::vbridge::build_error {kind message error_info} {
    return [_json_obj [dict create \
        success    [_json_bool 0] \
        error_kind [_json_str $kind] \
        message    [_json_str $message] \
        error_info [_json_str $error_info] \
        output     [_json_str ""]]]
}

proc ::vbridge::build_tcl_error {result error_info error_code} {
    return [_json_obj [dict create \
        success    [_json_bool 0] \
        error_kind [_json_str "tcl_error"] \
        message    [_json_str $result] \
        error_info [_json_str $error_info] \
        error_code [_json_str $error_code] \
        output     [_json_str ""]]]
}

proc ::vbridge::build_blocked {token message} {
    return [_json_obj [dict create \
        success       [_json_bool 0] \
        error_kind    [_json_str "blocked_command"] \
        message       [_json_str $message] \
        blocked_token [_json_str $token] \
        output        [_json_str ""]]]
}

# ============================================================
# Send a response line and flush. We always write a single line with
# trailing newline so the client's gets() returns immediately.
# Failures here are logged but cannot themselves trigger a response.
# ============================================================
proc ::vbridge::send_response {sock json} {
    if {[catch {puts $sock $json} err]} {
        _log_err "failed to send response: $err"
        catch {close $sock}
        ::vbridge::_forget_client $sock
        return
    }
    if {[catch {flush $sock} err]} {
        _log_err "failed to flush response: $err"
        catch {close $sock}
        ::vbridge::_forget_client $sock
    }
}

# ============================================================
# .env loading
# ============================================================
# Reads KEY=VALUE pairs from a .env file next to this script.
# Lines starting with # and blank lines are skipped. Values are
# returned trimmed; surrounding quotes (single or double) are stripped.
# Returns a dict; missing file yields an empty dict.
proc ::vbridge::_load_env {path} {
    set env_dict [dict create]
    if {![file exists $path]} { return $env_dict }
    if {[catch {open $path r} fp]} {
        _log_warn "cannot open $path: $fp"
        return $env_dict
    }
    set content ""
    catch { set content [read $fp] }
    catch { close $fp }
    foreach raw [split $content "\n"] {
        set line [string trim $raw]
        if {$line eq "" || [string index $line 0] eq "#"} { continue }
        set eq [string first "=" $line]
        if {$eq < 1} { continue }
        set key [string trim [string range $line 0 [expr {$eq - 1}]]]
        set val [string trim [string range $line [expr {$eq + 1}] end]]
        # Strip optional surrounding quotes.
        if {[string length $val] >= 2} {
            set first [string index $val 0]
            set last  [string index $val end]
            if {($first eq "\"" && $last eq "\"") || ($first eq "'" && $last eq "'")} {
                set val [string range $val 1 end-1]
            }
        }
        dict set env_dict $key $val
    }
    return $env_dict
}

# ============================================================
# Bootstrap when sourced.
#
# Configuration resolution (each setting independently):
#   1. Tcl global variable (::vbridge_port / ::vbridge_host) — explicit override
#   2. .env file next to this script (VIVADO_BRIDGE_PORT / VIVADO_BRIDGE_HOST)
#   3. Error — no defaults; user must configure once
#
# Typical usage (after editing .env):
#   source D:/path/vivado_socket_server.tcl
#
# Override for a second Vivado instance:
#   set ::vbridge_port 53730
#   source D:/path/vivado_socket_server.tcl
# ============================================================
proc ::vbridge::_resolve_config {} {
    # Locate .env next to this script. [info script] returns the path of
    # the file currently being sourced.
    set script_path [info script]
    set script_dir  [file dirname $script_path]
    set env_path    [file join $script_dir ".env"]
    set env         [_load_env $env_path]

    # Port: global var first, then .env.
    set port ""
    if {[info exists ::vbridge_port] && $::vbridge_port ne ""} {
        set port $::vbridge_port
    } elseif {[dict exists $env VIVADO_BRIDGE_PORT]} {
        set port [dict get $env VIVADO_BRIDGE_PORT]
    }

    # Host: global var first, then .env.
    set host ""
    if {[info exists ::vbridge_host] && $::vbridge_host ne ""} {
        set host $::vbridge_host
    } elseif {[dict exists $env VIVADO_BRIDGE_HOST]} {
        set host [dict get $env VIVADO_BRIDGE_HOST]
    }

    return [list $port $host $env_path]
}

proc ::vbridge::_bootstrap {} {
    lassign [_resolve_config] port host env_path

    if {$port eq "" || $host eq ""} {
        puts "============================================================"
        _log_err "configuration missing (see Tcl Console for details)"
        puts ""
        if {$port eq ""} { puts "  Port is not set." }
        if {$host eq ""} { puts "  Bind host is not set." }
        puts ""
        puts "How to configure:"
        puts ""
        puts "  Option 1 (recommended): edit .env next to this script:"
        puts "    $env_path"
        puts "  Example contents:"
        puts "    VIVADO_BRIDGE_HOST=127.0.0.1"
        puts "    VIVADO_BRIDGE_PORT=53729"
        puts ""
        puts "  Option 2: set Tcl globals before sourcing:"
        puts "    set ::vbridge_port 53729"
        puts "    set ::vbridge_host 127.0.0.1   ;# optional if .env has it"
        puts "    source <path>/vivado_socket_server.tcl"
        puts "============================================================"
        return
    }

    if {![string is integer -strict $port] || $port < 1 || $port > 65535} {
        _log_err "port must be an integer in 1..65535, got: $port"
        return
    }

    ::vbridge::start_server $port $host
    return ;# suppress the start_server return value from leaking to the Tcl Console
}

::vbridge::_bootstrap
