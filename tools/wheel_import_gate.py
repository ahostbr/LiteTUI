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

import os
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
    # Never certify a wheel via checkout imports or the caller's Python home.
    env = {key: value for key, value in os.environ.items()
           if key.upper() not in {"PYTHONPATH", "PYTHONHOME"}}
    env["PYTHONNOUSERSITE"] = "1"
    try:
        return subprocess.run(
            [str(c) for c in cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=str(cwd) if cwd else None,
            env=env,
        )
    except subprocess.TimeoutExpired:
        _fail(f"timed out after {timeout}s: {' '.join(str(c) for c in cmd)}")


def build_wheel(workdir: Path | None = None) -> Path:
    uv = shutil.which("uv")
    if uv is None:
        _fail("`uv` not found on PATH — the gate builds with it (CI has it)")
    # Never delete the operator's dist artifacts or select a pre-existing wheel.
    output = Path(tempfile.mkdtemp(prefix="wheel-output-", dir=workdir))
    r = _run([uv, "build", "--wheel", "--out-dir", str(output)], timeout=600, cwd=REPO)
    if r.returncode != 0:
        print(r.stdout, file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        _fail("uv build --wheel failed")
    wheels = sorted(output.glob("litetui-*.whl"))
    if len(wheels) != 1:
        _fail(f"expected exactly one fresh wheel, found {len(wheels)} in {output}")
    return wheels[0]


#: Directory names that never ship as source: bytecode caches and build/tool
#: artifacts. A `.py` under any of these is not a module and is excluded from the
#: required set. Everything else under src/litetui IS required — we do NOT gate
#: on __init__.py, because `[tool.setuptools.packages.find]` here declares only
#: `where` (no `namespaces = false`), and plain find can ship IMPLICIT NAMESPACE
#: packages (dirs with no __init__.py). Gating on __init__.py would silently drop
#: a namespace module from the requirement and let a wheel omit it undetected —
#: the exact class this gate exists to catch. Erring toward "require it": a false
#: require is a loud, fixable signal; a false pass is the silent ship bug.
_NON_SHIPPING_DIRS = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist"}


def _required_entries(pkg_dir: Path) -> list[str]:
    """Every wheel entry the source tree obliges a built wheel to carry.

    Package-data by glob (schemas/prompts/assets), PI_NOTICE by exact name, and
    every source `*.py` as a module (namespace-safe — see `_NON_SHIPPING_DIRS`).
    A required SOURCE input that is itself missing fails loudly here via `_fail`,
    so a hole in the source tree cannot vanish from a glob and pass unnoticed.
    """
    required: list[str] = []
    for folder, suffix in (("schemas", ".json"), ("prompts", ".md"), ("assets", ".wav")):
        src = pkg_dir / folder
        if not src.is_dir():
            _fail(f"{src} is missing from the source tree — nothing to ship")
        for f in sorted(src.glob(f"*{suffix}")):
            required.append(f"litetui/{folder}/{f.name}")
    notice = pkg_dir / "PI_NOTICE.txt"
    if not notice.is_file():
        _fail(f"{notice} is missing from the source tree — nothing to ship")
    required.append("litetui/PI_NOTICE.txt")
    for f in sorted(pkg_dir.rglob("*.py")):
        rel = f.relative_to(pkg_dir)
        if any(part in _NON_SHIPPING_DIRS or part.endswith(".egg-info") for part in rel.parts):
            continue
        required.append("litetui/" + rel.as_posix())
    return required


def check_zip(wheel: Path, pkg_dir: Path = PKG_DIR) -> None:
    """Every source module AND package-data file on disk must be inside the zip.

    Guards the packaging-drift class in one place: a module absent from the built
    wheel (the 26-vs-28 bug) or a package-data resource setuptools never shipped
    (schemas / prompts / assets / PI_NOTICE — the T135 FileNotFoundError-on-launch
    class). Package payload must match the source inventory; distribution metadata
    outside litetui/ is allowed. Obsolete build-directory residue is rejected.
    `pkg_dir` is a seam for the offline synthetic-fixture tests; main() uses the
    real tree.
    """
    required = _required_entries(pkg_dir)
    with zipfile.ZipFile(wheel) as zf:
        entries = zf.namelist()
        names = set(entries)
        missing = [entry for entry in required if entry not in names]
        if missing:
            _fail(f"source files NOT in the wheel: {sorted(missing)}")
        extra = sorted(entry for entry in names - set(required)
                       if entry.startswith("litetui/") and not entry.endswith("/"))
        if extra:
            _fail(f"unexpected package payload (possibly stale build residue): {extra}")
        duplicates = [entry for entry in required if entries.count(entry) != 1]
        if duplicates:
            _fail(f"ambiguous duplicate wheel members: {duplicates}")
        stale = [entry for entry in required
                 if zf.read(entry) != (pkg_dir / Path(entry).relative_to('litetui')).read_bytes()]
        if stale:
            _fail(f"wheel bytes differ from candidate source: {stale}")
    print(f"zip OK: all {len(required)} source modules + data byte-matched "
          f"({len(names)} entries in wheel)")


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
import importlib.metadata
from pathlib import Path
import sys
import litetui
from litetui.version import __version__

prefix = Path(sys.prefix).resolve()
assert sys.prefix != sys.base_prefix, "probe is not inside a virtual environment"
assert Path(litetui.__file__).resolve().is_relative_to(prefix), "package imported outside venv"
assert importlib.metadata.version("litetui") == __version__, "installed metadata/version mismatch"
from litetui import app  # noqa: F401  <- THE heavy import; died in 0.22.0
from litetui import paths, tool_schemas, textfmt
from litetui.textfmt import validate_tool_denied

for module in (app, paths, tool_schemas, textfmt):
    assert Path(module.__file__).resolve().is_relative_to(prefix), f"source fallback: {module.__name__}"

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
    r = _run([venv_python, "-I", str(check)], timeout=300, cwd=workdir)
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
    r = _run([exe, "--version"], timeout=120, cwd=workdir)
    if r.returncode != 0:
        _fail(f"installed --version failed (exit {r.returncode})")
    got = (r.stdout or "").strip()
    print(f"--version -> {got!r} (repo says {expected!r})")
    if got != f"litetui {expected}":
        _fail(f"installed --version output {got!r} does not exactly match repo version {expected!r}")


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="litetui-wheelgate-"))
    print(f"workdir: {workdir}", flush=True)
    try:
        wheel = build_wheel(workdir)
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
