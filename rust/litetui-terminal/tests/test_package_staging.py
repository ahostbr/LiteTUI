"""Offline package contract and controlled Python probes; no Rust or provider launch."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

CRATE = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell packaging")


def run(*args: str | Path, cwd: Path | None = None, env=None):
    return subprocess.run([str(a) for a in args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", timeout=60, check=False, env=env)


def powershell(script, *args):
    return run("pwsh.exe", "-NoLogo", "-NoProfile", "-NonInteractive", "-File", script, *args)


def checked(result):
    assert result.returncode == 0, result.stdout + result.stderr
    return result


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "source tree"
    root = repo / "rust/litetui-terminal"
    root.mkdir(parents=True)
    for name in ("stage-package.ps1", "package-launch.ps1"):
        shutil.copyfile(CRATE / name, root / name)
    files = (
        "src/litetui/__init__.py", "src/litetui/cli.py", "src/litetui/app.py",
        "src/litetui/schemas/__init__.py", "src/litetui/schemas/harness.json",
        "src/litetui/prompts/systemprompt.md", "src/litetui/assets/ding.wav",
        "tools/chrome-bridge/bridge.py", "tools/chrome-bridge/extension/manifest.json",
        "tools/pccontrol/pccontrol.py", "tools/pccontrol/screenshot.ps1", "tools/convo_search.py",
        "pyproject.toml", "uv.lock", "README.md",
    )
    for name in files:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("FIXTURE " + name, encoding="utf-8")
    checked(run("git", "init", "-q", repo))
    checked(run("git", "-C", repo, "add", "."))
    hooks = tmp_path / "empty-hooks"
    hooks.mkdir()
    checked(run("git", "-C", repo, "-c", "user.name=Package Test", "-c", "user.email=test@example.invalid",
                "-c", f"core.hooksPath={hooks}", "-c", "commit.gpgSign=false",
                "commit", "-qm", "controlled packaging inputs"))
    executable = tmp_path / "inert build.exe"
    executable.write_bytes(b"NOT AN EXECUTABLE - MUST NEVER BE STARTED")
    package = tmp_path / "package"
    return repo, root, executable, package


def stage(project):
    _repo, root, executable, package = project
    checked(powershell(root / "stage-package.ps1", "-Destination", package, "-Executable", executable))
    return package


def manifest(package):
    return json.loads((package / "package-manifest.json").read_text(encoding="utf-8"))


def write_manifest(package, value):
    (package / "package-manifest.json").write_text(json.dumps(value), encoding="utf-8")


def test_stage_tracks_exact_bytes_and_excludes_user_state_and_untracked_files(project):
    repo, _root, _exe, _package = project
    (repo / "src/litetui/private-draft.py").write_text("private", encoding="utf-8")
    (repo / ".agents").mkdir()
    (repo / ".agents/secret.txt").write_text("private", encoding="utf-8")
    (repo / "src/litetui/app.py").write_text("DIRTY WORKING COPY", encoding="utf-8")
    package = stage(project)
    value = manifest(package)
    assert value["source"]["working_tree_changes"] is True
    assert value["source"]["base_revision"] == checked(run("git", "-C", repo, "rev-parse", "HEAD")).stdout.strip()
    assert value["python"]["bundled"] is False
    assert "not verified" in value["executable"]["provenance"]
    assert not (package / "runtime/src/litetui/private-draft.py").exists()
    assert not (package / "runtime/.agents").exists()
    assert (package / "runtime/src/litetui/app.py").read_text() == "DIRTY WORKING COPY"
    for entry in value["files"]:
        data = (package / entry["path"]).read_bytes()
        assert entry["sha256"] == hashlib.sha256(data).hexdigest() and entry["bytes"] == len(data)
    assert {"runtime/src/litetui/assets/ding.wav", "runtime/tools/pccontrol/screenshot.ps1",
            "runtime/tools/chrome-bridge/extension/manifest.json"} <= {e["path"] for e in value["files"]}


def test_package_verifies_after_relocation_without_python_or_checkout(project, tmp_path):
    package = stage(project)
    moved = tmp_path / "relocated package 雪 Ryan's"
    package.rename(moved)
    checked(powershell(moved / "launch.ps1", "-VerifyOnly"))
    assert manifest(moved)["source"]["working_tree_changes"] is False
    assert not (tmp_path / "data").exists()


@pytest.mark.parametrize("fault", ["changed", "missing", "extra", "duplicate", "escape", "absolute",
                                    "ads", "version-string", "python-bool", "negative-bytes"])
def test_verification_rejects_tampering_and_invalid_paths_before_launch(project, fault):
    package = stage(project)
    value = manifest(package)
    row = next(e for e in value["files"] if e["path"] == "runtime/src/litetui/app.py")
    if fault == "changed":
        (package / row["path"]).write_bytes(b"ALTERED")
    elif fault == "missing":
        (package / row["path"]).unlink()
    elif fault == "extra":
        (package / "unlisted.dll").write_bytes(b"UNLISTED")
    elif fault == "duplicate":
        value["files"].append(dict(row))
    elif fault == "escape":
        row["path"] = "../outside.txt"
    elif fault == "absolute":
        row["path"] = "C:/outside.txt"
    elif fault == "ads":
        row["path"] += ":hidden"
    elif fault == "version-string":
        value["version"] = "1"
    elif fault == "python-bool":
        value["python"]["bundled"] = 0
    elif fault == "negative-bytes":
        row["bytes"] = -1
    write_manifest(package, value)
    result = powershell(package / "launch.ps1", "-VerifyOnly")
    assert result.returncode != 0, result.stdout + result.stderr


def test_stage_refuses_existing_destination_and_missing_tracked_resource(project):
    repo, root, exe, package = project
    package.mkdir()
    sentinel = package / "keep.txt"
    sentinel.write_text("KEEP")
    result = powershell(root / "stage-package.ps1", "-Destination", package, "-Executable", exe)
    assert result.returncode != 0 and sentinel.read_text() == "KEEP"
    (repo / "src/litetui/app.py").unlink()
    other = package.parent / "other-package"
    result = powershell(root / "stage-package.ps1", "-Destination", other, "-Executable", exe)
    assert result.returncode != 0 and not other.exists()


def launch_wrapper(package, tmp_path):
    """Replace only the process boundary and rehash that controlled fixture."""
    launcher = package / "launch.ps1"
    text = launcher.read_text()
    boundary = "& $executable @launchArgs"
    assert text.count(boundary) == 1
    launcher.write_text(text.replace(boundary, "& Test-RustPackageLaunch @launchArgs"), encoding="utf-8")
    value = manifest(package)
    row = next(e for e in value["files"] if e["path"] == "launch.ps1")
    data = launcher.read_bytes()
    row.update(bytes=len(data), sha256=hashlib.sha256(data).hexdigest())
    write_manifest(package, value)
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text(
        "param([string]$Launcher,[string]$Capture,[string]$Values,[int]$Result=0)\n"
        "$ErrorActionPreference='Stop'\n"
        "function Test-RustPackageLaunch {\n"
        " if ($env:PYTHONDONTWRITEBYTECODE -ne '1') { throw 'Package imports would mutate source' }\n"
        " [IO.File]::WriteAllText($Capture,(ConvertTo-Json -InputObject @($args) -Compress))\n"
        " $global:LASTEXITCODE=$Result\n}\n"
        "$options=Get-Content -LiteralPath $Values -Raw | ConvertFrom-Json -AsHashtable\n"
        "$env:PYTHONDONTWRITEBYTECODE='fixture-parent'\n"
        "try { & $Launcher @options } finally {\n"
        " if ($env:PYTHONDONTWRITEBYTECODE -ne 'fixture-parent') { throw 'Caller environment changed' }\n"
        "}\n", encoding="utf-8")
    return wrapper


@pytest.mark.parametrize("profile", [None, "strict", "interactive", "autonomous"])
@pytest.mark.parametrize("existing", [False, True])
def test_portable_launch_preserves_explicit_policy_and_resume_without_build_or_data_writes(project, tmp_path, profile, existing):
    package = stage(project)
    wrapper = launch_wrapper(package, tmp_path)
    capture = tmp_path / "args.json"
    data_root = tmp_path / "retained data"
    if existing:
        data_root.mkdir()
        (data_root / "keep.txt").write_bytes(b"retained data")
    options = {"Python": sys.executable, "Backend": "codex", "Model": "saved model",
               "Tools": "on", "Workspace": str(tmp_path), "DataRoot": str(data_root),
               "Resume": "12345678-1234-4123-8123-123456789abc"}
    if profile is not None:
        options["ToolProfile"] = profile
    values = tmp_path / "values.json"
    values.write_text(json.dumps(options), encoding="utf-8")
    checked(powershell(wrapper, "-Launcher", package / "launch.ps1", "-Capture", capture, "-Values", values))
    args = json.loads(capture.read_text())
    assert args[0] == "--interactive"
    for key, expected in (("--source", str(package / "runtime")), ("--data-root", str(data_root)),
                          ("--workspace", str(tmp_path)), ("--model", "saved model"), ("--backend", "codex")):
        assert args[args.index(key) + 1] == expected
    assert ("--tool-profile" in args) is (profile is not None)
    if profile is not None:
        assert args[args.index("--tool-profile") + 1] == profile
    assert args.count("--resume") == 1
    if existing:
        assert {path.name: path.read_bytes() for path in data_root.iterdir()} == {"keep.txt": b"retained data"}
    else:
        assert not data_root.exists()


@pytest.mark.parametrize("fault", ["data-inside-package", "resume-without-root", "tools-without-workspace", "python-missing"])
def test_invalid_launch_never_reaches_process_boundary(project, tmp_path, fault):
    package = stage(project)
    wrapper = launch_wrapper(package, tmp_path)
    capture = tmp_path / "args.json"
    options = {"Python": sys.executable, "Backend": "claude", "Model": "explicit-model"}
    if fault == "data-inside-package":
        options["DataRoot"] = str(package / "state")
    elif fault == "resume-without-root":
        options["Resume"] = "12345678-1234-4123-8123-123456789abc"
    elif fault == "tools-without-workspace":
        options["Tools"] = "on"
    else:
        options["Python"] = str(tmp_path / "missing.exe")
    values = tmp_path / "values.json"
    values.write_text(json.dumps(options), encoding="utf-8")
    result = powershell(wrapper, "-Launcher", package / "launch.ps1", "-Capture", capture, "-Values", values)
    assert result.returncode != 0 and not capture.exists()


def test_runtime_failure_is_not_reported_as_package_success(project, tmp_path):
    package = stage(project)
    wrapper = launch_wrapper(package, tmp_path)
    capture, values = tmp_path / "args.json", tmp_path / "values.json"
    values.write_text(json.dumps({"Python": sys.executable, "Backend": "claude", "Model": "explicit-model"}))
    result = powershell(wrapper, "-Launcher", package / "launch.ps1", "-Capture", capture, "-Values", values, "-Result", "7")
    assert result.returncode != 0 and capture.exists()
    assert "Live runtime failed: 7" in result.stderr


def make_junction(tmp_path, path, target):
    script = tmp_path / "junction.ps1"
    script.write_text("param($Link,$Target)\n$ErrorActionPreference='Stop'\n"
                      "New-Item -ItemType Junction -Path $Link -Target $Target | Out-Null\n")
    checked(powershell(script, "-Link", path, "-Target", target))


@pytest.mark.parametrize("suffix", ["", "new-state", "nested/new-state"])
def test_data_root_junction_alias_is_rejected_before_launch_or_package_writes(project, tmp_path, suffix):
    package = stage(project)
    wrapper = launch_wrapper(package, tmp_path)
    alias = tmp_path / "external state alias"
    make_junction(tmp_path, alias, package / "runtime")
    data_root = alias / suffix if suffix else alias
    before = {str(path.relative_to(package)): path.read_bytes() for path in package.rglob("*") if path.is_file()}
    capture, values = tmp_path / "args.json", tmp_path / "values.json"
    values.write_text(json.dumps({
        "Python": sys.executable, "Backend": "claude", "Model": "explicit-model", "DataRoot": str(data_root),
    }), encoding="utf-8")
    result = powershell(wrapper, "-Launcher", package / "launch.ps1", "-Capture", capture, "-Values", values)
    assert result.returncode != 0 and "Linked DataRoot path:" in result.stderr
    assert not capture.exists()
    assert {str(path.relative_to(package)): path.read_bytes() for path in package.rglob("*") if path.is_file()} == before
    assert not (package / "runtime/new-state").exists() and not (package / "runtime/nested").exists()
    checked(powershell(package / "launch.ps1", "-VerifyOnly"))


def test_package_parent_junction_cannot_hide_data_root_containment(project, tmp_path):
    package = stage(project)
    wrapper = launch_wrapper(package, tmp_path)
    real_parent = tmp_path / "real parent"
    real_parent.mkdir()
    relocated = real_parent / "package"
    package.rename(relocated)
    alias = tmp_path / "package parent alias"
    make_junction(tmp_path, alias, real_parent)
    data_root = relocated / "runtime/new-state"
    capture, values = tmp_path / "args.json", tmp_path / "values.json"
    values.write_text(json.dumps({
        "Python": sys.executable, "Backend": "claude", "Model": "explicit-model", "DataRoot": str(data_root),
    }), encoding="utf-8")
    result = powershell(wrapper, "-Launcher", alias / "package/launch.ps1", "-Capture", capture, "-Values", values)
    assert result.returncode != 0
    assert "Linked package path:" in result.stderr or "DataRoot must be outside" in result.stderr
    assert not capture.exists() and not data_root.exists()
    checked(powershell(relocated / "launch.ps1", "-VerifyOnly"))


@pytest.mark.parametrize("where", ["source", "package"])
def test_junctions_are_rejected_before_external_resources_are_read(project, tmp_path, where):
    repo, root, exe, destination = project
    if where == "source":
        source = repo / "src/litetui"
        outside = tmp_path / "outside-source"
        source.rename(outside)
        make_junction(tmp_path, source, outside)
        result = powershell(root / "stage-package.ps1", "-Destination", destination, "-Executable", exe)
        assert result.returncode != 0 and not destination.exists()
    else:
        package = stage(project)
        source = package / "runtime/src/litetui"
        outside = tmp_path / "outside-package"
        source.rename(outside)
        make_junction(tmp_path, source, outside)
        result = powershell(package / "launch.ps1", "-VerifyOnly")
        assert result.returncode != 0
    assert "Linked" in result.stderr


def test_actual_runtime_resources_and_owned_construction_survive_packaging(project, tmp_path):
    """Actual packaged source, real owned constructor/store; external boundaries controlled."""
    repo, _root, _exe, _package = project
    checkout = CRATE.parents[1]
    paths = checked(run("git", "-C", checkout, "ls-files", "-z", "--",
                        "src/litetui", "tools/chrome-bridge", "tools/pccontrol",
                        "tools/convo_search.py", "prompts", "pyproject.toml", "uv.lock")).stdout.split("\0")
    for relative in filter(None, paths):
        target = repo / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(checkout / relative, target)
    checked(run("git", "-C", repo, "add", "."))
    package = stage(project)
    runtime = package / "runtime"
    environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", LITETUI_NO_HARNESS="1",
                       PYTHONIOENCODING="utf-8", PYTHONUTF8="1",
                       PYTHONPATH=str(runtime / "src"))
    help_result = run(sys.executable, "-B", "-m", "litetui.cli", "--help", cwd=tmp_path, env=environment)
    checked(help_result)
    assert "--hold-restored-input" in help_result.stdout and "--create-agent" in help_result.stdout
    checks = tmp_path / "source-verification"
    checks.mkdir()
    for name in ("test_rust_owned_construction.py", "test_rust_packaged_tool_state.py",
                 "_owned_seat.py", "_isolated_home.py"):
        shutil.copyfile(checkout / "tests" / name, checks / name)
    conftest = (checkout / "tests/conftest.py").read_text(encoding="utf-8")
    anchor = "ROOT = Path(__file__).resolve().parent.parent"
    assert conftest.count(anchor) == 1
    (checks / "conftest.py").write_text(conftest.replace(anchor, f"ROOT = Path({str(runtime)!r})"), encoding="utf-8")
    (checks / "test_packaged_origin.py").write_text(
        "from pathlib import Path\n"
        "def test_packaged_source_origin():\n"
        "    import litetui.app\n"
        "    from litetui import tool_schemas, paths\n"
        f"    root = Path({str(runtime)!r})\n"
        "    assert Path(litetui.app.__file__).is_relative_to(root)\n"
        "    assert paths.ROOT == root\n"
        "    assert tool_schemas.load('harness')\n"
        "    assert paths.prompt_file('systemprompt.md').is_file()\n"
        "    assert (root / 'tools/chrome-bridge/bridge.py').is_file()\n"
        "    assert (root / 'tools/pccontrol/screenshot.ps1').is_file()\n",
        encoding="utf-8")
    checked(run(sys.executable, "-B", "-m", "pytest", "-q", "-p", "no:cacheprovider",
                "--confcutdir", checks,
                str(checks / "test_rust_owned_construction.py") + "::test_owned_constructor_preserves_authority_and_real_agent_lease",
                str(checks / "test_rust_owned_construction.py") + "::test_real_materialization_and_conversation_lease_remain_separate",
                checks / "test_packaged_origin.py", checks / "test_rust_packaged_tool_state.py",
                cwd=checks, env=environment))
    checked(powershell(package / "launch.ps1", "-VerifyOnly"))
    assert not list(runtime.rglob("__pycache__"))
