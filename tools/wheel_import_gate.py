"""T135 gate — build the wheel, install it in a scratch venv, run the HEAVY import.

The bug this exists for: litetui 0.22.0 crashed on first launch from an
installed wheel because its data (tools/*.json, prompts/*.md) was read through
repo-relative paths that do not exist inside site-packages — and every
pre-publish proof missed it, because ``litetui --version`` takes a fast path in
cli.py that skips exactly the import where the crash lives. A probe engineered
around the heavy path cannot certify the heavy path.

So this gate does what an install actually is:

  1. ``uv build --wheel`` and assert every schema/prompt file on disk is IN the
     zip — a missing package-data entry fails here, not on someone's machine.
  2. Create a scratch venv and pip-install ONLY that wheel (plus its declared
     dependencies), exactly as PyPI would deliver it.
  3. In that venv: ``import litetui.app`` — the heavy import that died in
     0.22.0 — then load every schema through the app's own seams, read every
     prompt file the first turn needs, validate tool-denied.md, and compare
     ``litetui --version`` against src/litelitui/version.py.

Stdlib only; needs ``uv`` on PATH (CI has it; so does this repo's dev box).
Exits non-zero on any failure — wire it into CI as a blocking step.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PKG_DIR = REPO / "src" / "litetui"


def _fail(msg: str) -> None:
    print(f"WHEEL GATE FAILED: {msg}", file=sys.stderr)
    raise SystemExit(1)


def _run(cmd: list[str], *, timeout: int, cwd: Path | None = None) -> subprocess.CompletedProcess:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    try:
        return subprocess.run(
            [str(c) for c in cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
        )
    except subprocess.TimeoutExpired:
        _fail(f"timed out after {timeout}s: {' '.join(str(c) for c in cmd)}")


def build_wheel() -> Path:
    uv = shutil.which("uv")
    if uv is None:
        _fail("`uv` not found on PATH — the gate builds with it (CI has it)")
    # Drop stale wheels so a failed rebuild cannot certify an old artifact.
    for old in (REPO / "dist").glob("litetui-*.whl"):
        old.unlink()
    r = _run([uv, "build", "--wheel"], timeout=600, cwd=REPO)
    if r.returncode != 0:
        print(r.stdout, file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        _fail("uv build --wheel failed")
    wheels = sorted((REPO / "dist").glob("litetui-*.whl"))
    if not wheels:
        _fail("build reported success but produced no wheel in dist/")
    return wheels[-1]


def check_zip(wheel: Path) -> None:
    """Every data file on disk must be inside the zip — package-data drift."""
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())
    missing = []
    for folder, suffix in (("schemas", ".json"), ("prompts", ".md")):
        src = PKG_DIR / folder
        if not src.is_dir():
            _fail(f"{src} is missing from the source tree — nothing to ship")
        for f in sorted(src.glob(f"*{suffix}")):
            entry = f"litetui/{folder}/{f.name}"
            if entry not in names:
                missing.append(entry)
    if missing:
        _fail(
            "these files are on disk but NOT in the wheel — package-data is "
            f"not shipping them: {missing}"
        )
    print(f"zip OK: all schemas+prompts present ({len(names)} entries total)")


def make_venv(wheel: Path, workdir: Path) -> Path:
    venv = workdir / "venv"
    r = _run([sys.executable, "-m", "venv", str(venv)], timeout=300)
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        _fail("could not create the scratch venv")
    venv_python = (
        venv / "Scripts" / "python.exe" if os_name() == "nt" else venv / "bin" / "python"
    )
    r = _run(
        [venv_python, "-m", "pip", "install", "--no-input", str(wheel)],
        timeout=900,
    )
    if r.returncode != 0:
        print(r.stdout[-4000:], file=sys.stderr)
        print(r.stderr[-4000:], file=sys.stderr)
        _fail("pip install of the wheel failed")
    return venv_python


def os_name() -> str:
    import os

    return os.name


IN_VENV_CHECKS = r'''
# Runs INSIDE the scratch venv, against ONLY what the wheel installed.
from litetui import app  # noqa: F401  <- THE heavy import; died in 0.22.0
from litetui import paths, tool_schemas, textfmt
from litetui.textfmt import validate_tool_denied

names = sorted(tool_schemas.available())
assert len(names) >= 15, f"only {len(names)} schemas visible in the install: {names}"
for n in names:
    spec = tool_schemas.load(n)
    assert spec.get("type") == "function", f"{n}: not a function spec"
    fn = spec.get("function", {})
    assert fn.get("name") == n, f"{n}.json declares {fn.get('name')!r}"

assert "{exe}" in tool_schemas.raw("powershell"), (
    "powershell.json lost its {exe} placeholder — the file is box-specific"
)

for p in (paths.SYSTEM_PROMPT_FILE, paths.TOOLS_PROMPT_FILE):
    assert p.is_file(), f"prompt missing from the install: {p}"

# Every prompt the first turn reads through textfmt.load_prompt.
for name in ("compact", "wake-after-compact", "conversation-store",
             "tool-denied", "harness-capabilities"):
    text = textfmt.load_prompt(name)
    assert text.strip(), f"{name}.md loaded empty"

validate_tool_denied()  # the loud half — a broken refusal file fails here
print(f"in-venv checks OK: {len(names)} schemas, all prompts readable")
'''


def run_in_venv(venv_python: Path, workdir: Path) -> None:
    check = workdir / "in_venv_checks.py"
    check.write_text(IN_VENV_CHECKS, encoding="utf-8")
    r = _run([venv_python, str(check)], timeout=300)
    if r.returncode != 0:
        print(r.stdout[-4000:], file=sys.stderr)
        print(r.stderr[-4000:], file=sys.stderr)
        _fail("in-venv checks failed — the installed wheel is not importable")


def check_version(venv_python: Path, workdir: Path) -> None:
    text = (PKG_DIR / "version.py").read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*["\']([^"\']+)["\']', text)
    if not m:
        _fail("cannot find __version__ in src/litetui/version.py")
    expected = m.group(1)
    exe = (
        workdir / "venv" / "Scripts" / "litetui.exe"
        if os_name() == "nt"
        else workdir / "venv" / "bin" / "litetui"
    )
    r = _run([exe, "--version"], timeout=120)
    got = (r.stdout or "").strip()
    print(f"--version -> {got!r} (repo says {expected!r})")
    if expected not in got:
        _fail(f"installed --version output {got!r} does not carry repo version {expected!r}")


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="litetui-wheelgate-"))
    print(f"workdir: {workdir}", flush=True)
    try:
        wheel = build_wheel()
        print(f"wheel: {wheel.name}")
        check_zip(wheel)
        venv_python = make_venv(wheel, workdir)
        run_in_venv(venv_python, workdir)
        check_version(venv_python, workdir)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    print("WHEEL GATE PASSED")


if __name__ == "__main__":
    main()
