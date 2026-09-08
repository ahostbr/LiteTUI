"""T530: the bash tool runs a real bash on Windows when the box has one.

Applies three edits: a `bash_exe()` resolver beside `powershell_exe()`, an argv
spawn in `tool_bash`, a derived `bash_spec()` (like `powershell_spec()`), and
two placeholders in schemas/bash.json. Idempotent: refuses to run twice.
Run with the .venv python from the repo root.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CT = ROOT / "src" / "litetui" / "plugins" / "core_tools.py"
SCHEMA = ROOT / "src" / "litetui" / "schemas" / "bash.json"

s = CT.read_text(encoding="utf-8")
if "def bash_exe(" in s:
    raise SystemExit("already applied")

OLD_RESOLVER = '''def powershell_exe() -> str | None:
    """The best available PowerShell, or None when there is none."""
    global _PS_EXE, _PS_RESOLVED
    if not _PS_RESOLVED:
        _PS_RESOLVED = True
        for cand in ("pwsh", "powershell"):
            found = shutil.which(cand)
            if found:
                _PS_EXE = found
                break
    return _PS_EXE
'''
NEW_RESOLVER = OLD_RESOLVER + '''

#: A REAL bash on Windows, resolved once like PowerShell above.
#:
#: T530 (Ryan 2026-09-08, after the async test: the 4B kept choosing the bash
#: tool for `sleep 45 && echo ... > file` and cmd.exe answered "'sleep' is not
#: recognized"). Git for Windows is on every box this runs on and ships a full
#: bash with the coreutils the model expects. Two look-alikes must NOT win:
#: System32\\bash.exe is the WSL launcher (no distro = a dialog, or a different
#: filesystem), and the WindowsApps bash.exe is the Store alias stub. So the
#: well-known Git paths are tried first, and PATH only as a fallback with those
#: two directories excluded.
_BASH_EXE: str | None = None
_BASH_RESOLVED = False
_BASH_WELL_KNOWN = (
    r"C:\\Program Files\\Git\\bin\\bash.exe",
    r"C:\\Program Files\\Git\\usr\\bin\\bash.exe",
    r"C:\\Program Files (x86)\\Git\\bin\\bash.exe",
)
_BASH_STUB_DIRS = ("\\\\system32\\\\", "\\\\windowsapps\\\\")


def bash_exe() -> str | None:
    """A usable bash on this box, or None (then the tool is cmd.exe)."""
    global _BASH_EXE, _BASH_RESOLVED
    if not _BASH_RESOLVED:
        _BASH_RESOLVED = True
        if os.name == "nt":
            for cand in _BASH_WELL_KNOWN:
                if os.path.isfile(cand):
                    _BASH_EXE = cand
                    break
            else:
                found = shutil.which("bash")
                if found and not any(d in found.lower() for d in _BASH_STUB_DIRS):
                    _BASH_EXE = found
        else:
            _BASH_EXE = shutil.which("bash")
    return _BASH_EXE
'''

OLD_BASH = '''    # be cancelled \u2014 the process existed and nothing could reach it.
    return _run_shell(command, shell=True, timeout=_timeout_arg(args))
'''
NEW_BASH = '''    # be cancelled \u2014 the process existed and nothing could reach it.
    #
    # T530: a real bash when the box has one, argv form (shell=False) so the
    # command reaches bash verbatim, with no cmd.exe pass to re-quote or
    # re-split it. The direct child is bash.exe and the work is still its
    # grandchild, so the cancel path (kill_tree over the job object) is
    # unchanged. Without a bash this stays what it was: the string via cmd.exe.
    exe = bash_exe()
    if exe is not None:
        return _run_shell([exe, "-c", command], shell=False, timeout=_timeout_arg(args))
    return _run_shell(command, shell=True, timeout=_timeout_arg(args))
'''

OLD_SPEC = '''BASH_SPEC = tool_schemas.load("bash")
'''
NEW_SPEC = '''def bash_spec() -> dict:
    """Derived like powershell_spec(): the description names the shell the
    tool will ACTUALLY run, Git bash on this box or cmd.exe where there is
    none. A description that says cmd.exe while bash runs (or the reverse)
    sends the model to the wrong tool for every Unix-shaped command (T530)."""
    exe = bash_exe()
    if exe is not None:
        return tool_schemas.load(
            "bash",
            shell_note=f"a real bash ({exe}) on Windows: sleep, grep, sed, pipes and redirection all work",
            windows_hint="The `powershell` tool is still the right one for Windows-native cmdlets, services and the registry.",
        )
    return tool_schemas.load(
        "bash",
        shell_note="cmd.exe on Windows, no Git bash found",
        windows_hint="ON WINDOWS, PREFER THE `powershell` TOOL: this one runs cmd.exe, where sleep, grep, sed and input redirection are all unavailable.",
    )


BASH_SPEC = bash_spec()
'''

for old, new in ((OLD_RESOLVER, NEW_RESOLVER), (OLD_BASH, NEW_BASH), (OLD_SPEC, NEW_SPEC)):
    if s.count(old) != 1:
        raise SystemExit(f"anchor not found exactly once:\n{old[:120]}")
    s = s.replace(old, new)
if "\nimport os\n" not in s:
    s = s.replace("\nimport shutil\n", "\nimport os\nimport shutil\n", 1)
CT.write_text(s, encoding="utf-8")

t = SCHEMA.read_text(encoding="utf-8")
OLD_HEAD = ("Execute a shell command in the current working directory (bash on Unix, cmd.exe on Windows). "
            "ON WINDOWS, PREFER THE `powershell` TOOL \\u2014 this one runs cmd.exe, where sleep, grep, sed and input redirection are all unavailable.")
if OLD_HEAD not in t:
    raise SystemExit("bash.json description head not found")
t = t.replace(OLD_HEAD, "Execute a shell command in the current working directory (bash on Unix; {shell_note}). {windows_hint}")
SCHEMA.write_text(t, encoding="utf-8")
print("applied: core_tools.py (bash_exe, tool_bash, bash_spec) + schemas/bash.json")
