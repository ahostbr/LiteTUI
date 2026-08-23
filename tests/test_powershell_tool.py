"""A first-class PowerShell tool, preferred on Windows. bash stays as backup.

Ryan: "add a new PS tool and point the agent to it if OS = windows first. keep
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
from plugins import core_tools as ct

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
    """Exits 0 without the $Error.Count check — a silent success on an error."""
    out = _run("Get-Item C:\\nope\\nope\\nope")
    assert "code 1" in out, f"a failing cmdlet reported success: {out!r}"


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
    assert "powershell" in bash and "prefer" in bash, (
        "bash does not point Windows users at the PowerShell tool"
    )


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
