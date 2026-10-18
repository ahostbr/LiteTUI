"""T0586: malformed path tokens are denials, never crashes or safe targets."""
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from _owned_seat import owned_app

from litetui import deny_floor, seat_authority, tool_policy

INCIDENT = "Stopped \x00b7 approval policy (not completed),"


def assert_refused(reason):
    assert reason and "DENY FLOOR [unresolvable-path]" in reason
    assert "cannot resolve" in reason
    assert "\x00" not in reason, "the diagnostic must escape control characters"


@pytest.mark.parametrize("command", [
    f"echo data > '{INCIDENT}'",
    f"Set-Location '{INCIDENT}'; echo data > jobs.json",
    f"Remove-Item -Recurse '{INCIDENT}'",
])
def test_exact_incident_token_is_refused(tmp_path, command):
    assert_refused(deny_floor.refusal(command, tmp_path, shell="powershell"))


@pytest.mark.parametrize("guard", [seat_authority.jobs_file_refusal,
                                   seat_authority.settings_file_refusal])
@pytest.mark.parametrize("args", [
    {"command": f"echo data > '{INCIDENT}'"},
    {"path": INCIDENT},
    {"command": "echo data > ordinary.txt", "cwd": INCIDENT},
])
def test_seat_guard_refuses_invalid_paths_even_for_owner(tmp_path, monkeypatch, guard, args):
    monkeypatch.setattr(seat_authority, "is_ryans_own", lambda *_a, **_k: True)
    assert_refused(guard(SimpleNamespace(), args, tmp_path, shell="powershell"))


def test_direct_tool_write_is_refused(tmp_path):
    assert_refused(deny_floor.write_refusal(INCIDENT, tmp_path))


@pytest.mark.parametrize("error", [ValueError("invalid path"),
                                    OSError("invalid path"),
                                    RuntimeError("symlink loop")])
def test_resolution_failure_is_not_a_lexically_safe_path(tmp_path, monkeypatch, error):
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        if path.name == "bad-path":
            raise error
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    assert_refused(deny_floor.refusal("echo data > bad-path", tmp_path, shell="powershell"))
    assert_refused(deny_floor.write_refusal("bad-path", tmp_path))


@pytest.mark.parametrize("profile", [tool_policy.STRICT, tool_policy.INTERACTIVE,
                                      tool_policy.AUTONOMOUS])
@pytest.mark.asyncio
async def test_authorization_returns_refusal_instead_of_killing_seat(tmp_path, profile):
    app = owned_app()
    app.settings.tool_policy_profile = profile
    app._active_tool_profile = profile
    result = await app._authorize_action(
        "powershell", {"command": f"echo data > '{INCIDENT}'"},
        tool_policy.SHELL_POLICY, workspace=tmp_path)
    assert result
    assert_refused(result[0])


def test_valid_unicode_path_is_not_refused(tmp_path):
    assert deny_floor.refusal("echo data > 'Stopped \u00b7 approval policy'",
                              tmp_path, shell="powershell") is None
    assert deny_floor.write_refusal("Stopped \u00b7 approval policy", tmp_path) is None


@pytest.mark.parametrize("path", ["$unknown\x00", "bad\x00/*", "ordinary.txt:stream\x00"])
def test_null_is_not_erased_by_variables_globs_or_aliases(tmp_path, path):
    assert_refused(deny_floor.write_refusal(path, tmp_path))
    assert_refused(deny_floor.refusal(f"echo data > '{path}'", tmp_path, shell="powershell"))


@pytest.mark.parametrize("path", ["bad|path", "bad<path", 'bad"path', "bad\x01path",
                                   "ordinary.txt:bad|stream", "ordinary.txt:bad?stream"])
@pytest.mark.skipif(os.name != "nt", reason="Windows path syntax")
def test_invalid_windows_characters_are_refused_before_alias_reduction(tmp_path, path, monkeypatch):
    assert_refused(deny_floor.write_refusal(path, tmp_path))
    monkeypatch.setattr(seat_authority, "is_ryans_own", lambda *_a, **_k: True)
    assert_refused(seat_authority.jobs_file_refusal(SimpleNamespace(), {"path": path}, tmp_path))


@pytest.mark.parametrize("command, home, workspace", [
    ("cd; rm -rf ordinary", INCIDENT, None),
    ("rm -rf ordinary", INCIDENT, None),
    ("rm -rf ordinary", None, INCIDENT),
    ("cd 'bad\x00'; rm -rf *", None, None),
])
def test_other_resolution_sites_fail_closed(tmp_path, command, home, workspace):
    assert_refused(deny_floor.refusal(command, workspace or tmp_path,
                                      home=home or tmp_path, jobs=False))


@pytest.mark.parametrize("name", ["jobs.json", "settings.json", "ordinary.txt"])
def test_invalid_native_writer_operand_cannot_miss_name_prefilter(tmp_path, name, monkeypatch):
    command = f"Set-Content -LiteralPath '{name}\x00' -Value '[]'"
    assert_refused(deny_floor.refusal(command, tmp_path, shell="powershell"))
    monkeypatch.setattr(seat_authority, "is_ryans_own", lambda *_a, **_k: True)
    assert_refused(seat_authority.jobs_file_refusal(
        SimpleNamespace(), {"command": command}, tmp_path, shell="powershell"))


@pytest.mark.parametrize("guard", [seat_authority.jobs_file_refusal,
                                   seat_authority.settings_file_refusal])
def test_invalid_git_target_is_not_waived_for_owner(tmp_path, monkeypatch, guard):
    monkeypatch.setattr(seat_authority, "is_ryans_own", lambda *_a, **_k: True)
    name = "jobs.json" if guard is seat_authority.jobs_file_refusal else "settings.json"
    assert_refused(guard(SimpleNamespace(), {"command": f"git add 'bad\x00/{name}'"},
                         tmp_path, shell="powershell"))


@pytest.mark.skipif(os.name != "nt", reason="Windows path syntax")
def test_invalid_data_root_is_not_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path / "bad|root"))
    assert_refused(deny_floor.write_refusal("jobs.json", tmp_path))


@pytest.mark.skipif(os.name != "nt", reason="Windows path syntax")
def test_bad_git_environment_only_refuses_git_writers(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "bad|repo"))
    assert deny_floor.refusal("echo ok", tmp_path, shell="powershell") is None
    assert deny_floor.refusal("git status", tmp_path, shell="powershell") is None
    assert_refused(deny_floor.refusal("git add ordinary.txt", tmp_path, shell="powershell"))


def test_missing_process_cwd_is_a_seat_refusal(tmp_path, monkeypatch):
    def missing_cwd(cls):
        raise FileNotFoundError("current directory was removed")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "cwd", classmethod(missing_cwd))
        reason = seat_authority.jobs_file_refusal(SimpleNamespace(), {"path": "ordinary.txt"}, tmp_path)
    assert_refused(reason)


def test_nonexistent_valid_paths_remain_usable(tmp_path):
    assert deny_floor.write_refusal("new/subfolder/file.txt", tmp_path) is None
    assert deny_floor.refusal("echo data > new.txt", tmp_path, shell="powershell") is None


@pytest.mark.skipif(os.name != "nt", reason="Windows path syntax")
def test_valid_extended_length_paths_are_preserved(tmp_path, monkeypatch):
    path = "\\\\?\\" + str(tmp_path / "ordinary.txt")
    assert deny_floor.write_refusal(path, tmp_path) is None
    assert deny_floor.resolve_path(Path(path)) == Path(path).resolve()
    monkeypatch.setattr(seat_authority, "is_ryans_own", lambda *_a, **_k: True)
    assert seat_authority.jobs_file_refusal(SimpleNamespace(), {"path": path}, tmp_path) is None


@pytest.mark.skipif(os.name != "nt", reason="Windows path syntax")
def test_embedded_quote_in_parsed_redirect_remains_invalid(tmp_path):
    assert_refused(deny_floor.refusal("echo data > 'bad\"path'", tmp_path, shell="powershell"))
