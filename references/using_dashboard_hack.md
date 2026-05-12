# Auto-populating the VIO Dashboard and ILA Waveform pane

> Best-effort hack. Vivado does not provide a Tcl API for this; we
> edit two project files directly. Validated against Vivado 2024.1.
> If a future Vivado version changes hw.xml's schema this stops
> working, and the SKILL falls back to "ask the user to Add Probes
> in the GUI" (see [using_vio.md](using_vio.md) §6).

## Why this exists

The most expensive friction in AI-driven debug with vivado-bridge is
the **last mile to the human eye**. The AI can drive VIO probes,
arm ILA, capture data, and analyze CSV without any GUI -- but if
the user wants to *also* see what's happening in the Vivado IDE,
they have to manually right-click each probe, "Add Probes...", drag
them into a sensible order, set radix, and pick Digital/Analog.
Every fresh project, every fresh `program_device`, every time.

`scripts/setup_dashboard.py` does that step from a JSON layout file.
Run it between `close_hw_manager` and re-open, and the user sees
the dashboard you described, in the order you wanted, with the
radix and waveform style you chose.

## The mechanism

Two files are involved. Vivado *reads* both at HW Manager open time
and *writes* both at `close_hw_manager` time. **Neither is touched
by Ctrl+S.**

```
project_<N>.hw/hw_1/
├── hw.xml                              <- VIO dashboard layout, ILA waveform style
├── hw.xml.bak_setup                    <- our pre-edit backup
├── layout/hw_ila_1.layout              <- ILA window pane sizes (untouched)
└── wave/hw_ila_data_1/
    ├── hw_ila_data_1.wcfg              <- ILA Waveform pane: which probes, what order
    └── hw_ila_data_1.wdb               <- last captured sample data
```

### What goes into `hw.xml`

For each VIO probe shown on a dashboard:

```xml
<Object name="mode_select_2"
        gui_info="dashboard_1/hw_vio_1=0"/>
```

The `gui_info` attribute is comma-separated `key=value` pairs. The
key `dashboard_1/hw_vio_1=N` means "this probe occupies row N of
the VIO panel inside `dashboard_1`". Removing the key removes the
probe from the dashboard. Other keys (`hw_vios/hw_vio_1=N`,
`Trigger Setup=N`) control unrelated panels and we leave them
alone.

For each ILA probe's waveform style:

```xml
<probe type="ila" ...>
  <probeOptions Id="DebugProbeParams">
    <Option Id="WAVEFORM_STYLE" value="Analog"/>   <!-- or Digital -->
    ...
  </probeOptions>
  <nets>
    <net name="fast_counter[7]"/>
    ...
  </nets>
</probe>
```

This is the *style* setting only. The Analog Settings dialog in the
GUI also has Row Height, Y Range Auto/Fixed, Min/Max, Hold/Linear,
Hide/Clip/Overlap and Horizontal Line -- **none of those are
persisted in hw.xml**. They reset to defaults on every re-open.

For VIO probe radixes:

```xml
<probe type="vio_input" ...>
  <Option Id="INPUT_VALUE_RADIX" value="HEX"/>     <!-- or BINARY -->
  ...
</probe>
<probe type="vio_output" ...>
  <Option Id="OUTPUT_VALUE_RADIX" value="BINARY"/>
  ...
</probe>
```

### What goes into `hw_ila_data_1.wcfg`

The ILA *Waveform pane* is a separate concept from the ILA core.
It's a saved view, like a `.wcfg` file in the simulator. The
`<wvobject>` list defines which probes appear and in what order:

```xml
<wave_config>
   ...
   <WVObjectSize size="6"/>
   <wvobject fp_name="fast_counter" type="array">
      <obj_property name="ElementShortName">fast_counter[7:0]</obj_property>
      <obj_property name="ObjectShortName">fast_counter[7:0]</obj_property>
      <obj_property name="Radix">UNSIGNEDDECRADIX</obj_property>
   </wvobject>
   ...
</wave_config>
```

Radix tokens here are *different* from the hw.xml ones:

| Our config | wcfg token |
|---|---|
| `HEX` | `HEXRADIX` |
| `BINARY` | `BINRADIX` |
| `UNSIGNED` | `UNSIGNEDDECRADIX` |
| `SIGNED` | `SIGNEDDECRADIX` |

`type="array"` for buses, `type="logic"` for single-bit. `fp_name`
is the bare stem, `ElementShortName` / `ObjectShortName` is the
display name (with `[hi:lo]` suffix on buses).

## Workflow

The full cycle is: flush Vivado's in-memory HW Manager state, edit
the project files outside Vivado, then re-open and refresh. Each
step is one dispatcher call (or one subprocess invocation for the
file edit):

```bash
# 1. Flush whatever Vivado has in memory.
python vivado_op.py '{"op":"hardware.close_hardware_target","params":{}}'
python exec_tcl.py "close_hw_manager"

# 2. Edit hw.xml + wcfg from outside.
python <bridge>/scripts/setup_dashboard.py \
    <project>/project_1.hw/hw_1/hw.xml \
    --config my_layout.json

# 3. Re-open. The new layout is picked up.
python vivado_op.py '{"op":"hardware.open_hw_manager","params":{}}'
python vivado_op.py '{"op":"hardware.connect_hw_server","params":{}}'
python vivado_op.py '{"op":"hardware.open_hardware_target","params":{"force_refresh":true}}'
python vivado_op.py '{"op":"hardware.open_hardware_device","params":{"device_filter":"xc7"}}'
python exec_tcl.py "refresh_hw_device [current_hw_device]"
```

The user sees the new layout immediately after step 3. No clicks
required.

## Layout config

```json
{
  "vio": [
    {"name": "mode_select_2",   "direction": "out", "radix": "BINARY"},
    {"name": "led_force_2[3:0]", "direction": "out", "radix": "HEX"},
    {"name": "toggle_1",        "direction": "in",  "radix": "BINARY"}
  ],
  "ila": [
    {"name": "mode_select",       "style": "Digital", "radix": "BINARY"},
    {"name": "fast_counter[7:0]", "style": "Analog",  "radix": "UNSIGNED"}
  ]
}
```

Order of entries = display order on the dashboard / pane. Either
list can be empty (skip that pane). Names must match exactly what
`debug.list_vio_probes` and the ILA `<probe>` `<nets>` sections
report at runtime; don't guess from the IP-port names.

A complete example lives at
[scripts/dashboard_layout.example.json](../scripts/dashboard_layout.example.json).

## Limits and risks

- **Schema is undocumented.** Vivado may change hw.xml/wcfg between
  versions. If you hit a Vivado release that doesn't accept the
  rewritten file, the symptom is the dashboard appearing empty
  after re-open (Vivado silently ignored the file). The script
  always writes a `.bak_setup` next to the original; restore by
  copying it back.
- **Analog Settings detail is volatile.** Only `WAVEFORM_STYLE`
  itself sticks. Y Range Fixed/Auto, Min/Max, Hold, Hide off-scale,
  Horizontal Line all reset on every re-open. There is no known
  way to persist them today.
- **Dashboard names hardcoded.** The script targets `dashboard_1`
  and `hw_ila_data_1` because that's the default Vivado creates.
  Multi-dashboard projects need the script tweaked.
- **Doesn't read from hw.xml first.** The script doesn't try to
  preserve unknown `<Option>` keys it didn't write -- it just
  appends what it needs. If the user has set additional Options
  through the GUI, those are kept (the script only touches the
  options it knows about), but if Vivado adds a new required
  option in a future version, it won't be added.
- **wcfg reads probe metadata from the matching .wdb.** If the
  ILA hasn't been captured at least once, the `.wdb` may be
  empty and Vivado may resolve probe display lazily. After
  installing the layout, do a `run_hw_ila` + `upload_hw_ila_data`
  + `display_hw_ila_data` to populate the pane.

## When to use the GUI fallback instead

- The user has an existing complex dashboard layout you don't want
  to overwrite. (The script *will* overwrite the dashboard layout.
  Always back up `hw.xml` first if the existing state matters.)
- You're investigating a single signal once and don't expect to
  reuse the layout. Right-click -> Add Probes is faster than
  writing a JSON config.
- Vivado has updated and the script no longer works -- ask the
  user to do it in the GUI and revisit the script.
