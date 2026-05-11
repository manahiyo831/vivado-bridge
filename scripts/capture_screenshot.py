#!/usr/bin/env python3
"""
Vivado screenshot capture script
Captures screenshot of Vivado main window via the Win32 PrintWindow API.

Why PrintWindow + PW_RENDERFULLCONTENT (and not BitBlt of the screen):
----------------------------------------------------------------------
Modern Windows composes most top-level windows through the Desktop
Window Manager (DWM). A classic `BitBlt` from the screen DC samples
whatever pixels happen to be on the monitor at that moment, which:

  - returns a black bitmap when the window is GPU-composited and not
    actually drawn into the screen DC,
  - misses any portion that is occluded by another window,
  - silently picks up the wrong monitor or DPI scale on multi-display
    setups,
  - cannot capture a minimised window at all.

`PrintWindow` (with the `PW_RENDERFULLCONTENT = 0x2` flag introduced in
Windows 8.1) asks the target HWND to render its full client+frame
content into a bitmap we provide. The window does not need to be
visible, focused, on the primary monitor, or even on the same display
arrangement as last week. This is the single supported capture path
in this script -- there is no BitBlt fallback. If PrintWindow fails
we surface the error so the real cause gets investigated, not masked.

Limitation Note:
----------------
Vivado uses a Java/SWT GUI (Eclipse RCP). Individual panel capture
(Sources tab only, Messages tab only, ...) is not possible -- the
panels are not their own top-level HWNDs. Only the main Vivado
window can be captured.

Author: Claude Code
Date: 2026-01-24
"""

import ctypes
import ctypes.wintypes as wt
import sys
from pathlib import Path
from typing import Dict, Any, Optional


# ==========================================
# DPI awareness
# ==========================================

def _set_per_monitor_dpi_awareness() -> None:
    """Make this process per-monitor DPI aware so window rectangles and
    PrintWindow bitmaps come back at physical pixel resolution on
    high-DPI displays.

    Without this call, a window that is e.g. 1839x969 logical pixels on
    a 150% scaled monitor reports as 1226x646 to GetWindowRect, and the
    captured bitmap is shrunk + blurry. Per-monitor v2 (Win10 1703+)
    handles each monitor's scale individually, which is what we want
    for multi-display setups.

    Idempotent and best-effort: SetProcessDpiAwarenessContext fails
    cleanly with ERROR_ACCESS_DENIED if the process already set a
    different awareness (e.g. via manifest), and we leave it alone in
    that case.
    """
    user32 = ctypes.windll.user32
    # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
    DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = ctypes.c_void_p(-4)
    try:
        user32.SetProcessDpiAwarenessContext(
            DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        )
    except (AttributeError, OSError):
        # Older Windows (pre-1703) without SetProcessDpiAwarenessContext.
        # Try the per-monitor v1 fallback path; if that also isn't there,
        # leave the process at whatever awareness Python was launched
        # with -- a slightly scaled bitmap is still better than no
        # bitmap. This is platform capability, not a fallback over a
        # failed call, so it does not violate the no-fallback rule.
        try:
            shcore = ctypes.windll.shcore
            # PROCESS_PER_MONITOR_DPI_AWARE = 2
            shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            pass


# ==========================================
# Helper Functions
# ==========================================

def _query_vivado_project_name() -> Optional[str]:
    """Ask the running Vivado (via the bridge) for its open project name.

    Returns the project name string (e.g. "simple_counter_proj"), or
    None if no project is open. Raises on bridge errors -- we want
    those to surface, not be silently treated as "no project".

    Vivado main window titles look like:
        <project_name> - [<full path>.xpr] - Vivado <version>
    so a Tcl-confirmed project name uniquely identifies the right
    HWND even when other apps (VSCode, browser, ...) happen to have
    "Vivado" or the workspace folder name in their title.
    """
    # Import lazily so importing this module doesn't pull in the
    # bridge client and its own dependencies for callers that only
    # need the helper functions.
    bridge_dir = Path(__file__).resolve().parent
    if str(bridge_dir) not in sys.path:
        sys.path.insert(0, str(bridge_dir))
    from vivado_bridge_client import Client

    c = Client.connect(timeout=5.0)
    r = c.exec_tcl("get_property NAME [current_project]")
    if not r.success:
        # Most common reason: no project open (Tcl error from
        # current_project on an empty session). Treat as "no name to
        # match on" and let the caller fall back to a Vivado-only
        # title match. Other errors propagate.
        if r.error_kind == "tcl_error":
            return None
        raise RuntimeError(
            f"Bridge query for project name failed "
            f"({r.error_kind}: {r.message})"
        )
    name = (r.output or "").strip()
    return name or None


def _find_vivado_window(project_name: Optional[str]) -> Optional[int]:
    """Find the Vivado main window HWND.

    Match policy:
      - Title must contain "Vivado" (Vivado always puts that in its
        main window's title).
      - If `project_name` is given (and non-empty), the title must also
        contain that exact string. Vivado puts the open project name
        at the start of the title, so this excludes editors / browsers
        whose title happens to also contain "Vivado".
      - When multiple HWNDs match (rare; e.g. an "About Vivado" dialog
        is open), prefer the one with the largest area -- the main
        window is much larger than any modal child.

    Returns the HWND or None if no match.
    """
    import win32gui

    candidates: list[tuple[int, int]] = []  # (area, hwnd)

    def callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        title = win32gui.GetWindowText(hwnd)
        if "Vivado" not in title:
            return True
        if project_name and project_name not in title:
            return True
        # Compute area for tie-break.
        try:
            l, t, r, b = win32gui.GetWindowRect(hwnd)
            area = max(0, r - l) * max(0, b - t)
        except Exception:
            area = 0
        candidates.append((area, hwnd))
        return True

    win32gui.EnumWindows(callback, None)
    if not candidates:
        return None
    candidates.sort(reverse=True)  # largest area first
    return candidates[0][1]


def _capture_window_by_hwnd(hwnd: int, output_path: Path) -> Dict[str, Any]:
    """
    Capture window by HWND using PrintWindow + PW_RENDERFULLCONTENT.

    Works for:
      - DWM-composited windows (modern default; BitBlt returns black for these)
      - Windows on a non-primary monitor
      - Windows that are partially or fully occluded by other windows
      - Per-monitor DPI scaled displays

    Args:
        hwnd: Window handle to capture
        output_path: Where to save screenshot

    Returns:
        Dictionary with success, image, path, width, height

    Raises:
        RuntimeError: If PrintWindow fails (rather than silently writing
            a black image). The caller can act on the error.
    """
    import win32gui
    import win32ui
    from PIL import Image

    PW_RENDERFULLCONTENT = 0x00000002

    user32 = ctypes.windll.user32
    user32.PrintWindow.argtypes = [wt.HWND, wt.HDC, wt.UINT]
    user32.PrintWindow.restype = wt.BOOL

    bitmap = None
    save_dc = None
    mfc_dc = None
    hwnd_dc = None
    try:
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            raise RuntimeError(
                f"Vivado window has non-positive size {width}x{height}; "
                f"is it minimised in a way GetWindowRect can't measure?"
            )

        hwnd_dc = win32gui.GetWindowDC(hwnd)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()

        bitmap = win32ui.CreateBitmap()
        bitmap.CreateCompatibleBitmap(mfc_dc, width, height)
        save_dc.SelectObject(bitmap)

        # PrintWindow asks the target HWND to render itself into our DC.
        # PW_RENDERFULLCONTENT (Win 8.1+) makes DWM-composited windows
        # render their full GPU-composed content -- without it, modern
        # apps frequently produce a black or partial bitmap.
        ok = user32.PrintWindow(hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        if not ok:
            err = ctypes.get_last_error()
            raise RuntimeError(
                f"PrintWindow failed (GetLastError={err}). The window may "
                f"have been destroyed mid-capture, or refused to render "
                f"(some kernel-mode dialogs do)."
            )

        bmp_info = bitmap.GetInfo()
        bmp_bits = bitmap.GetBitmapBits(True)
        img = Image.frombuffer(
            'RGB',
            (bmp_info['bmWidth'], bmp_info['bmHeight']),
            bmp_bits, 'raw', 'BGRX', 0, 1
        )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        img.save(output_path)

        return {
            'success': True,
            'image': img,
            'path': str(output_path),
            'width': width,
            'height': height,
        }

    finally:
        # Cleanup GDI resources in reverse order of acquisition. Each
        # is wrapped individually so a failure in one doesn't leak the
        # others. We log nothing here -- if cleanup fails the process
        # ending will reclaim the handles anyway.
        if bitmap is not None:
            try:
                win32gui.DeleteObject(bitmap.GetHandle())
            except Exception:
                pass
        if save_dc is not None:
            try:
                save_dc.DeleteDC()
            except Exception:
                pass
        if mfc_dc is not None:
            try:
                mfc_dc.DeleteDC()
            except Exception:
                pass
        if hwnd_dc is not None:
            try:
                win32gui.ReleaseDC(hwnd, hwnd_dc)
            except Exception:
                pass


# ==========================================
# Main Capture Function
# ==========================================

def capture_screenshot(output_path: Optional[Path] = None,
                      project_root: Optional[Path] = None) -> Dict[str, Any]:
    """
    Capture screenshot of Vivado main window

    Args:
        output_path: Optional custom path. When None, the screenshot
                    is saved to <bridge_dir>/screenshots/vivado_screenshot.png
                    -- a single rolling file overwritten on each call.
                    Pass an explicit path only when you want to keep
                    a specific shot (e.g. before/after evidence).
        project_root: Kept for backwards compatibility. Ignored when
                    `output_path` is also given. When `output_path` is
                    None and this is set, the default path becomes
                    `<project_root>/screenshots/vivado_screenshot.png`.
                    Pass None (the default) to use the bridge directory.

    Returns:
        Dictionary with keys:
        - success: bool
        - image: PIL.Image
        - path: str
        - width: int
        - height: int
        - message: str

    Raises:
        RuntimeError: If Vivado window not found
    """
    # Default output: alongside the script, not relative to cwd. The
    # script lives at <bridge>/scripts/capture_screenshot.py, so the
    # bridge dir is one level up. This keeps the rolling file in a
    # predictable location regardless of where the user runs from.
    if output_path is None:
        if project_root is None:
            project_root = Path(__file__).resolve().parent.parent
        output_path = project_root / 'screenshots' / 'vivado_screenshot.png'

    # Per-monitor DPI awareness must be set before GetWindowRect /
    # PrintWindow, otherwise the window's rectangle and the captured
    # bitmap come back in pre-scaled (logical) pixels on a high-DPI
    # monitor. Idempotent: safe to call every invocation.
    _set_per_monitor_dpi_awareness()

    # Ask Vivado for its open project name so we can match its window
    # title precisely. This avoids picking up an editor / browser /
    # other tool whose title happens to contain "Vivado" (e.g. an
    # IDE workspace named "vivado-skills-dev"). When no project is
    # open, fall through to a plain "Vivado" title match -- a stale
    # editor would still be a misidentification risk in that case,
    # but it's the only signal we have left, and Vivado without a
    # project is rare in practice.
    project_name = _query_vivado_project_name()

    # Find Vivado window
    vivado_hwnd = _find_vivado_window(project_name)
    if vivado_hwnd is None:
        if project_name:
            raise RuntimeError(
                f"No top-level window found with title containing "
                f"both 'Vivado' and project name '{project_name}'. "
                f"Is Vivado minimised to the system tray, or running on "
                f"a different desktop?"
            )
        raise RuntimeError(
            "No top-level window found with 'Vivado' in its title, "
            "and no Vivado project is open to disambiguate. Is Vivado "
            "running?"
        )

    # Capture
    result = _capture_window_by_hwnd(vivado_hwnd, output_path)
    matched = f"project='{project_name}'" if project_name else "no project open"
    result['message'] = (
        f"Captured Vivado main window ({matched}) to {result['path']}"
    )
    result['project_name'] = project_name

    return result


# ==========================================
# CLI Interface
# ==========================================

def main():
    """
    Capture screenshot of Vivado main window

    Usage:
        python scripts/capture_screenshot.py [output_path]

    Args:
        output_path: Optional custom output path
                    Default: screenshots/vivado_screenshot.png

    Examples:
        python scripts/capture_screenshot.py
        python scripts/capture_screenshot.py "results/my_screenshot.png"
    """
    # Parse arguments
    output_path = Path(sys.argv[1]) if len(sys.argv) > 1 else None

    bridge_dir = Path(__file__).resolve().parent.parent

    print("="*60)
    print("Vivado Screenshot Capture")
    print("="*60)
    if output_path:
        print(f"Output path: {output_path} (caller-specified, kept)")
    else:
        default_path = bridge_dir / 'screenshots' / 'vivado_screenshot.png'
        print(f"Output path: {default_path} (default, overwritten)")
    print()
    print("Note: Captures main window only (individual panels not supported)")
    print()

    # Capture screenshot
    print("[1/2] Finding Vivado window...")
    try:
        result = capture_screenshot(output_path=output_path)

        if result['success']:
            print(f"✓ Vivado window found")
            print()
            print(f"[2/2] Capturing screenshot...")
            print(f"✓ Screenshot captured successfully!")
            print(f"  Size: {result['width']}x{result['height']} pixels")
            print(f"  Saved to: {result['path']}")
        else:
            print(f"✗ Screenshot failed: {result.get('message', 'Unknown error')}")
            return False

    except RuntimeError as e:
        print(f"✗ Capture failed: {e}")
        print()
        print("Troubleshooting:")
        print("  1. Ensure Vivado GUI is running")
        print("  2. The window title must contain 'Vivado'")
        return False
    except Exception as e:
        print(f"✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        return False

    # Summary
    print()
    print("="*60)
    print("✓ Screenshot capture complete!")
    print("="*60)
    print(f"\nScreenshot saved: {Path(result['path']).absolute()}")
    print(f"Image size: {result['width']}x{result['height']} pixels")
    print(f"\nNext time you capture, it will overwrite this file.")

    return True


if __name__ == "__main__":
    try:
        success = main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n✗ Interrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
