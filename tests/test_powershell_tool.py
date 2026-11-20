"""A first-class PowerShell tool, preferred on Windows. bash stays as backup.

the user: "add a new PS tool and point the agent to it if OS = windows first. keep
bash for unix systems or as backup."

The cost of not having one was visible in a real turn: the model tried
`sleep 30` (not a cmd builtin), then `timeout /t 30 /nobreak` (input
redirection unsupported), and only then reached PowerShell — three attempts and
~30 seconds burned because bash on Windows is cmd.exe.

EVERY CLAUSE OF THE WRAPPER WAS MEASURED. Plain `pwsh -Command` gets two things
wrong, and both would have passed a casual test:

  1. It COLLAPSES NATIVE EXIT CODES TO 1 — `cmd /c exit 3` and python's
     sys.exit(4) both surface as 1, destroying every distinction a caller reads
     from an exit code.
  2. A failing cmdlet exits 0, because a non-terminating error is not a failed
     exit.

These tests run the real shell rather than asserting on the wrapper string: the
wrapper is only interesting insofar as the exit codes come out right.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui.plugins import core_tools as ct

pytestmark = pytest.mark.skipif(
    ct.powershell_exe() is None, reason="no PowerShell on PATH"
)


def _run(command: str, timeout: int = 60) -> str:
    return ct.tool_powershell({"command": command, "timeout": timeout})


def test_a_native_exit_code_survives_intact() -> None:
    """THE regression a naive swap would have shipped."""
    out = _run("cmd /c exit 3")
    assert "code 3" in out, f"native exit code was not preserved: {out!r}"


def test_a_python_exit_code_survives_intact() -> None:
    out = _run('python -c "import sys; sys.exit(4)"')
    assert "code 4" in out, f"expected 4, got: {out!r}"


def test_an_explicit_exit_survives() -> None:
    assert "code 5" in _run("exit 5")


def test_a_failing_cmdlet_reports_failure() -> None:
    """Plain `pwsh -Command` exits 0 here: a silent success on an error. The wrapper's
    `$ErrorActionPreference='Stop'` is what makes it exit 1."""
    out = _run("Get-Item C:\\nope\\nope\\nope")
    assert "code 1" in out, f"a failing cmdlet reported success: {out!r}"
    assert "Cannot find path" in out, f"the error text must reach the model: {out!r}"


def test_an_uncaught_error_stops_the_rest_and_fails() -> None:
    """T1026: the statement after a failed one must not run."""
    out = _run("Get-Item C:\\nope\\nope\\nope; Write-Output AFTER-THE-ERROR")
    assert "code 1" in out and "AFTER-THE-ERROR" not in out, out
    assert "code 1" in _run("throw 'boom'")


# ── an error the command HANDLED is not a failed command (RustSuite, 2026-11-20) ──
#
# 🔴 The wrapper used to compare `$Error.Count` before and after. `$Error` also records
# errors that were caught, and under 'Stop' an uncaught one never reaches that check, so it
# could only ever fire for handled errors: a command that dealt with its own error and whose
# last native command exited 0 was reported as "exited with code 1".

def test_an_error_caught_by_try_catch_is_success(tmp_path) -> None:
    """The reported case: New-Item on a lock file that exists (IOException 0x80070050)."""
    lock = tmp_path / "held.lock"
    lock.write_text("", encoding="utf-8")
    out = _run(f"try {{ New-Item -ItemType File -Path '{lock}' -ErrorAction Stop | Out-Null }} "
               "catch { Write-Output 'lock is held, handled' }; cmd /c exit 0")
    assert "lock is held, handled" in out
    assert "exited with code" not in out, f"a caught error marked the command failed: {out!r}"


@pytest.mark.parametrize("command", [
    "Get-Item C:\\nope\\nope -ErrorAction SilentlyContinue; Write-Output went-on",
    "try { [int]::Parse('x') } catch { Write-Output went-on }",
    "try { throw 'x' } catch { Write-Output went-on }",
])
def test_a_silenced_or_caught_error_is_success(command) -> None:
    out = _run(command)
    assert "went-on" in out and "exited with code" not in out, out


def test_a_native_failure_after_a_caught_error_still_fails_with_its_own_code() -> None:
    out = _run("try { throw 'x' } catch { Write-Output handled }; cmd /c exit 3")
    assert "handled" in out and "code 3" in out, out


def test_a_command_that_asks_to_go_on_past_an_error_is_not_failed_by_the_tool() -> None:
    """The ceiling of this fix, pinned so it is a decision and not an accident: the author
    said Continue, PowerShell goes on, the error text is still shown, the exit is 0."""
    out = _run("Write-Error 'reported and passed' -ErrorAction Continue; Write-Output AFTER")
    assert "AFTER" in out and "reported and passed" in out
    assert "exited with code" not in out, out


def test_success_is_success() -> None:
    out = _run("Write-Output hello-from-powershell")
    assert "hello-from-powershell" in out
    assert "exited with code" not in out, f"a successful command reported an exit: {out!r}"


def test_writing_to_stderr_is_not_by_itself_a_failure() -> None:
    """stderr is not the same fact as a non-zero exit, and conflating them
    would make every noisy-but-fine command look broken."""
    out = _run('Write-Output fine; cmd /c "echo noise 1>&2"')
    assert "exited with code" not in out, f"stderr alone was treated as failure: {out!r}"


def test_output_carries_no_ansi_escapes() -> None:
    """Killed at the source with OutputRendering rather than stripped later."""
    out = _run("Get-Item C:\\nope\\nope")
    assert "\x1b[" not in out, "ANSI escapes reached the model"


def test_powershell_is_actually_powershell() -> None:
    out = _run("$PSVersionTable.PSVersion.Major")
    assert any(ch.isdigit() for ch in out), f"no version came back: {out!r}"


def test_a_missing_command_is_an_error_not_a_crash() -> None:
    assert ct.tool_powershell({}).startswith("[error]")
    assert ct.tool_powershell({"command": "   "}).startswith("[error]")


# ── the pointing, which is the other half of the ask ────────────────────────
def test_the_spec_names_the_interpreter_it_actually_found() -> None:
    """Derived, not hardcoded — a fact no code reads is a fact that rots."""
    desc = ct.powershell_spec()["function"]["description"]
    exe = ct.powershell_exe()
    assert exe is not None and exe in desc, f"the spec does not name {exe!r}"


def test_both_descriptions_point_windows_at_powershell() -> None:
    ps = ct.powershell_spec()["function"]["description"].lower()
    bash = ct.BASH_SPEC["function"]["description"].lower()
    assert "prefer" in ps and "bash" in ps, "the PS tool does not claim precedence"
    # Both real Git bash and the cmd.exe fallback point at PowerShell.
    assert "powershell" in bash, "bash does not mention the PowerShell tool"
    if ct.bash_exe() is None:
        assert "prefer" in bash, "bash (cmd.exe) does not point Windows users at PowerShell"
    else:
        assert ct.bash_exe().lower() in bash, "bash does not name the shell it runs"


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell is only offered on Windows")
def test_powershell_is_offered_before_bash() -> None:
    """Registration order is the order the model sees them in."""
    seen: list[str] = []

    class _Ctx:
        app = None

        def tool(self, spec, fn, **_metadata):
            seen.append(spec["function"]["name"])

        def command(self, *a, **k):
            pass

        def palette_row(self, *a, **k):
            pass

        def prompt_section(self, *a, **k):
            pass

    ct._register(_Ctx())
    assert "powershell" in seen, "the PowerShell tool was never registered"
    assert seen.index("powershell") < seen.index("bash"), (
        "bash is offered first, so the model reaches for cmd.exe before PowerShell"
    )
