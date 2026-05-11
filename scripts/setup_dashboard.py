"""
Auto-populate Vivado VIO Dashboard layout and ILA Waveform pane by
editing the project's hw.xml and the corresponding .wcfg.

WHY THIS EXISTS
---------------
Vivado does not expose a Tcl API for "Add Probes to Dashboard" or
"arrange the Waveform pane". Those are GUI-only actions in the IDE.
The state they produce, however, is persisted in two project files
that Vivado re-reads when the Hardware Manager is closed and reopened:

    project_1.hw/hw_1/hw.xml
        - VIO probe -> dashboard slot mapping
          (gui_info="dashboard_1/hw_vio_1=N")
        - Each ILA probe's WAVEFORM_STYLE (Digital | Analog)
        - hw_vio probe radix (INPUT_VALUE_RADIX, OUTPUT_VALUE_RADIX)

    project_1.hw/hw_1/wave/hw_ila_data_1/hw_ila_data_1.wcfg
        - Which ILA probes appear in the Waveform pane, in what order
        - Per-probe radix (HEXRADIX, BINRADIX, UNSIGNEDDECRADIX, ...)

By rewriting these two files between a close_hw_manager and a
re-open + refresh, an AI can install a complete dashboard layout
without any GUI clicks.

LIMITS
------
- Best effort: hw.xml is not a documented public schema. Vivado 2024.1
  is the only version this has been validated against.
- Analog Settings details (Row Height, Y Range, Hold, Off-scale) are
  *not* persisted -- only WAVEFORM_STYLE=Analog itself is. Re-open
  resets the analog presentation to defaults.
- The dashboard name is hardcoded to dashboard_1 / hw_ila_data_1
  because that's how Vivado names the first dashboard / data record
  it creates. If a project uses different names, edit the script.

WORKFLOW (driven by the caller)
-------------------------------
    # 1. flush whatever Vivado currently has in memory
    close_hardware_target(c)
    c.exec_tcl("close_hw_manager")
    # 2. rewrite hw.xml + wcfg
    python setup_dashboard.py <hw.xml-path> --config layout.json
    # 3. re-open
    open_hw_manager(c)
    connect_hw_server(c)
    open_hardware_target(c, force_refresh=True)
    open_hardware_device(c, device_filter="xc7")
    c.exec_tcl("refresh_hw_device [current_hw_device]")

CONFIG FILE
-----------
JSON with two top-level lists. Empty lists are allowed (skip that pane).

    {
      "vio": [
        {"name": "mode_select_2",  "radix": "BINARY", "direction": "out"},
        {"name": "led_force_2[3:0]", "radix": "HEX",  "direction": "out"},
        ...
      ],
      "ila": [
        {"name": "mode_select",       "style": "Digital", "radix": "BINARY"},
        {"name": "fast_counter[7:0]", "style": "Analog",  "radix": "UNSIGNED"},
        ...
      ]
    }

VIO `direction` is "in" or "out" -- it picks INPUT_VALUE_RADIX vs
OUTPUT_VALUE_RADIX. ILA `radix` accepts HEX/BINARY/UNSIGNED/SIGNED.
"""

from __future__ import annotations
import argparse
import json
import shutil
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# helpers: hw.xml gui_info attribute (comma-separated key=value)
# ---------------------------------------------------------------------------

def _split_gui_info(value: str) -> list[tuple[str, str]]:
    if not value:
        return []
    out = []
    for part in value.split(","):
        if "=" in part:
            k, v = part.split("=", 1)
            out.append((k.strip(), v.strip()))
    return out


def _join_gui_info(pairs: Iterable[tuple[str, str]]) -> str:
    return ",".join(f"{k}={v}" for k, v in pairs)


def _set_gui_info_key(obj: ET.Element, key: str, value: str | None) -> None:
    pairs = _split_gui_info(obj.get("gui_info", ""))
    pairs = [(k, v) for k, v in pairs if k != key]
    if value is not None:
        pairs.append((key, value))
    obj.set("gui_info", _join_gui_info(pairs))


# ---------------------------------------------------------------------------
# helpers: locate <Object> entries and <probe> definitions
# ---------------------------------------------------------------------------

def _find_probe_object(root: ET.Element, name: str) -> ET.Element | None:
    """ <ObjectList object_type='hw_probe'> / <Object name='X'> """
    for ol in root.findall("./ObjectList[@object_type='hw_probe']"):
        for obj in ol.findall("Object"):
            if obj.get("name") == name:
                return obj
    return None


def _find_probe_definitions(root: ET.Element) -> dict[str, ET.Element]:
    """Map canonical probe name -> <probe> element. Bus probes appear
    as 'X[hi:lo]'; single-bit probes as 'X'."""
    result: dict[str, ET.Element] = {}
    for probeset in root.findall("./probeset"):
        for probe in probeset.findall("probe"):
            nets = [n.get("name") for n in probe.findall("nets/net")]
            if not nets:
                continue
            stems = {n.rsplit("[", 1)[0] for n in nets}
            if len(stems) != 1:
                continue
            stem = next(iter(stems))
            if any("[" in n for n in nets):
                hi = max(int(n.split("[")[1].rstrip("]")) for n in nets)
                lo = min(int(n.split("[")[1].rstrip("]")) for n in nets)
                pname = f"{stem}[{hi}:{lo}]"
            else:
                pname = stem
            result[pname] = probe
    return result


def _set_probe_option(probe: ET.Element, option_id: str, value: str) -> None:
    opts = probe.find("probeOptions")
    if opts is None:
        return
    for opt in opts.findall("Option"):
        if opt.get("Id") == option_id:
            opt.set("value", value)
            return
    ET.SubElement(opts, "Option", {"Id": option_id, "value": value})


# ---------------------------------------------------------------------------
# wcfg rewriting
# ---------------------------------------------------------------------------

_WCFG_RADIX = {
    "HEX": "HEXRADIX",
    "BINARY": "BINRADIX",
    "UNSIGNED": "UNSIGNEDDECRADIX",
    "SIGNED": "SIGNEDDECRADIX",
}


def _stem_to_array_or_net(name: str) -> tuple[str, str]:
    if "[" in name and ":" in name:
        return name.split("[", 1)[0], "array"
    return name, "logic"


def _rewrite_wcfg(wcfg_path: Path, ila_layout: list[dict[str, Any]]) -> None:
    """Replace <wvobject> entries (and WVObjectSize) so the Waveform
    pane shows exactly the listed probes in order. Other settings
    (db_ref, zoom, column widths, markers) are preserved."""
    if not wcfg_path.exists():
        print(f"    [warn] wcfg not found: {wcfg_path} (skipping ILA wave)")
        return
    tree = ET.parse(wcfg_path)
    root = tree.getroot()

    for w in list(root.findall("wvobject")):
        root.remove(w)

    sz = root.find("WVObjectSize")
    if sz is not None:
        sz.set("size", str(len(ila_layout)))

    for entry in ila_layout:
        name = entry["name"]
        radix = entry.get("radix", "HEX")
        stem, kind = _stem_to_array_or_net(name)
        wv = ET.SubElement(root, "wvobject", {"fp_name": stem, "type": kind})
        for prop, val in [
            ("ElementShortName", name),
            ("ObjectShortName", name),
            ("Radix", _WCFG_RADIX.get(radix, "HEXRADIX")),
        ]:
            p = ET.SubElement(wv, "obj_property", {"name": prop})
            p.text = val

    tree.write(wcfg_path, encoding="UTF-8", xml_declaration=True)
    print(f"    wrote -> {wcfg_path}")


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def apply_layout(hw_xml: Path, config: dict[str, Any], backup_suffix: str = ".bak_setup") -> int:
    if not hw_xml.exists():
        print(f"hw.xml not found: {hw_xml}", file=sys.stderr)
        return 2

    backup = hw_xml.with_suffix(hw_xml.suffix + backup_suffix)
    shutil.copy2(hw_xml, backup)
    print(f"  backup -> {backup}")

    tree = ET.parse(hw_xml)
    root = tree.getroot()
    probe_defs = _find_probe_definitions(root)

    vio = config.get("vio", [])
    ila = config.get("ila", [])

    print("\n  configuring VIO dashboard:")
    if not vio:
        print("    (skipped, no entries)")
    for pos, entry in enumerate(vio):
        name = entry["name"]
        direction = entry.get("direction", "in").lower()
        radix = entry.get("radix")
        obj = _find_probe_object(root, name)
        if obj is None:
            print(f"    [warn] probe object not found: {name}")
            continue
        _set_gui_info_key(obj, "dashboard_1/hw_vio_1", str(pos))
        if radix:
            probe = probe_defs.get(name)
            if probe is not None:
                opt = "INPUT_VALUE_RADIX" if direction == "in" else "OUTPUT_VALUE_RADIX"
                _set_probe_option(probe, opt, radix)
        print(f"    [{pos}] {name:30s} dir={direction:3s}  radix={radix}")

    print("\n  configuring ILA waveform style (hw.xml):")
    if not ila:
        print("    (skipped, no entries)")
    for entry in ila:
        name = entry["name"]
        style = entry.get("style", "Digital")
        probe = probe_defs.get(name)
        if probe is None:
            print(f"    [warn] ILA probe not found: {name}")
            continue
        _set_probe_option(probe, "WAVEFORM_STYLE", style)
        print(f"    {name:30s} -> WAVEFORM_STYLE={style}")

    tree.write(hw_xml, encoding="UTF-8", xml_declaration=True)
    print(f"\n  wrote -> {hw_xml}")

    print("\n  configuring ILA Waveform pane (wcfg):")
    if not ila:
        print("    (skipped, no entries)")
    else:
        wcfg = hw_xml.parent / "wave" / "hw_ila_data_1" / "hw_ila_data_1.wcfg"
        for pos, entry in enumerate(ila):
            print(f"    [{pos}] {entry['name']:30s} radix={entry.get('radix','HEX')}")
        _rewrite_wcfg(wcfg, ila)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("hw_xml", type=Path,
                    help="path to project_<N>.hw/hw_1/hw.xml")
    ap.add_argument("--config", type=Path, required=True,
                    help="JSON config with 'vio' and 'ila' lists")
    ap.add_argument("--backup-suffix", default=".bak_setup")
    args = ap.parse_args()

    config = json.loads(args.config.read_text(encoding="utf-8"))
    return apply_layout(args.hw_xml, config, args.backup_suffix)


if __name__ == "__main__":
    sys.exit(main())
