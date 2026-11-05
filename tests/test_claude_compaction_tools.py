"""Reviewed Claude compaction with tools and a wake: the pure rules the process scenarios rely on.

The real end-to-end cases live in tests/test_rust_compact_process.py (claude-segment-*); these pin the
individual decisions: what a review binds, what a summary turn may do, who may speak for the root
execution, and what the audit tolerance admits.
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace as NS

import pytest

from litetui import claude_compaction_evidence as evidence
from litetui import conversation_action as action
from litetui import history_projection as projection
from litetui.claude_tools import NATIVE_TOOLS, ClaudeTools
from litetui.event_origin import EventOrigin

ROOT = "11111111-1111-4111-8111-111111111111"
WAKE = "22222222-2222-4222-8222-222222222222"
OP = "compact-op"
SESSION = "33333333-3333-4333-8333-333333333333"


def app_with(tools_enabled):
    return NS(tools_enabled=tools_enabled, convo_id=SESSION)


def options(tools=(), servers=None, strict=True, mode=None):
    return NS(tools=list(tools), mcp_servers={} if servers is None else servers, strict_mcp_config=strict,
              permission_mode=mode)


# ── what a review binds ──────────────────────────────────────────────────────────────────────

def test_tools_off_binds_an_empty_inventory_and_tools_on_binds_what_the_session_offers():
    off = evidence._tool_options(app_with(False), options(), rewrite=False)
    assert off == {"tools_enabled": False, "tools": [], "mcp": [], "strict_mcp_config": True, "permission_mode": None}
    on = evidence._tool_options(app_with(True), options(["Write", "Edit"], {"litetui": object()}, mode="default"),
                                rewrite=False)
    assert on["tools"] == ["Edit", "Write"] and on["mcp"] == ["litetui"] and on["permission_mode"] == "default"


@pytest.mark.parametrize(("enabled", "opts", "rewrite"), [
    (False, options(["Write"]), False),                       # tools off, yet the session still offers tools
    (False, options(servers={"litetui": 1}), False),
    (True, options(["Write"]), True),                         # Edit/Retry forks keep their tools-off rule
    (True, options(["NotAToolWeKnow"]), False),               # an inventory LiteTUI cannot attribute
    (True, options(["Write"], {"somebody-elses-server": 1}), False),
    (True, options(["Write"], strict=False), False),
    (True, NS(tools="all", mcp_servers={}, strict_mcp_config=True), False),
])
def test_a_session_the_review_cannot_describe_is_refused(enabled, opts, rewrite):
    with pytest.raises(ValueError):
        evidence._tool_options(app_with(enabled), opts, rewrite=rewrite)


DIGEST = "d" * 64


def test_a_review_discloses_the_host_tools_the_session_bridges_and_binds_their_schemas():
    on = evidence._tool_options(app_with(True), options(["Write"], {"litetui": object()}), rewrite=False,
                                host={"view_image": "e" * 64, "convo_search": DIGEST})
    assert on["host_tools"] == [["convo_search", DIGEST], ["view_image", "e" * 64]]
    none = evidence._tool_options(app_with(True), options(["Write"]), rewrite=False, host={})
    assert "host_tools" not in none, "a session with no bridged host tools binds exactly what it did before"
    changed = evidence._tool_options(app_with(True), options(["Write"], {"litetui": object()}), rewrite=False,
                                     host={"convo_search": "f" * 64, "view_image": "e" * 64})
    assert evidence._digest(on) != evidence._digest(changed), "a different schema is a different review"


@pytest.mark.parametrize(("enabled", "opts", "rewrite", "host"), [
    (True, options(["Write"]), False, {"convo_search": DIGEST}),                      # tools bridged, no server declared
    (True, options(["Write"], {"litetui": 1}), False, {}),                           # server declared, nothing bridged
    (True, options(["Write"], {"litetui": 1}), False, {"not_a_host_tool": DIGEST}),  # a name LiteTUI never bridges
    (True, options(["Write"], {"litetui": 1}), False, {"convo_search": "short"}),    # not a digest
    (False, options(), False, {"convo_search": DIGEST}),                              # tools off cannot bridge
    (True, options(["Write"], {"litetui": 1}), True, {"convo_search": DIGEST}),      # Edit/Retry keep tools off
])
def test_a_host_bridge_the_session_options_do_not_declare_is_refused(enabled, opts, rewrite, host):
    with pytest.raises(ValueError):
        evidence._tool_options(app_with(enabled), opts, rewrite=rewrite, host=host)


def test_the_plan_names_the_disclosed_host_tools_and_commits_to_their_digests():
    host = binding(host={"convo_search": DIGEST})
    plain = binding()
    assert host.plan(4)["host_tools"] == ["convo_search"] and plain.plan(4)["host_tools"] == []
    assert host.plan(4)["tools_sha256"] != plain.plan(4)["tools_sha256"]
    assert host.host_tools == {"convo_search": DIGEST} and plain.host_tools == {}


def binding(tools_enabled=True, tools=("Write",), mcp=(), wake=False, host=None):
    snapshot = {"tools_enabled": tools_enabled, "tools": list(tools), "mcp": list(mcp),
                "strict_mcp_config": True, "permission_mode": None}
    if host:
        snapshot["host_tools"] = [[name, host[name]] for name in sorted(host)]
    return evidence.Binding(None, None, None, 1, "a" * 32, SESSION, "w", {}, snapshot, wake)


def test_the_plan_fingerprints_the_tool_snapshot_and_a_changed_snapshot_refuses(monkeypatch):
    first, other = binding(), binding(tools=("Write", "Edit"))
    assert evidence._digest(first.tools) != evidence._digest(other.tools)
    monkeypatch.setattr(evidence.Binding, "capture", classmethod(lambda cls, app, rewrite=False: other))
    first_same_everything_else = replace(first, tools=other.tools, wake=True)
    with pytest.raises(ValueError, match="tools"):
        first_same_everything_else.check(None)  # wake differs even though the inventory matches
    with pytest.raises(ValueError, match="tools"):
        first.check(None)


def test_tool_work_needs_tools_on_and_something_offered():
    assert binding().tool_work
    assert not binding(tools_enabled=False, tools=()).tool_work
    assert not binding(tools=()).tool_work
    assert binding(tools=(), mcp=("litetui",)).tool_work


# ── what a summary turn may do ───────────────────────────────────────────────────────────────

def event(kind, tool_id="t1", name="Write", tool_input=None, complete=True, is_error=False):
    return NS(kind=kind, tool_id=tool_id, tool_name=name, tool_input=tool_input, complete=complete, is_error=is_error)


def test_a_tools_off_review_refuses_any_tool_event_and_every_review_refuses_native_compaction():
    off = evidence.Progress(binding(tools_enabled=False, tools=()))
    for kind in ("tool_use", "tool_result"):
        with pytest.raises(ValueError):
            off.tool(event(kind))
    on = evidence.Progress(binding())
    with pytest.raises(ValueError):
        on.tool(event("compaction"))


def test_only_a_write_whose_result_did_not_fail_becomes_a_store_file():
    progress = evidence.Progress(binding())
    for tool_id, name, path, failed in (("a", "Write", "C:/store/memory.md", False), ("b", "Edit", "C:/store/soul.md", True),
                                        ("c", "Read", "C:/store/handoff.md", False), ("d", "Write", "C:/store/memory.md", False)):
        progress.tool(event("tool_use", tool_id, name, {"file_path": path}))
        progress.tool(event("tool_result", tool_id, name, is_error=failed))
    assert progress.written == ["C:/store/memory.md", "C:/store/memory.md"]
    assert len(progress.tool_ids) == 4
    # A partial tool_use (input still streaming) is not an attempt yet.
    progress.tool(event("tool_use", "e", "Write", {"file_path": "C:/store/x.md"}, complete=False))
    assert "e" not in progress.attempted


def test_the_tool_call_budget_fails_the_compaction_instead_of_truncating_the_receipt():
    progress = evidence.Progress(binding())
    for number in range(evidence.MAX_TOOL_CALLS):
        progress.tool(event("tool_use", f"id{number}", "Read", {}))
    with pytest.raises(ValueError, match="budget"):
        progress.tool(event("tool_use", "one-too-many", "Read", {}))


@pytest.mark.parametrize("files", [[f"C:/store/{n}.md" for n in range(evidence.MAX_STORE_FILES + 1)],
                                   ["x" * (evidence.MAX_PATH_CHARS + 1)]])
def test_a_receipt_that_cannot_name_every_store_file_refuses(files):
    progress = evidence.Progress(binding())
    progress.written = files
    with pytest.raises(ValueError, match="store files"):
        progress.selected(None, {})


# ── who may speak for the root execution ─────────────────────────────────────────────────────

def origin(execution=ROOT, **changes):
    return replace(EventOrigin(session_id=SESSION, operation_id=OP, source="rpc", execution_id=execution), **changes)


def reviewed_app(*, kind="compact", root_finished=False, segments=None):
    attempt = action.Attempt(None, OP, None, kind=kind)
    attempt.execution = action.Execution(NS(is_finished=root_finished), origin(ROOT))
    attempt.segments = segments
    return NS(convo_id=SESSION, _conversation_rewrite_attempt=attempt), attempt


def test_the_live_root_may_speak_and_the_attribution_reaches_child_tasks_and_threads():
    app, _ = reviewed_app()

    async def scenario():
        with action.claude_event_scope(app, origin(ROOT)) as granted:
            assert granted == origin(ROOT)
            assert action.event_origin(app) == origin(ROOT)
            assert await asyncio.create_task(_origin_in_task(app)) == origin(ROOT)
            assert await asyncio.to_thread(action.event_origin, app) == origin(ROOT)
            # Attribution is not admission: the caller still has no execution authority.
            assert not action._authorized(app)
        assert action.event_origin(app) is None

    asyncio.run(scenario())


async def _origin_in_task(app):
    return action.event_origin(app)


def test_the_newest_wake_stream_may_speak_and_an_older_one_may_not():
    app, _ = reviewed_app(root_finished=True, segments=[WAKE])
    with action.claude_event_scope(app, origin(WAKE)) as granted:
        assert granted == origin(WAKE)
    older = "44444444-4444-4444-8444-444444444444"
    app, _ = reviewed_app(root_finished=True, segments=[older, WAKE])
    with action.claude_event_scope(app, origin(older)) as granted:
        assert granted is None


@pytest.mark.parametrize("change", [
    {"operation_id": "another-op"}, {"session_id": "55555555-5555-4555-8555-555555555555"},
    {"source": "typed"}, {"execution_id": None}, {"execution_id": "66666666-6666-4666-8666-666666666666"},
])
def test_a_foreign_origin_is_never_attributed(change):
    app, _ = reviewed_app()
    with action.claude_event_scope(app, origin(**change)) as granted:
        assert granted is None
        assert action.event_origin(app) is None


def test_a_retired_root_a_rewrite_action_or_no_origin_is_not_attributed():
    for app, given in ((reviewed_app(root_finished=True)[0], origin(ROOT)), (reviewed_app(kind="rewrite")[0], origin(ROOT)),
                       (reviewed_app()[0], None), (NS(convo_id=SESSION), origin(ROOT))):
        with action.claude_event_scope(app, given) as granted:
            assert granted is None


def test_a_request_opened_while_the_root_ran_still_closes_under_its_origin_after_the_root_retires():
    app, attempt = reviewed_app()
    with action.claude_event_scope(app, origin(ROOT)):
        attempt.execution.worker.is_finished = True
        assert action.event_origin(app) == origin(ROOT)
        attempt.pending = False
        assert action.event_origin(app) is None


def test_host_tools_are_refused_before_any_prompt_outside_the_reviewed_summary_turn():
    app, _ = reviewed_app()
    bridge = ClaudeTools.__new__(ClaudeTools)
    bridge.app = app
    bridge._lane = bridge._turn = None
    result = asyncio.run(bridge.host_call("chrome", {}, {}))
    assert result["isError"] and "reviewed compaction" in result["content"][0]["text"]
    assert action.compact_pending(app) and not action.compact_pending(NS())


# ── host tools of the reviewed summary turn ──────────────────────────────────────────────────

HOST_SPEC = {"name": "convo_search", "description": "Search conversations",
             "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}


class Lane:
    """A fake session: parks delivered requests for a fake worker, like the real events() queue."""

    def __init__(self, open_turn=True):
        self.queue, self.open_turn = asyncio.Queue(), open_turn

    def deliver(self, item):
        if not self.open_turn:
            return False
        self.queue.put_nowait(item)
        return True


def host_world(*, spec=HOST_SPEC, disclosed=None, live=None, execute=None):
    """A reviewed-compaction app with one bridge, one bound turn and a host lane."""
    app, attempt = reviewed_app()
    ran = []

    async def _execute(name, args):
        ran.append((name, dict(args), asyncio.current_task()))
        return "found it", True

    app.tools_enabled, app._stop_requested, app.settings = True, False, NS(tools_disabled=())
    app._pending_tool_images = []
    app._all_tools = lambda: [{"type": "function", "function": spec if live is None else live}]
    app._execute_tool = execute or _execute
    session = Lane()
    app.backend = NS(session=session, segment_id="seg")
    bridge = ClaudeTools(app, app.backend, "seg")
    turn = bridge.bind_turn(origin(ROOT))
    digest = claude_tools_digest(spec)
    lane = bridge.bind_host_lane(turn, {"convo_search": digest} if disclosed is None else disclosed)
    return NS(app=app, bridge=bridge, turn=turn, lane=lane, session=session, ran=ran, attempt=attempt)


def claude_tools_digest(spec):
    from litetui.claude_tools import spec_digest
    return spec_digest(spec)


async def worker_serves(world, count=1):
    """The reviewed worker: consume what the lane parked, as `_turn_events` does."""
    for _ in range(count):
        request = await world.session.queue.get()
        await world.bridge.serve_host_call(world.lane, request)


def test_a_disclosed_host_tool_runs_on_the_reviewed_worker_task_and_answers_the_sdk():
    async def scenario():
        world = host_world()
        worker = asyncio.create_task(worker_serves(world))
        sdk = asyncio.create_task(world.bridge.host_call("convo_search", {"query": "x"}, HOST_SPEC))
        result = await sdk
        await worker
        assert result == {"content": [{"type": "text", "text": "found it"}], "isError": False}
        (name, args, ran_in), = world.ran
        assert (name, args) == ("convo_search", {"query": "x"})
        assert ran_in is worker and ran_in is not sdk, "the tool must run on the worker's task, never the SDK's"
        assert not world.lane.pending

    asyncio.run(scenario())


def test_a_host_tool_the_review_did_not_disclose_never_runs():
    async def scenario():
        world = host_world(disclosed={})
        result = await world.bridge.host_call("convo_search", {"query": "x"}, HOST_SPEC)
        assert result["isError"] and "not part of the compaction review" in result["content"][0]["text"]
        assert result["is_error"] is True, "the SDK's tool wrapper reads is_error, so Claude sees a refusal as one"
        assert world.session.queue.empty() and not world.ran
        # Same name, different schema than the one the review hashed.
        other = {**HOST_SPEC, "description": "Search conversations, and also delete them"}
        world = host_world(spec=HOST_SPEC)
        result = await world.bridge.host_call("convo_search", {"query": "x"}, other)
        assert result["isError"] and world.session.queue.empty() and not world.ran

    asyncio.run(scenario())


def test_a_host_tool_whose_live_schema_changed_after_the_call_was_parked_is_denied_by_the_worker():
    async def scenario():
        changed = {**HOST_SPEC, "description": "A different tool now"}
        world = host_world(live=changed)
        worker = asyncio.create_task(worker_serves(world))
        result = await world.bridge.host_call("convo_search", {"query": "x"}, HOST_SPEC)
        await worker
        assert result["isError"] and "changed since the compaction review" in result["content"][0]["text"]
        assert not world.ran

    asyncio.run(scenario())


def test_host_calls_outside_the_bound_turn_or_after_it_ended_execute_nothing():
    async def scenario():
        world = host_world()
        world.bridge.finish_turn(world.turn)  # the summary turn is over
        result = await world.bridge.host_call("convo_search", {"query": "x"}, HOST_SPEC)
        assert result["isError"] and "summary turn" in result["content"][0]["text"] and not world.ran
        world = host_world()
        world.session.open_turn = False  # the session accepts nothing (idle or gone)
        result = await world.bridge.host_call("convo_search", {"query": "x"}, HOST_SPEC)
        assert result["isError"] and "not accepting" in result["content"][0]["text"] and not world.ran

    asyncio.run(scenario())


def test_ending_the_turn_answers_a_parked_host_call_without_running_it():
    async def scenario():
        world = host_world()
        sdk = asyncio.create_task(world.bridge.host_call("convo_search", {"query": "x"}, HOST_SPEC))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert world.lane.pending, "the call is parked for the worker"
        world.bridge.stop()  # Stop: the same door the stop watcher uses
        result = await asyncio.wait_for(sdk, 2)
        assert result["isError"] and "ended" in result["content"][0]["text"] and not world.ran
        # The stale request still sits in the session queue; the worker skips it.
        await worker_serves(world)
        assert not world.ran

    asyncio.run(scenario())


def test_a_cancelled_worker_still_answers_the_sdk_and_a_failing_tool_is_only_a_tool_result():
    async def scenario():
        started = asyncio.Event()

        async def hang(name, args):
            started.set()
            await asyncio.sleep(60)

        world = host_world(execute=hang)
        worker = asyncio.create_task(worker_serves(world))
        sdk = asyncio.create_task(world.bridge.host_call("convo_search", {"query": "x"}, HOST_SPEC))
        await started.wait()
        worker.cancel()
        assert (await sdk)["isError"]
        with pytest.raises(asyncio.CancelledError):
            await worker

        async def boom(name, args):
            raise OSError("disk on fire")

        world = host_world(execute=boom)
        worker = asyncio.create_task(worker_serves(world))
        result = await world.bridge.host_call("convo_search", {"query": "x"}, HOST_SPEC)
        await worker
        assert result["isError"] and "OSError" in result["content"][0]["text"]
        assert "disk on fire" not in result["content"][0]["text"]

    asyncio.run(scenario())


def test_the_sdk_giving_up_before_the_worker_reaches_a_call_runs_nothing():
    async def scenario():
        world = host_world()
        sdk = asyncio.create_task(world.bridge.host_call("convo_search", {"query": "x"}, HOST_SPEC))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        sdk.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sdk
        await worker_serves(world)
        assert not world.ran

    asyncio.run(scenario())


def test_the_session_hands_host_items_over_only_while_a_turn_is_open():
    from litetui.claude_session import ClaudeSession

    async def scenario():
        session = ClaudeSession(NS())
        assert not session.deliver("item")  # no owner
        session._owner = asyncio.create_task(asyncio.sleep(60))
        assert not session.deliver("item")  # owner, but no open turn
        session.lifecycle.active_turn = "turn"
        assert session.deliver("item") and session._events.get_nowait() == "item"
        session._owner.cancel()
        await asyncio.gather(session._owner, return_exceptions=True)
        assert not session.deliver("item")  # owner gone

    asyncio.run(scenario())


def test_every_native_tool_name_the_review_accepts_is_one_the_bridge_offers():
    assert "Write" in NATIVE_TOOLS and "Edit" in NATIVE_TOOLS


# ── what the audit tolerance admits ──────────────────────────────────────────────────────────

REVIEWED = b'{"type":"snapshot"}\n'
AUDIT = json.dumps({"type": "edit", "reason": action.AUDIT_REASON, "index": 3}).encode() + b"\n"


def test_only_approval_audit_edits_may_follow_the_reviewed_transcript():
    assert action.audit_raw_only(REVIEWED, REVIEWED)
    assert action.audit_raw_only(REVIEWED + AUDIT + AUDIT, REVIEWED)
    other = json.dumps({"type": "edit", "reason": "something else"}).encode() + b"\n"
    assert not action.audit_raw_only(REVIEWED + other, REVIEWED)
    assert not action.audit_raw_only(REVIEWED + b'{"type":"truncate"}\n', REVIEWED)
    assert not action.audit_raw_only(REVIEWED + b"{torn", REVIEWED)
    assert not action.audit_raw_only(b'{"type":"snapshot","x":1}\n' + AUDIT, REVIEWED)


def test_the_history_tolerance_strips_only_approval_metadata(tmp_path, monkeypatch):
    messages = [{"role": "user", "content": "one"}, {"role": "assistant", "content": "two"}]
    review = NS(conversation=messages, history=projection.encoded(messages), raw=REVIEWED)
    app = NS(conversation=messages, convo_path=tmp_path / "convo.jsonl")
    monkeypatch.setattr(action.native_history, "capture_file", lambda path: NS(raw=REVIEWED + AUDIT))
    assert action.audit_history_only(app, review)
    messages[0]["approval_delivery"] = {"appr-1": {"outcome": "approved"}}
    assert action.audit_history_only(app, review)
    messages[0]["content"] = "one, quietly rewritten"
    assert not action.audit_history_only(app, review)
    messages[0]["content"] = "one"
    app.conversation = list(messages)
    assert not action.audit_history_only(app, review), "a different list object is a different conversation"
