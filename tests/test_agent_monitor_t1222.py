"""T1222: the agent-callable `monitor` tool (litetui/agent_monitor.py).

Core behaviour is driven with real threads, real files and real child processes
at shrunk timings; the last test builds a real owned app and proves the event
crosses onto the UI loop through `_deliver_inbox`, the funnel a finished
background task uses.
"""
import os
import sys
import threading
import time

import pytest
from _owned_seat import owned_app

from litetui import agent_monitor as am
from litetui import paths, tool_policy, tool_schemas
from litetui.app import LiteTUI

PY = sys.executable


def py_argv(command):
    # `command` is Python source here, so the tests need no shell quoting.
    return [PY, "-u", "-c", command], False


def wait_for(cond, timeout=15.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(am, "BATCH_S", 0.05)
    monkeypatch.setattr(am, "POLL_S", 0.03)
    monkeypatch.setattr(am, "PARTIAL_FLUSH_S", 0.2)


@pytest.fixture
def make(tmp_path, fast):
    made = []

    def build(**kw):
        out = []
        state = {"convo": "c1"}
        service = am.MonitorService(
            deliver=kw.pop("deliver", out.append), convo_id=lambda: state["convo"],
            running=kw.pop("running", lambda: True), argv=py_argv, data_root=lambda: tmp_path, **kw)
        made.append(service)
        return service, out, state

    yield build
    for service in made:
        service.stop_all()


def mon_of(service, text):
    return service._monitors[text.split()[1]]


def bodies(out):
    return "\n".join(out)


# ── command mode ────────────────────────────────────────────────────────────

def test_lines_within_the_batch_window_are_one_event_and_exit_is_reported(make):
    service, out, _ = make()
    text = service.run({"command": "print('a'); print('b'); print('c')"})
    mon = mon_of(service, text)
    assert wait_for(lambda: mon.state == am.ENDED)
    assert len(out) == 2, out
    event, notice = out
    assert event.startswith("[monitor m-1 · command ·") and "3 lines" in event
    assert event.splitlines()[1:] == ["a", "b", "c"]
    assert "ended: the command exited with exit code 0" in notice


def test_stderr_lines_are_events_too(make):
    service, out, _ = make()
    mon = mon_of(service, service.run({"command": "import sys; print('boom', file=sys.stderr)"}))
    assert wait_for(lambda: mon.state == am.ENDED)
    assert "boom" in bodies(out)


def test_start_text_names_a_log_that_exists_and_ends_up_with_every_line(make):
    service, out, _ = make()
    text = service.run({"command": "print('one'); print('two')"})
    mon = mon_of(service, text)
    assert mon.log_path.as_posix() in text and mon.log_path.is_file()
    assert wait_for(lambda: mon.state == am.ENDED)
    assert mon.log_path.read_text(encoding="utf-8").splitlines() == ["one", "two"]


def test_an_event_cut_for_length_names_the_log_with_the_rest(make):
    service, out, _ = make()
    mon = mon_of(service, service.run({"command": "[print('line', i) for i in range(100)]"}))
    assert wait_for(lambda: mon.state == am.ENDED)
    event = out[0]
    assert f"{100 - am.EVENT_MAX_LINES} more line(s) cut" in event
    assert mon.log_path.as_posix() in event
    assert len(mon.log_path.read_text(encoding="utf-8").splitlines()) == 100


def test_a_command_runs_without_a_console_window_and_no_stdin(make, monkeypatch):
    seen = {}
    real = am.ttyguard.popen

    def spy(*args, **kwargs):
        seen.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(am.ttyguard, "popen", spy)
    service, out, _ = make()
    mon = mon_of(service, service.run({"command": "import sys; print(sys.stdin.read() == '')"}))
    assert wait_for(lambda: mon.state == am.ENDED)
    assert seen["stdin"] is am.subprocess.DEVNULL and seen["kill_on_close"] is True
    assert "True" in bodies(out)


# ── file mode ───────────────────────────────────────────────────────────────

def test_a_file_that_exists_yields_only_lines_appended_after_arming(make, tmp_path):
    target = tmp_path / "w.log"
    target.write_bytes(b"old\n")  # write_text would make it CRLF on Windows: 5 bytes
    service, out, _ = make()
    text = service.run({"path": str(target)})
    assert "exists, 4 bytes" in text
    time.sleep(0.3)
    assert out == []
    with target.open("a", encoding="utf-8") as fh:
        fh.write("new\n")
    assert wait_for(lambda: out)
    assert "new" in out[0] and "old" not in out[0]


def test_a_line_appended_right_after_the_call_is_not_missed(make, tmp_path):
    # The offset is taken when the tool is CALLED, not when the poll thread first runs.
    target = tmp_path / "w.log"
    target.write_text("old\n", encoding="utf-8")
    service, out, _ = make()
    service.run({"path": str(target)})
    with target.open("a", encoding="utf-8") as fh:
        fh.write("immediately\n")
    assert wait_for(lambda: out)
    assert "immediately" in out[0]


def test_a_file_that_does_not_exist_yet_is_watched_from_its_first_byte(make, tmp_path):
    target = tmp_path / "later.log"
    service, out, _ = make()
    assert "not created yet" in service.run({"path": str(target)})
    time.sleep(0.2)
    target.write_text("first\nsecond\n", encoding="utf-8")
    assert wait_for(lambda: out)
    assert out[0].splitlines()[1:] == ["first", "second"]


def test_rotation_by_replace_restarts_at_the_new_file(make, tmp_path):
    target = tmp_path / "r.log"
    target.write_text("a long first generation of the log\n" * 3, encoding="utf-8")
    service, out, _ = make()
    service.run({"path": str(target)})
    fresh = tmp_path / "r.log.new"
    fresh.write_text("fresh\n", encoding="utf-8")
    os.replace(fresh, target)
    assert wait_for(lambda: out)
    assert out[0].splitlines()[1:] == ["fresh"]


def test_truncation_restarts_at_zero(make, tmp_path):
    target = tmp_path / "t.log"
    target.write_text("x" * 200 + "\n", encoding="utf-8")
    service, out, _ = make()
    service.run({"path": str(target)})
    target.write_text("after truncate\n", encoding="utf-8")
    assert wait_for(lambda: out)
    assert out[0].splitlines()[1:] == ["after truncate"]


def test_a_trailing_line_with_no_newline_is_flushed_once_the_file_is_quiet(make, tmp_path):
    target = tmp_path / "p.log"
    target.write_text("", encoding="utf-8")
    service, out, _ = make()
    service.run({"path": str(target)})
    with target.open("a", encoding="utf-8") as fh:
        fh.write("READY")
    assert wait_for(lambda: out)
    assert out[0].splitlines()[1:] == ["READY"]


def test_an_in_place_rewrite_with_no_growth_is_reported_once_as_modified(make, tmp_path):
    target = tmp_path / "m.json"
    target.write_text("aaaa\n", encoding="utf-8")
    service, out, _ = make()
    service.run({"path": str(target)})
    with target.open("r+b") as fh:
        fh.write(b"bbbb")
    st = target.stat()
    os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert wait_for(lambda: out)
    assert "[modified:" in out[0]
    time.sleep(0.3)
    assert len(out) == 1


def test_a_directory_is_refused(make, tmp_path):
    service, _, _ = make()
    assert service.run({"path": str(tmp_path)}).startswith("[error]")


# ── lifetime, flood, stop, scope ────────────────────────────────────────────

def test_timeout_gives_one_notice_and_kills_the_child(make, monkeypatch):
    monkeypatch.setattr(am, "MIN_TIMEOUT_MS", 50)
    service, out, _ = make()
    mon = mon_of(service, service.run({"command": "import time; time.sleep(60)", "timeout_ms": 150}))
    assert wait_for(lambda: mon.state == am.TIMEOUT)
    assert wait_for(lambda: mon.proc.poll() is not None)
    assert len(out) == 1 and "timed out" in out[0]


def test_default_and_clamped_timeouts(make):
    service, _, _ = make()
    first = service.run({"command": "import time; time.sleep(30)"})
    assert "timeout 300 s" in first
    second = service.run({"command": "import time; time.sleep(30)", "timeout_ms": 10**9})
    assert "timeout 3600 s" in second
    third = service.run({"command": "import time; time.sleep(30)", "timeout_ms": 5})
    assert "timeout 1 s" in third


def test_flood_guard_stops_the_monitor_and_says_so(make, monkeypatch):
    monkeypatch.setattr(am, "FLOOD_MAX_EVENTS", 3)
    monkeypatch.setattr(am, "FLOOD_WINDOW_S", 60.0)
    service, out, _ = make()
    cmd = "import time\nfor i in range(200):\n    print(i)\n    time.sleep(0.08)"
    mon = mon_of(service, service.run({"command": cmd}))
    assert wait_for(lambda: mon.state == am.FLOOD)
    assert wait_for(lambda: mon.proc.poll() is not None)
    assert mon.events == 3
    assert "flood guard" in out[-1] and len(out) == 4


def test_stop_kills_the_child_and_sends_no_notice(make):
    service, out, _ = make()
    text = service.run({"command": "import time; print('up', flush=True); time.sleep(60)"})
    mon = mon_of(service, text)
    assert wait_for(lambda: out)
    reply = service.run({"action": "stop", "id": "m-1"})
    assert reply.startswith("m-1 stopped")
    assert wait_for(lambda: mon.proc.poll() is not None)
    assert wait_for(lambda: mon.state == am.STOPPED)
    assert len(out) == 1
    assert service.run({"action": "stop", "id": "m-1"}).startswith("m-1 already stopped")


def test_a_conversation_change_ends_the_watch_quietly(make, tmp_path):
    target = tmp_path / "c.log"
    target.write_text("", encoding="utf-8")
    service, out, state = make()
    mon = mon_of(service, service.run({"path": str(target)}))
    state["convo"] = "c2"
    assert wait_for(lambda: mon.state == am.LOST)
    with target.open("a", encoding="utf-8") as fh:
        fh.write("late\n")
    time.sleep(0.3)
    assert out == []
    assert service.run({"action": "list"}) == "no monitors"  # c2 does not see c1's


def test_the_app_going_away_ends_every_watch(make, tmp_path):
    target = tmp_path / "q.log"
    target.write_text("", encoding="utf-8")
    flag = {"up": True}
    service, out, _ = make(running=lambda: flag["up"])
    mon = mon_of(service, service.run({"path": str(target)}))
    flag["up"] = False
    assert wait_for(lambda: mon.state == am.LOST)


def test_a_delivery_that_raises_ends_the_watch(make, tmp_path):
    target = tmp_path / "d.log"
    target.write_text("", encoding="utf-8")

    def boom(_text):
        raise RuntimeError("seat gone")

    service, _, _ = make(deliver=boom)
    mon = mon_of(service, service.run({"path": str(target)}))
    with target.open("a", encoding="utf-8") as fh:
        fh.write("x\n")
    assert wait_for(lambda: mon.state == am.LOST)


def test_list_shows_state_and_the_active_cap_holds(make, tmp_path):
    service, _, _ = make()
    for i in range(am.MAX_ACTIVE):
        assert service.run({"path": str(tmp_path / f"f{i}.log")}).startswith("[monitor m-")
    assert service.run({"path": str(tmp_path / "extra.log")}).startswith("[error]")
    listing = service.run({"action": "list"})
    assert len(listing.splitlines()) == am.MAX_ACTIVE and "running" in listing


@pytest.mark.parametrize("args", [
    {}, {"command": "x", "path": "y"}, {"action": "bogus"}, {"action": "stop"},
    {"action": "stop", "id": "m-99"}, {"command": "x", "timeout_ms": "soon"},
])
def test_bad_arguments_are_errors_not_monitors(make, args):
    service, _, _ = make()
    assert service.run(args).startswith("[error]")
    assert service.run({"action": "list"}) == "no monitors"


# ── authority and contract ──────────────────────────────────────────────────

def test_a_file_watch_is_a_read_and_a_command_is_process_execution():
    ws = paths.ROOT
    assert am.POLICY.classify({"path": "x.log"}, ws) == {tool_policy.READ_ONLY}
    assert am.POLICY.classify({"action": "list"}, ws) == {tool_policy.READ_ONLY}
    assert tool_policy.PROCESS_EXECUTION in am.POLICY.classify({"command": "tail -f x.log"}, ws)
    assert tool_policy.DESTRUCTIVE_IRREVERSIBLE in am.POLICY.classify({"command": "rm -rf /"}, ws)


@pytest.mark.parametrize("profile", sorted(tool_policy.PROFILES))
@pytest.mark.parametrize("command", ["tail -f x.log", "rm -rf /", "Remove-Item -Recurse -Force C:/Windows"])
def test_a_command_monitor_is_never_more_permitted_than_bash(profile, command):
    """The monitor runs the same command bash would, so it gets the same answer
    from every profile, and the deny floor (which reads `command` for any policy)
    refuses what it refuses for bash."""
    ws = paths.ROOT
    rank = {tool_policy.DENY: 0, tool_policy.CONFIRM: 1, tool_policy.ALLOW: 2}
    mine = tool_policy.evaluate(profile, am.POLICY, {"command": command}, ws, tool_name="monitor")
    bash = tool_policy.evaluate(profile, tool_policy.SHELL_POLICY, {"command": command}, ws, tool_name="bash")
    assert rank[mine.action] <= rank[bash.action], (mine, bash)


def test_a_file_watch_is_allowed_where_a_read_is_and_a_command_where_a_shell_is():
    ws = paths.ROOT
    strict_read = tool_policy.evaluate(tool_policy.STRICT, am.POLICY, {"path": "x.log"}, ws, tool_name="monitor")
    assert strict_read.action == tool_policy.ALLOW
    strict_run = tool_policy.evaluate(tool_policy.STRICT, am.POLICY, {"command": "tail -f x.log"}, ws, tool_name="monitor")
    assert strict_run.action != tool_policy.ALLOW


def test_the_schema_matches_what_the_service_enforces():
    spec = tool_schemas.load("monitor")["function"]
    props = spec["parameters"]["properties"]
    assert spec["name"] == "monitor" and spec["parameters"]["additionalProperties"] is False
    assert props["timeout_ms"]["minimum"] == am.MIN_TIMEOUT_MS
    assert props["timeout_ms"]["maximum"] == am.MAX_TIMEOUT_MS
    assert "300000" in spec["description"] and str(am.MAX_ACTIVE) in spec["description"]
    assert str(am.FLOOD_MAX_EVENTS) in spec["description"]
    assert set(props["action"]["enum"]) == {"start", "stop", "list"}


# ── the real app ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_file_event_reaches_the_ui_loop_through_the_inbox_funnel(tmp_path, monkeypatch):
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(paths, "CONVO_DIR", tmp_path / ".convos")
    monkeypatch.setattr(LiteTUI, "connect", lambda self: None)
    monkeypatch.setattr(am, "POLL_S", 0.03)
    monkeypatch.setattr(am, "BATCH_S", 0.05)
    app = owned_app()
    app._connect = lambda: None
    app._active_tool_profile = tool_policy.AUTONOMOUS
    got, loop_thread = [], {}
    app._deliver_inbox = lambda message: got.append((message, threading.get_ident()))
    target = tmp_path / "watched.log"
    target.write_text("", encoding="utf-8")
    async with app.run_test(size=(100, 35)) as pilot:
        loop_thread["id"] = threading.get_ident()
        assert "monitor" in [s["function"]["name"] for s in app.plugins.tool_specs()]
        result, ok = await app._execute_tool("monitor", {"path": str(target), "label": "proof"})
        assert ok and result.startswith("[monitor m-1 started")
        with target.open("a", encoding="utf-8") as fh:
            fh.write("wake up\n")
        for _ in range(200):
            if got:
                break
            await pilot.pause(0.05)
        assert got, "no event reached _deliver_inbox"
        message, thread = got[0]
        assert message["from"] == "monitor" and "wake up" in message["body"]
        assert thread == loop_thread["id"], "delivery must run on the UI loop, not the monitor thread"
        listing, _ = await app._execute_tool("monitor", {"action": "list"})
        assert "m-1" in listing and "proof" in listing
        app.agent_monitors.stop_all()
