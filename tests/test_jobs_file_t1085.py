"""T1085 — jobs.json is Owner's: only Owner's own seat may directly write it.

T1133 removed profile locks and schedule API caps. These arms protect only direct
file writes, at the shared _authorize_action door, without restoring those caps.
The original alias bug was measured by Dijkstra (69c7c209 A1).
"""
from __future__ import annotations

import pytest

from litetui import app as m
from litetui import claude_tools, scheduler, seat_authority, settings, tool_policy
from litetui.agent_launch_context import ordinary
from litetui.tool_policy import AUTONOMOUS

SPAWNER = "leader-4f1e2d3c-0000-0000-0000-000000000001"
_SESSIONS: list = []


@pytest.fixture(autouse=True)
def _release_owned_sessions():
    """Give back the sessions this test's apps were built on."""
    yield
    while _SESSIONS:
        _SESSIONS.pop().release()


def _app(tmp_path):
    """The constructor refuses a bare build: own a test seat in tmp_path."""
    cfg = settings.Settings()
    cfg.backend, cfg.default_model, cfg.thinking_level = "lmstudio", "a-model", "off"
    session = ordinary(tmp_path, cfg)
    _SESSIONS.append(session)
    return m.LiteTUI(agent_session=session)


def _agent(tmp_path, monkeypatch):
    """An autonomous agent seat; the file guard must not narrow its profile."""
    a = _app(tmp_path)
    a.settings.tool_policy_profile = AUTONOMOUS
    a._active_tool_profile = AUTONOMOUS
    a._spawned_seat = True
    a._owner_seat = False
    a._spawner_id = SPAWNER
    sent: list = []
    a.seat.registered = True
    a.seat.send = lambda to, body: sent.append((to, body)) or False
    a._system = lambda *_a, **_k: None

    def no_door(*_a, **_k):
        raise AssertionError("a human door opened; the jobs-file rule is not theirs to waive")

    monkeypatch.setattr(m, "show_dialog", no_door)

    async def no_rpc(*_a, **_k):
        raise AssertionError("the rpc host was asked")

    monkeypatch.setattr(m.tool_approval, "approve_over_rpc", no_rpc)
    return a, sent


def _ryans(a):
    a._spawned_seat, a._owner_seat, a._pty_term = False, True, None
    return a


def _calls(jobs):
    """Every backend's spelling of a write to the schedule file."""
    native_policy, native_args = claude_tools.native_policy(
        "Write", {"file_path": str(jobs), "content": "[]"})
    patch = f"*** Begin Patch\n*** Update File: {jobs}\n@@\n-[]\n+[1]\n*** End Patch\n"
    return [
        ("write", {"path": str(jobs), "content": "[]"}, tool_policy.WRITE_POLICY),
        ("edit", {"path": str(jobs), "old_string": "[]", "new_string": "[1]"},
         tool_policy.WRITE_POLICY),
        ("Write", native_args, native_policy),
        ("apply_patch", {"command": ["apply_patch", patch]}, tool_policy.WRITE_POLICY),
        ("bash", {"command": f"echo [] > {jobs}"}, tool_policy.SHELL_POLICY),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("index", range(5))
async def test_an_agent_seat_may_not_write_jobs_json_by_any_backend(tmp_path, monkeypatch, index):
    a, sent = _agent(tmp_path, monkeypatch)
    name, args, policy = _calls(tmp_path / "jobs.json")[index]
    result = await a._authorize_action(name, args, policy, workspace=tmp_path)
    assert a._active_tool_profile == AUTONOMOUS, "the file guard must not cap profiles"
    assert result, (name, "the write was allowed")
    text = result[0]
    assert "may not write" in text and "jobs.json" in text and "T1085" in text, text
    assert "/cron" in text
    assert sent == [], "a jobs.json write was relayed to the spawner for an APPROVE"


@pytest.mark.asyncio
async def test_the_refusal_comes_before_the_relay(tmp_path, monkeypatch):
    a, sent = _agent(tmp_path, monkeypatch)
    assert seat_authority.confirm_route(a) == "spawner"
    result = await a._authorize_action("bash", {"command": "echo [] > jobs.json"},
                                       tool_policy.SHELL_POLICY, workspace=tmp_path)
    assert result and "T1085" in result[0]
    assert sent == []


def test_ryans_own_seat_may_write_its_schedule(tmp_path):
    a = _ryans(_app(tmp_path))
    for _name, args, policy in _calls(tmp_path / "jobs.json"):
        assert seat_authority.jobs_file_refusal(a, args, tmp_path, policy) is None, args


def test_the_litetui_floor_leaves_jobs_to_the_seat(tmp_path):
    assert tool_policy._floor({"command": f"echo [] > {tmp_path / 'jobs.json'}"}, tmp_path) is None
    assert "[home-variable-delete]" in tool_policy._floor({"command": "rm -rf ~"}, tmp_path)


def test_scheduler_save_is_untouched_in_an_agent_seat(tmp_path, monkeypatch):
    _agent(tmp_path, monkeypatch)
    scheduler.save([], tmp_path)
    assert (tmp_path / "jobs.json").is_file()


@pytest.mark.parametrize("args", [
    {"path": "notes.md", "content": "x"},
    {"command": "cat jobs.json"},
    {"command": "copy jobs.json backup.json"},
    {"path": "myjobs.json", "content": "x"},
])
def test_other_files_and_reads_pass_in_an_agent_seat(tmp_path, monkeypatch, args):
    a, _ = _agent(tmp_path, monkeypatch)
    assert seat_authority.jobs_file_refusal(a, args, tmp_path) is None, args


def test_another_folders_jobs_json_passes(tmp_path, monkeypatch):
    a, _ = _agent(tmp_path, monkeypatch)
    other = tmp_path / "elsewhere"
    other.mkdir()
    assert seat_authority.jobs_file_refusal(a, {"path": str(other / "jobs.json")}, tmp_path) is None


@pytest.mark.parametrize("alias", ["jobs.json::$DATA", "jobs.json.", "jobs.json ", "jobs.json:x"])
def test_a_windows_alias_of_jobs_json_is_refused_too(tmp_path, monkeypatch, alias):
    a, _ = _agent(tmp_path, monkeypatch)
    why = seat_authority.jobs_file_refusal(a, {"path": str(tmp_path / "x")[:-1] + alias}, tmp_path)
    assert why and "T1085" in why, alias


@pytest.mark.asyncio
async def test_a_read_tool_path_is_not_a_write(tmp_path, monkeypatch):
    """The original argument-only guard mistook read(path=jobs.json) for a write."""
    a, _ = _agent(tmp_path, monkeypatch)
    assert await a._authorize_action("read", {"path": str(tmp_path / "jobs.json")},
                                     tool_policy.READ_POLICY, workspace=tmp_path) is None


@pytest.mark.parametrize("clean", [True, False])
def test_protected_write_rechecks_owner_taint(tmp_path, monkeypatch, clean):
    """A cached owner mark must not exempt a bridge-tainted write mid-turn."""
    a = _ryans(_app(tmp_path))
    a._pty_term = "owner-terminal"
    monkeypatch.setattr(seat_authority, "pty_taint_clean", lambda term: clean)
    why = seat_authority.jobs_file_refusal(a, {"path": str(tmp_path / "jobs.json")}, tmp_path)
    assert (why is None) is clean
    assert a._owner_seat is clean


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["restore --", "checkout HEAD --", "rm --", "mv --", "clean -fx --", "-C {root} restore --", "-C {root} clean -fx --"])
async def test_git_explicit_schedule_writers_are_refused(tmp_path, monkeypatch, verb):
    a, sent = _agent(tmp_path, monkeypatch)
    verb = verb.format(root=tmp_path)
    target = "jobs.json" if verb.startswith("-C") else f"{tmp_path}/jobs.json"
    args = {"command": f"git {verb} {target} backup.json"}
    denied = await a._authorize_action("bash", args, tool_policy.SHELL_POLICY, workspace=tmp_path)
    assert denied and "[jobs-file]" in denied[0]
    assert sent == []
    assert a._active_tool_profile == AUTONOMOUS
    assert seat_authority.jobs_file_refusal(_ryans(a), args, tmp_path) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["show HEAD:", "diff -- ", "status -- ", "-C {root} diff -- ",
                                      "log -- ", "blame -- ", "ls-files -- "])
async def test_git_schedule_reads_are_allowed(tmp_path, monkeypatch, verb):
    a, sent = _agent(tmp_path, monkeypatch)
    verb = verb.format(root=tmp_path)
    target = "jobs.json" if verb.startswith("-C") else f"{tmp_path}/jobs.json"
    args = {"command": f"git {verb}{target}"}
    assert seat_authority.jobs_file_refusal(a, args, tmp_path) is None
    assert await a._authorize_action("bash", args, tool_policy.SHELL_POLICY, workspace=tmp_path) is None
    assert sent == []
    assert a._active_tool_profile == AUTONOMOUS


FINAL_GIT_WITNESSES = [
    "git --no-pager -C {root} clean -fx -- jobs.json",
    "git --no-pager -C {root} restore -- jobs.json",
    "git --git-dir={root}/.git --work-tree={root} clean -fx -- jobs.json",
    "git -c core.worktree={root} clean -fx -- jobs.json",
    "GIT_WORK_TREE={root} git clean -fx -- jobs.json",
    "git -C {root} clean -fx -- notes.md",  # accepted relocation friction
    "git diff --output={root}/jobs.json HEAD",
    "git log --output={root}/jobs.json",
    "git show HEAD:{root}/jobs.json > {root}/jobs.json",
    "git --no-pager -C {root} diff -- jobs.json > {root}/jobs.json",
    "git log -o {root}/jobs.json",
    "git log --output {root}/jobs.json",
    "git diff --output={root}/jobs.json -- jobs.json",
]

@pytest.mark.asyncio
@pytest.mark.parametrize("template", FINAL_GIT_WITNESSES)
async def test_final_git_guard_cross_root_witnesses(tmp_path, monkeypatch, template):
    a, sent = _agent(tmp_path, monkeypatch)
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / ".litetui-data.json").write_text("{}")
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    args = {"command": template.format(root=protected)}
    denied = await a._authorize_action("bash", args, tool_policy.SHELL_POLICY, workspace=plain)
    assert denied and "[jobs-file]" in denied[0]
    assert sent == []
    assert seat_authority.jobs_file_refusal(_ryans(a), args, plain) is None
    assert a._active_tool_profile == AUTONOMOUS


@pytest.mark.asyncio
@pytest.mark.parametrize("template", ["git show HEAD:{root}/jobs.json",
                                      "git --no-pager -C {root} diff -- jobs.json"])
async def test_git_cross_root_schedule_reads_are_allowed(tmp_path, monkeypatch, template):
    a, sent = _agent(tmp_path, monkeypatch)
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / ".litetui-data.json").write_text("{}")
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    args = {"command": template.format(root=protected)}
    assert seat_authority.jobs_file_refusal(a, args, plain) is None
    assert await a._authorize_action("bash", args, tool_policy.SHELL_POLICY, workspace=plain) is None
    assert sent == []


@pytest.mark.parametrize("command", ["git status", "git diff -- notes.md", "git add notes.md",
                                      "git restore -- notes.md", "git show HEAD:notes.md"])
def test_normal_worktree_git_friction_controls(tmp_path, monkeypatch, command):
    a, _ = _agent(tmp_path, monkeypatch)
    assert seat_authority.jobs_file_refusal(a, {"command": command}, tmp_path) is None


GIT_ENV_NAMES = ["GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"]
GIT_ENV_ASSIGNMENTS = ["set {name}={root} &&", "export {name}={root};",
                       "$env:{name}='{root}';", "{name}={root}"]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", GIT_ENV_NAMES)
@pytest.mark.parametrize("assignment", GIT_ENV_ASSIGNMENTS)
@pytest.mark.parametrize("writer", ["git clean -fx -- jobs.json", "git add notes.md"])
async def test_git_env_assignment_taints_submission(tmp_path, monkeypatch, name, assignment, writer):
    a, sent = _agent(tmp_path, monkeypatch)
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / ".litetui-data.json").write_text("{}")
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    args = {"command": assignment.format(name=name, root=protected) + " " + writer}
    denied = await a._authorize_action("bash", args, tool_policy.SHELL_POLICY, workspace=plain)
    assert denied and "[jobs-file]" in denied[0]
    assert sent == []
    assert a._active_tool_profile == AUTONOMOUS
    assert seat_authority.jobs_file_refusal(_ryans(a), args, plain) is None


@pytest.mark.parametrize("name", GIT_ENV_NAMES)
def test_git_env_inherited_protected_root_is_relocation(tmp_path, monkeypatch, name):
    a, _ = _agent(tmp_path, monkeypatch)
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / ".litetui-data.json").write_text("{}")
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.chdir(plain)
    target = protected / (".git/index" if name == "GIT_INDEX_FILE" else ".git")
    monkeypatch.setenv(name, str(target))
    args = {"command": "git clean -fx -- jobs.json"}
    assert seat_authority.jobs_file_refusal(a, args, plain) is not None
    assert seat_authority.jobs_file_refusal(a, {"command": "git add notes.md"}, plain) is not None
    assert seat_authority.jobs_file_refusal(a, {"command": "git status"}, plain) is None
    assert seat_authority.jobs_file_refusal(_ryans(a), args, plain) is None
    monkeypatch.setenv(name, str(plain / ".git"))
    assert seat_authority.jobs_file_refusal(a, {"command": "git add notes.md"}, plain) is None


@pytest.mark.parametrize("name", GIT_ENV_NAMES)
def test_git_env_submission_wide_taint_and_read_control(tmp_path, monkeypatch, name):
    a, _ = _agent(tmp_path, monkeypatch)
    assignment = f"set {name}={tmp_path}"
    assert seat_authority.jobs_file_refusal(a, {"command": f"git add notes.md; {assignment}"}, tmp_path) is not None
    assert seat_authority.jobs_file_refusal(a, {"command": f"{assignment}; git status"}, tmp_path) is None


@pytest.mark.asyncio
async def test_ryans_manual_autonomy_choice_is_not_capped(tmp_path, monkeypatch):
    a, sent = _agent(tmp_path, monkeypatch)
    _ryans(a)
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.settings.tool_policy_profile = tool_policy.STRICT
    a._active_tool_profile = tool_policy.STRICT
    saved = []
    monkeypatch.setattr(m.settings_runtime, "persist_or_raise",
                        lambda app, settings: saved.append(settings.tool_policy_profile))
    async with a.run_test(size=(120, 45)) as pilot:
        await pilot.pause()
        await pilot.press("shift+tab")
        await pilot.pause()
        assert a.settings.tool_policy_profile == AUTONOMOUS
        assert a._active_tool_profile == AUTONOMOUS
    assert saved == [AUTONOMOUS]
    assert sent == []


def test_ryans_scheduler_save_and_edit_remain_untouched(tmp_path, monkeypatch):
    a, sent = _agent(tmp_path, monkeypatch)
    _ryans(a)
    job = scheduler.Job(prompt="first", schedule="@daily", tool_profile=AUTONOMOUS)
    scheduler.save([job], tmp_path)
    rows = scheduler.load(tmp_path)
    assert rows[0].prompt == "first"
    rows[0].prompt = "edited"
    scheduler.save(rows, tmp_path)
    edited = scheduler.load(tmp_path)
    assert edited[0].prompt == "edited"
    assert edited[0].tool_profile == AUTONOMOUS
    assert a._active_tool_profile == AUTONOMOUS
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("verb", ["Set-Content", "Add-Content"])
async def test_quoted_separator_writer_is_refused_by_shipped_gate(tmp_path, monkeypatch, verb):
    a, sent = _agent(tmp_path, monkeypatch)
    args = {"command": f'{verb} -Value "x;git status --porcelain" -Path {tmp_path}/jobs.json'}
    denied = await a._authorize_action("bash", args, tool_policy.SHELL_POLICY, workspace=tmp_path)
    assert denied, "writer was allowed"
    assert sent == []
    assert a._active_tool_profile == AUTONOMOUS
    assert seat_authority.jobs_file_refusal(_ryans(a), args, tmp_path) is None


GIT_PAYLOAD_WRITERS = [
    'Set-Content {root}/jobs.json "git status --porcelain"',
    'Add-Content {root}/jobs.json "git status --porcelain"',
    '"git status --porcelain" | Out-File {root}/jobs.json',
    'echo "git status --porcelain" > {root}/jobs.json',
    'echo "git status --porcelain" >> {root}/jobs.json',
    'echo "git status --porcelain" | tee {root}/jobs.json',
    'printf "%s" "git status --porcelain" > {root}/jobs.json',
]


@pytest.mark.asyncio
@pytest.mark.parametrize("template", GIT_PAYLOAD_WRITERS)
async def test_writer_payload_cannot_acquire_git_reader_exemption(tmp_path, monkeypatch, template):
    a, sent = _agent(tmp_path, monkeypatch)
    args = {"command": template.format(root=tmp_path)}
    denied = await a._authorize_action("bash", args, tool_policy.SHELL_POLICY, workspace=tmp_path)
    assert denied and "T1085" in denied[0], "non-Git writer keeps its original seat refusal reason"
    assert sent == []
    assert a._active_tool_profile == AUTONOMOUS
    assert seat_authority.jobs_file_refusal(_ryans(a), args, tmp_path) is None


T0116_LITERAL_WRITERS = [
    'cd . > {root}/jobs.json',
    'echo x >& {root}/jobs.json',
    '(rm {root}/jobs.json)',
    'echo x > >(tee {root}/jobs.json)',
    'cat <(rm {root}/jobs.json)',
    "rm $'{root}/jobs.json'",
    'git -C{root} reset --hard',
    'bash -c "rm {root}/jobs.json"',
]

@pytest.mark.asyncio
@pytest.mark.parametrize("template", T0116_LITERAL_WRITERS)
async def test_t0116_literal_boundary_writers_shipped_gate(tmp_path, monkeypatch, template):
    a, sent = _agent(tmp_path, monkeypatch)
    args = {"command": template.format(root=tmp_path.as_posix())}
    denied = await a._authorize_action("bash", args, tool_policy.SHELL_POLICY, workspace=tmp_path)
    assert denied, "literal writer was allowed"
    assert sent == []
    assert seat_authority.jobs_file_refusal(_ryans(a), args, tmp_path) is None


@pytest.mark.asyncio
async def test_t0116_named_copy_read_write_owner_matrix(tmp_path, monkeypatch):
    a, sent = _agent(tmp_path, monkeypatch)
    read = {"command": "Copy-Item -Destination backup.json -Path jobs.json"}
    assert await a._authorize_action("powershell", read, tool_policy.SHELL_POLICY, workspace=tmp_path) is None
    write = {"command": "Copy-Item -Path backup.json -Destination jobs.json"}
    refused = await a._authorize_action("powershell", write, tool_policy.SHELL_POLICY, workspace=tmp_path)
    assert refused and "T1085" in refused[0]
    assert seat_authority.jobs_file_refusal(_ryans(a), write, tmp_path) is None
    assert sent == []


@pytest.mark.asyncio
async def test_t0116_jobs_small_nested_shell_shipped_host(tmp_path, monkeypatch):
    import shlex
    a, sent = _agent(tmp_path, monkeypatch)
    command = f"rm {tmp_path.as_posix()}/jobs.json"
    for _ in range(3):
        command = "bash -c " + shlex.quote(command)
    assert len(command) < 4096
    args = {"command": command}
    assert await a._authorize_action("bash", args, tool_policy.SHELL_POLICY, workspace=tmp_path)
    assert seat_authority.jobs_file_refusal(_ryans(a), args, tmp_path) is None
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("head", ["/bin/bash", "env bash", "sudo bash"])
async def test_t0116_review_heredoc_protected_writer_actual_host(tmp_path, monkeypatch, head):
    a, sent = _agent(tmp_path, monkeypatch)
    command = head + " <<'EOF'\nrm " + tmp_path.as_posix() + "/jobs.json\nEOF"
    args = {"command": command}
    result = await a._authorize_action("bash", args, tool_policy.SHELL_POLICY, workspace=tmp_path)
    print("RECEIPT HOST", repr(command), repr(result))
    assert result, "protected writer allowed"
    assert sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("depth", [3, 18])
async def test_t0116_review2_jobs_linear_depth_actual_host(tmp_path, monkeypatch, depth):
    a, sent = _agent(tmp_path, monkeypatch)
    command = "echo " + "$(" * depth + "rm " + tmp_path.as_posix() + "/jobs.json" + ")" * depth
    assert len(command) < 4096
    out = await a._authorize_action("bash", {"command": command}, tool_policy.SHELL_POLICY, workspace=tmp_path)
    print("REVIEW2 HOST DEPTH", depth, repr(out))
    assert out and sent == []
