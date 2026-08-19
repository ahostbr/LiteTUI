"""Sentinel Desktop Control — Win32 mouse/keyboard/window automation via PowerShell.

Ported from Kuroryuu k_pccontrol. No armed flag — Sentinel is trusted.
Usage from Claude Code Bash tool:
    python ~/.claude/skills/sentinel/pccontrol.py click 500 300
    python ~/.claude/skills/sentinel/pccontrol.py doubleclick 500 300
    python ~/.claude/skills/sentinel/pccontrol.py rightclick 500 300
    python ~/.claude/skills/sentinel/pccontrol.py type "Hello World"
    python ~/.claude/skills/sentinel/pccontrol.py keypress Enter
    python ~/.claude/skills/sentinel/pccontrol.py keypress ctrl+c
    python ~/.claude/skills/sentinel/pccontrol.py launch notepad.exe
    python ~/.claude/skills/sentinel/pccontrol.py windows
    python ~/.claude/skills/sentinel/pccontrol.py status
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Optional


def _run_ps(script: str, timeout: int = 10) -> subprocess.CompletedProcess:
    # encoding/errors are load-bearing: window titles routinely contain characters
    # outside the console's default cp1252, and without these the reader thread dies
    # with UnicodeDecodeError, leaving stdout as None (which then blows up on .strip()).
    return subprocess.run(
        ["powershell", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, timeout=timeout,
        encoding="utf-8", errors="replace",
    )


def _mouse_ps(x: int, y: int, mon: Optional[int], down: str, up: str, double: bool = False) -> str:
    """Build the PowerShell for a mouse action. When mon is not None, (x,y) are
    MONITOR-LOCAL pixels (same Screen.AllScreens index as screenshot.ps1) and the
    monitor's desktop offset is added at runtime — so coords read straight off a
    per-monitor screenshot need no manual math."""
    m = mon if mon is not None else -1
    extra = (f"\nStart-Sleep -Milliseconds 50\n"
             f"[M]::mouse_event({down},0,0,0,0); [M]::mouse_event({up},0,0,0,0)") if double else ""
    return f'''
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System; using System.Runtime.InteropServices;
public class M {{
    [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
    [DllImport("user32.dll")] public static extern void mouse_event(int f, int dx, int dy, int d, int e);
}}
"@
$gx = {x}; $gy = {y}
if ({m} -ge 0) {{ $s = [System.Windows.Forms.Screen]::AllScreens; if ({m} -lt $s.Count) {{ $b = $s[{m}].Bounds; $gx = $b.X + {x}; $gy = $b.Y + {y} }} }}
[M]::SetCursorPos($gx, $gy); Start-Sleep -Milliseconds 50
[M]::mouse_event({down},0,0,0,0); [M]::mouse_event({up},0,0,0,0){extra}
'''


def _coord_tag(x: int, y: int, mon: Optional[int]) -> str:
    return f"(mon{mon} {x},{y})" if mon is not None else f"({x},{y})"


def click(x: int, y: int, mon: Optional[int] = None, show: bool = False,
          size: Optional[int] = None, color: str = "") -> str:
    if show:
        marker(x, y, mon=mon, ms=1400, label="click", size=size, color=color)
    r = _run_ps(_mouse_ps(x, y, mon, "0x02", "0x04"))
    return f"clicked {_coord_tag(x, y, mon)}" if r.returncode == 0 else f"ERROR: {r.stderr}"


def doubleclick(x: int, y: int, mon: Optional[int] = None, show: bool = False,
                size: Optional[int] = None, color: str = "") -> str:
    if show:
        marker(x, y, mon=mon, ms=1400, label="dblclick", size=size, color=color)
    r = _run_ps(_mouse_ps(x, y, mon, "0x02", "0x04", double=True))
    return f"double-clicked {_coord_tag(x, y, mon)}" if r.returncode == 0 else f"ERROR: {r.stderr}"


def rightclick(x: int, y: int, mon: Optional[int] = None, show: bool = False,
               size: Optional[int] = None, color: str = "") -> str:
    if show:
        marker(x, y, mon=mon, ms=1400, label="rclick", size=size, color=color)
    r = _run_ps(_mouse_ps(x, y, mon, "0x08", "0x10"))
    return f"right-clicked {_coord_tag(x, y, mon)}" if r.returncode == 0 else f"ERROR: {r.stderr}"


def marker(x: int, y: int, mon: Optional[int] = None, ms: int = 2500, label: str = "",
           size: Optional[int] = None, color: str = "") -> str:
    """Flash a topmost, click-through ring+crosshair at a screen point (auto-
    closes after ms). Coords are monitor-local when mon is set. Because GDI
    screenshots omit the cursor, this is how a click target is made visible — to
    the user AND to a screenshot so the agent can verify-then-click.
    size = ring diameter in px (default 110). color = name (red/lime/cyan/yellow/
    orange/white/blue...) or #RRGGBB hex — pick a contrasting one for busy scenes."""
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "marker_overlay.ps1")
    cmd = ["powershell", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
           "-File", script, "-Mon", str(mon if mon is not None else -1),
           "-X", str(x), "-Y", str(y), "-Ms", str(ms)]
    if label:
        cmd += ["-Label", label]
    if size:
        cmd += ["-Size", str(size)]
    if color:
        cmd += ["-Color", color]
    try:
        subprocess.Popen(cmd, creationflags=0x08000000,  # CREATE_NO_WINDOW
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # noqa
        return f"ERROR: {e}"
    extra = (f" size={size}" if size else "") + (f" {color}" if color else "")
    return f"marker shown {_coord_tag(x, y, mon)} for {ms}ms{extra}"


def type_text(text: str) -> str:
    escaped = text
    for char in ['+', '^', '%', '~', '(', ')', '{', '}', '[', ']']:
        escaped = escaped.replace(char, '{' + char + '}')
    escaped = escaped.replace('"', '`"')
    ps = f'''
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.SendKeys]::SendWait("{escaped}")
'''
    r = _run_ps(ps)
    return f"typed {len(text)} chars" if r.returncode == 0 else f"ERROR: {r.stderr}"


KEY_MAP = {
    "enter": "{ENTER}", "return": "{ENTER}", "tab": "{TAB}",
    "escape": "{ESC}", "esc": "{ESC}", "backspace": "{BACKSPACE}",
    "delete": "{DELETE}", "del": "{DELETE}", "insert": "{INSERT}",
    "home": "{HOME}", "end": "{END}",
    "pageup": "{PGUP}", "pgup": "{PGUP}", "pagedown": "{PGDN}", "pgdn": "{PGDN}",
    "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
    "space": " ",
    "f1": "{F1}", "f2": "{F2}", "f3": "{F3}", "f4": "{F4}",
    "f5": "{F5}", "f6": "{F6}", "f7": "{F7}", "f8": "{F8}",
    "f9": "{F9}", "f10": "{F10}", "f11": "{F11}", "f12": "{F12}",
    "ctrl+a": "^a", "ctrl+c": "^c", "ctrl+v": "^v", "ctrl+x": "^x",
    "ctrl+z": "^z", "ctrl+y": "^y", "ctrl+s": "^s",
    "alt+f4": "%{F4}", "alt+tab": "%{TAB}",
}


def keypress(key: str) -> str:
    sendkey = KEY_MAP.get(key.lower().strip())
    if not sendkey:
        sendkey = key if len(key) == 1 else None
    if not sendkey:
        return f"ERROR: unknown key '{key}'. Available: {', '.join(sorted(KEY_MAP.keys()))}"
    ps = f'''
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.SendKeys]::SendWait("{sendkey}")
'''
    r = _run_ps(ps)
    return f"pressed {key}" if r.returncode == 0 else f"ERROR: {r.stderr}"


def launch(path: str, attached: bool = False) -> str:
    """Launch an app fully DETACHED by default.

    Electron/Node apps write console.* to stdout. If the child inherits our handles
    that output floods the caller's terminal — LiteSuite did exactly this, dumping its
    main-process logs into Ryan's session. DETACHED_PROCESS + DEVNULL keeps the
    launched app's output out of the terminal and lets it outlive this process.
    Pass attached=True only when you actually want the child's output.
    """
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200

    if attached:
        escaped = path.replace("'", "''")
        r = _run_ps(f"Start-Process -FilePath '{escaped}'", timeout=30)
        return f"launched {path} (attached)" if r.returncode == 0 else f"ERROR: {r.stderr}"

    try:
        subprocess.Popen(
            [path],
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        return f"launched {path} (detached)"
    except OSError as e:
        return f"ERROR: {e}"


def activate(target: str) -> str:
    """Bring a window reliably to the foreground (beats the Windows foreground
    lock via AttachThreadInput). target = PID (digits) or window-title substring.
    Call this BEFORE type/paste so keystrokes land in the right window."""
    t = target.replace('"', '`"')
    ps = f'''
Add-Type @"
using System; using System.Runtime.InteropServices;
public class W {{
  [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
  [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int n);
  [DllImport("user32.dll")] public static extern bool BringWindowToTop(IntPtr h);
  [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
  [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint pid);
  [DllImport("user32.dll")] public static extern bool AttachThreadInput(uint a, uint b, bool f);
  [DllImport("kernel32.dll")] public static extern uint GetCurrentThreadId();
}}
"@
$t = "{t}"
$p = $null
if ($t -match '^[0-9]+$') {{ $p = Get-Process -Id ([int]$t) -ErrorAction SilentlyContinue }}
if (-not $p) {{ $p = Get-Process | Where-Object {{ $_.MainWindowTitle -like "*$t*" -and $_.MainWindowHandle -ne 0 }} | Select-Object -First 1 }}
if (-not $p) {{ Write-Output "ERROR: no window matches '$t'"; exit 1 }}
$h = $p.MainWindowHandle
$dummy = [uint32]0
$cur = [W]::GetCurrentThreadId()
$fg = [W]::GetForegroundWindow()
$fgT = [W]::GetWindowThreadProcessId($fg, [ref]$dummy)
[W]::AttachThreadInput($cur, $fgT, $true) | Out-Null
[W]::ShowWindow($h, 9) | Out-Null
[W]::BringWindowToTop($h) | Out-Null
[W]::SetForegroundWindow($h) | Out-Null
[W]::AttachThreadInput($cur, $fgT, $false) | Out-Null
Write-Output ("activated " + $p.ProcessName + " (" + $p.Id + ")")
'''
    r = _run_ps(ps)
    return r.stdout.strip() or (f"ERROR: {r.stderr}" if r.returncode else "activated")


def settext(text: str) -> str:
    """Set the CURRENTLY FOCUSED control's value directly via UI Automation
    ValuePattern. No keystroke simulation -> immune to SendKeys focus races and
    RDP. Click/focus the target field first, then call this. Falls back with a
    clear error if the focused control has no ValuePattern (use paste then)."""
    esc = text.replace('`', '``').replace('"', '`"')
    ps = f'''
Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$el = [System.Windows.Automation.AutomationElement]::FocusedElement
if ($null -eq $el) {{ Write-Output "ERROR: no focused element"; exit 1 }}
$pat = $null
$ok = $el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, [ref]$pat)
if ($ok) {{ $pat.SetValue("{esc}"); Write-Output ("setvalue ok -> " + $el.Current.Name) }}
else {{ Write-Output ("ERROR: focused control has no ValuePattern: " + $el.Current.ControlType.ProgrammaticName); exit 2 }}
'''
    r = _run_ps(ps)
    return r.stdout.strip() or (f"ERROR: {r.stderr}" if r.returncode else "setvalue done")


def paste(text: str) -> str:
    """Set the clipboard to text then send Ctrl+V to the focused window. Robust
    for controls without ValuePattern. Focus/click the target field first."""
    esc = text.replace('"', '`"')
    ps = f'''
Add-Type -AssemblyName System.Windows.Forms
Set-Clipboard -Value "{esc}"
Start-Sleep -Milliseconds 120
[System.Windows.Forms.SendKeys]::SendWait("^v")
'''
    r = _run_ps(ps)
    return f"pasted {len(text)} chars" if r.returncode == 0 else f"ERROR: {r.stderr}"


def get_windows() -> str:
    ps = '''
Get-Process | Where-Object {$_.MainWindowTitle -ne ""} | ForEach-Object {
    [PSCustomObject]@{
        Process = $_.ProcessName
        Title = $_.MainWindowTitle
        PID = $_.Id
        Handle = $_.MainWindowHandle
    }
} | ConvertTo-Json -Compress
'''
    r = _run_ps(ps)
    if r.returncode != 0:
        return f"ERROR: {r.stderr}"
    out = (r.stdout or "").strip()
    if not out:
        return "No windows found"
    windows = json.loads(out)
    if isinstance(windows, dict):
        windows = [windows]
    lines = [f"  {w.get('Process','?'):20s} | {w.get('Title','')[:60]}" for w in windows]
    return f"{len(windows)} windows:\n" + "\n".join(lines)


def status() -> str:
    try:
        r = _run_ps('Add-Type -AssemblyName System.Windows.Forms; Write-Output "ready"', timeout=5)
        if r.returncode == 0 and "ready" in r.stdout:
            return "OK: PowerShell + System.Windows.Forms ready"
        return f"ERROR: PowerShell test failed: {r.stderr}"
    except Exception as e:
        return f"ERROR: {e}"


def _parse_opts(args: list) -> tuple:
    """Split out flags (--mon N, --ms N, --label TEXT, --show) from positionals."""
    opts = {"mon": None, "ms": None, "label": "", "show": False, "size": None, "color": ""}
    pos = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--mon" and i + 1 < len(args):
            opts["mon"] = int(args[i + 1]); i += 2
        elif a == "--ms" and i + 1 < len(args):
            opts["ms"] = int(args[i + 1]); i += 2
        elif a == "--label" and i + 1 < len(args):
            opts["label"] = args[i + 1]; i += 2
        elif a == "--size" and i + 1 < len(args):
            opts["size"] = int(args[i + 1]); i += 2
        elif a == "--color" and i + 1 < len(args):
            opts["color"] = args[i + 1]; i += 2
        elif a == "--show":
            opts["show"] = True; i += 1
        else:
            pos.append(a); i += 1
    return pos, opts


USAGE = """Usage: pccontrol.py <action> [args...] [flags]
Actions:
  click|doubleclick|rightclick <x> <y> [--mon N] [--show] [--size N] [--color C]
  marker <x> <y> [--mon N] [--ms N] [--label TEXT] [--size N] [--color C]
  type <text> | paste <text> | settext <text>
  keypress <key> | activate <pid|title> | launch <path> | windows | status
Flags:
  --mon N    (x,y) are pixels on monitor N (same index as screenshot.ps1) — no offset math
  --show     flash the marker overlay at the click point
  --ms N     marker lifetime in ms (default 2500)
  --label T  text under the marker
  --size N   marker ring diameter in px (default 110)
  --color C  marker color: name (red/lime/cyan/yellow/orange/white/blue) or #RRGGBB"""


def main():
    raw = sys.argv[1:]
    if not raw:
        print(USAGE)
        sys.exit(1)

    action = raw[0].lower()
    pos, o = _parse_opts(raw[1:])
    mon, show = o["mon"], o["show"]

    if action == "click" and len(pos) >= 2:
        print(click(int(pos[0]), int(pos[1]), mon=mon, show=show, size=o["size"], color=o["color"]))
    elif action == "doubleclick" and len(pos) >= 2:
        print(doubleclick(int(pos[0]), int(pos[1]), mon=mon, show=show, size=o["size"], color=o["color"]))
    elif action == "rightclick" and len(pos) >= 2:
        print(rightclick(int(pos[0]), int(pos[1]), mon=mon, show=show, size=o["size"], color=o["color"]))
    elif action == "marker" and len(pos) >= 2:
        print(marker(int(pos[0]), int(pos[1]), mon=mon, ms=(o["ms"] or 2500),
                     label=o["label"], size=o["size"], color=o["color"]))
    elif action == "type" and len(pos) >= 1:
        print(type_text(" ".join(pos)))
    elif action == "keypress" and len(pos) >= 1:
        print(keypress(pos[0]))
    elif action == "launch" and len(pos) >= 1:
        print(launch(pos[0], attached="--attached" in sys.argv))
    elif action == "activate" and len(pos) >= 1:
        print(activate(" ".join(pos)))
    elif action == "settext" and len(pos) >= 1:
        print(settext(" ".join(pos)))
    elif action == "paste" and len(pos) >= 1:
        print(paste(" ".join(pos)))
    elif action == "windows":
        print(get_windows())
    elif action == "status":
        print(status())
    else:
        print(f"Unknown action or missing args: {action}\n\n{USAGE}")
        sys.exit(1)


if __name__ == "__main__":
    main()
