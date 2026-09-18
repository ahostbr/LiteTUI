"""Exercise actual host doors with harmless local scripts; no model service."""
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from litetui import hook_host, tool_policy
from litetui.app import LiteTUI
from litetui.lifecycle_hooks import event_document


@pytest.fixture
def app(monkeypatch):
    a = LiteTUI()
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a._resync_ctx_if_stale = lambda: None
    a._maybe_autocompact = lambda: None
    a._active_tool_profile = tool_policy.AUTONOMOUS
    a.settings.tool_policy_profile = tool_policy.AUTONOMOUS
    a.settings.tool_auto_background_s = 0
    a.tools_enabled = True
    a.messages = []
    a._system = a.messages.append
    a.jobs[:] = []
    return a


def configure(app, tmp_path, *, mode="gate", events=("tool_before",), code=None):
    script = tmp_path / "hook.py"
    script.write_text(code or 'import json,sys\njson.load(sys.stdin)\nprint(json.dumps({"decision":"deny","reason":"fix it"}))', encoding="utf-8")
    path = app.hook_config.project_path
    path.write_text(json.dumps({"version": 1, "hooks": [{"id": "check", "mode": mode,
        "events": list(events), "executable": sys.executable, "argv": [str(script)]}]}), encoding="utf-8")


def tool(app, fn, name="hook_probe", policy=tool_policy.READ_POLICY):
    app.plugins.add_tool("test", {"type": "function", "function": {"name": name,
        "description": "fixture", "parameters": {"type": "object", "properties": {}}}}, fn, policy=policy)


@pytest.mark.asyncio
@pytest.mark.parametrize("queued", [False, True])
async def test_mark_image_prompt_uses_conversation_profile(app, tmp_path, monkeypatch, queued):
    from litetui.convo_settings import ConvoSettings

    handoff = tmp_path / "mark.json"
    handoff.write_text(json.dumps({"x": 1, "y": 2, "mon": 0,
        "mon_x": 1, "mon_y": 2, "png": str(tmp_path / "fixture.png")}))
    monkeypatch.setattr("litetui.app.appsvc.load_image_file", lambda *args: "fixture-image")
    async with app.run_test(size=(110, 40)):
        app._materialise_convo()
        app._convo_settings = ConvoSettings(tool_policy_profile=tool_policy.INTERACTIVE)
        app.settings.tool_policy_profile = tool_policy.AUTONOMOUS
        assert app.chosen_tool_profile == tool_policy.INTERACTIVE
        app._stream = lambda: None
        app._chat_running = lambda: queued
        await app._mark_wait(handoff, None).wait()
        if queued:
            app._chat_running = lambda: False
            app._flush_pending_input()
        assert app._active_tool_profile == tool_policy.INTERACTIVE
        assert app.conversation[-1]["content"][0]["type"] == "image_url"


@pytest.mark.asyncio
async def test_gate_prevents_real_tool_side_effect_and_allows_recovery(app, tmp_path):
    target = tmp_path / "side-effect.txt"
    tool(app, lambda args: target.write_text("ran") or "ran")
    configure(app, tmp_path)
    result, ok = await app._execute_tool("hook_probe", {})
    assert not ok and "fix it" in result and not target.exists()
    assert not app._stop_requested
    configure(app, tmp_path, code='print(\'{"decision":"allow"}\')')
    result, ok = await app._execute_tool("hook_probe", {})
    assert ok and target.read_text() == "ran"


@pytest.mark.asyncio
async def test_hook_process_cannot_escape_effective_policy(app, tmp_path):
    configure(app, tmp_path, code='print(\'{"decision":"allow"}\')')
    calls = []
    tool(app, lambda args: calls.append(1))
    app._active_tool_profile = tool_policy.SCHEDULED
    result, ok = await app._execute_tool("hook_probe", {})
    assert not ok and not calls
    assert "profile" in result or "process_execution" in result


@pytest.mark.asyncio
async def test_human_hook_denial_stops_turn_but_test_panel_does_not(app, tmp_path, monkeypatch):
    from litetui.tool_approval import DENIED
    configure(app, tmp_path)
    tool(app, lambda args: "ran")
    async def refuse(*args, **kwargs):
        return DENIED
    monkeypatch.setattr("litetui.app.show_dialog", refuse)
    app._active_tool_profile = tool_policy.INTERACTIVE
    result, ok = await app._execute_tool("hook_probe", {})
    assert not ok and app._stop_requested
    app._stop_requested = False
    hook = app.hook_config.read("project")[0]
    before = list(app.conversation)
    result = await hook_host.invoke(app, hook, {"event": "tool_before"}, tool_policy.INTERACTIVE, testing=True)
    assert not result.allowed
    assert not app._stop_requested and app.conversation == before


@pytest.mark.asyncio
async def test_compaction_tool_and_wake_suppress_all_hooks(app, tmp_path):
    configure(app, tmp_path)
    calls = []
    tool(app, lambda args: calls.append(1) or "ran")
    assert (await app._execute_tool("hook_probe", {}, hooks_enabled=False))[1]
    app._hooks_suppressed = True
    assert (await app._execute_tool("hook_probe", {}))[1]
    assert await hook_host.completion(app, "done") == "allow"
    assert calls == [1, 1] and not app.hook_results


@pytest.mark.asyncio
async def test_actual_compact_caller_bypasses_hooks(app, tmp_path, monkeypatch):
    configure(app, tmp_path, events=["prompt_before", "tool_before", "completion_before"])
    calls = []
    tool(app, lambda args: calls.append(1) or "stored")
    app.model_id = "fixture"
    app.settings.compact_keep_recent = 0
    app.settings.wake_after_compact = False
    app.settings.clear_screen_after_compact = False
    async def ready(**kwargs):
        pass
    app._ensure_chat_ready = ready
    requests = []
    async def create(**kwargs):
        requests.append(kwargs)
        async def chunks():
            yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                content="summary" if len(requests) > 1 else None,
                reasoning_content=None, tool_calls=None if len(requests) > 1 else [SimpleNamespace(
                    index=0, id="fixture-call", function=SimpleNamespace(name="hook_probe", arguments="{}"))]))])
        return chunks()
    monkeypatch.setattr("litetui.app.model_transport.for_app", lambda a: SimpleNamespace(create=create))
    async with app.run_test(size=(110, 40)):
        app._materialise_convo()
        for role in ("user", "assistant", "user"):
            app._append({"role": role, "content": "old context"})
        worker = app._compact()
        await worker.wait()
        assert calls == [1]
        assert not app.hook_results
        app._stream = lambda: None
        app.settings.wake_after_compact = True
        app._wake_after_compact()
        assert app._hooks_suppressed
        assert not app.hook_results


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["typed", "queued", "interrupted", "rpc", "scheduled", "harness"])
async def test_prompt_admission_rejects_once_and_retains_input(app, tmp_path, source):
    configure(app, tmp_path, events=["prompt_before"])
    before = list(app.conversation)
    assert not await hook_host.admit_prompt(app, {"content": "blocked", "source": source,
                                                "tool_profile": tool_policy.AUTONOMOUS})
    assert app.conversation == before
    assert len(app.rejected_prompts) == 1
    assert app.rejected_prompts[0]["source"] == source


@pytest.mark.asyncio
async def test_completion_cap_and_internal_prompt_bypass(app, tmp_path):
    configure(app, tmp_path, events=["completion_before", "prompt_before"])
    assert [await hook_host.completion(app, "draft") for _ in range(4)] == ["retry", "retry", "retry", "pause"]
    assert app._hook_corrections == 3
    assert not app.rejected_prompts
    assert sum(str(m.get("content", "")).startswith("[internal hook correction]") for m in app.conversation) == 3


@pytest.mark.asyncio
async def test_background_after_only_when_work_finishes(app, tmp_path, monkeypatch):
    from litetui import tasks
    event_file = tmp_path / "events.jsonl"
    configure(app, tmp_path, mode="observe", events=["tool_before", "tool_after"],
        code=f'import json,sys\ne=json.load(sys.stdin)\nwith open({str(event_file)!r},"a") as f: f.write(json.dumps(e)+"\\n")')
    release = threading.Event()
    tool(app, lambda args: release.wait(5) and "finished")
    monkeypatch.setattr(tasks, "backgroundable", lambda name: True)
    app._deliver_inbox = lambda msg: None
    async with app.run_test(size=(110, 40)) as pilot:
        _result, ok = await app._execute_tool("hook_probe", {"background": True})
        assert ok
        assert [json.loads(s)["event"] for s in event_file.read_text().splitlines()] == ["tool_before"]
        release.set()
        for _ in range(80):
            if event_file.exists() and len(event_file.read_text().splitlines()) == 2:
                break
            await pilot.pause(.05)
        assert [json.loads(s)["event"] for s in event_file.read_text().splitlines()] == ["tool_before", "tool_after"]


def test_event_total_text_bound_and_image_omission(tmp_path):
    payload = event_document("prompt_before", tmp_path,
        {"a": "a" * 200000, "b": "b" * 200000, "image": "data:image/png;base64,SECRETIMAGE"})
    assert len(payload["data"]["a"]) + len(payload["data"]["b"]) == 256 * 1024
    assert "SECRETIMAGE" not in json.dumps(payload)
    assert payload["truncation"]


@pytest.mark.asyncio
async def test_lifecycle_transitions_and_graceful_shutdown_once(app, tmp_path):
    event_file = tmp_path / "lifecycle.jsonl"
    configure(app, tmp_path, mode="observe", events=["app_start", "app_shutdown",
        "conversation_start", "conversation_leave", "conversation_resume"],
        code=f'import json,sys\ne=json.load(sys.stdin)\nwith open({str(event_file)!r},"a") as f: f.write(json.dumps(e)+"\\n")')
    async with app.run_test(size=(110, 40)):
        await hook_host.drain_lifecycle(app)
        assert [json.loads(s)["event"] for s in event_file.read_text().splitlines()] == ["app_start"]
        assert app.store.pending
        app._materialise_convo()
        path = app.convo_path
        app._append({"role": "user", "content": "saved"})
        app._materialise_convo()  # idempotent
        app._new_convo()
        app._resume(path)
        await hook_host.drain_lifecycle(app)
    events = [json.loads(s)["event"] for s in event_file.read_text().splitlines()]
    assert events == ["app_start", "conversation_start", "conversation_leave",
                      "conversation_resume", "conversation_leave", "app_shutdown"]


@pytest.mark.asyncio
async def test_external_producers_reach_admission_once(app, monkeypatch, tmp_path):
    from litetui import paths, rpc, scheduler
    monkeypatch.setattr(paths, "data_root", lambda: tmp_path)
    scheduler.save([], tmp_path)
    received = []
    monkeypatch.setattr(hook_host, "start_prompt", lambda a, item: received.append(item))
    monkeypatch.setattr(rpc, "_respond", lambda *args, **kwargs: None)
    app._user_bubble = lambda *args, **kwargs: None
    app._chat_running = lambda: False
    async with app.run_test(size=(110, 40)):
        app._submit_text("typed", False)
        rpc._dispatch(app, {"type": "prompt", "message": "rpc"})
        app._deliver_inbox({"from": "fixture", "body": "mail"})
        # Shared schedulers reread the durable candidate under their lease so
        # a sibling's deletion cannot fire stale work. Persist a real job;
        # an in-memory fake depended on whether the checkout had jobs.json.
        job = scheduler.Job(label="fixture", id="fixture", prompt="scheduled",
                            schedule="* * * * *")
        app.jobs.append(job)
        scheduler.save(app.jobs, tmp_path)
        app._fire_job(job)
        # Scheduled prompts enter the same admission door exactly once, just
        # like typed, RPC and harness prompts; keep the exact four sources.
        assert [item["source"] for item in received] == ["typed", "rpc", "harness", "scheduled"]
        assert scheduler.load(tmp_path)[0].run_count == 1
        app._chat_running = lambda: True
        app.settings.enter_interrupts = False
        app._submit_text("queued", False)
        app._submit_text("interrupt", True)
        assert [item["source"] for item in app._pending_input] == ["interrupted", "queued"]
        app._chat_running = lambda: False
        app._flush_pending_input()
        app._flush_pending_input()
        assert [item["source"] for item in received][-2:] == ["interrupted", "queued"]



@pytest.mark.asyncio
@pytest.mark.parametrize("accept_after", [None, 1])
async def test_real_stream_rejected_completion_never_finalizes(app, tmp_path, monkeypatch, accept_after):
    configure(app, tmp_path, events=["completion_before"])
    if accept_after:
        counter = tmp_path / "counter"
        configure(app, tmp_path, events=["completion_before"], code=(
            f'import json,pathlib\np=pathlib.Path({str(counter)!r})\n'
            'n=int(p.read_text()) if p.exists() else 0\np.write_text(str(n+1))\n'
            'print(json.dumps({"decision":"deny" if n == 0 else "allow", "reason":"check again"}))'))
    app.model_id = "fixture"
    app.available_models = ["fixture"]
    app.settings.tool_iterations = 10
    async def ready():
        pass
    app._ensure_chat_ready = ready
    requests = []
    class Stream:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def __aiter__(self):
            async def chunks():
                yield SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(
                    content="draft", reasoning_content=None, tool_calls=None), finish_reason="stop")], usage=None)
            return chunks()
        async def close(self):
            pass
    async def create(**kwargs):
        requests.append(kwargs)
        return Stream()
    monkeypatch.setattr("litetui.app.model_transport.for_app", lambda a: SimpleNamespace(create=create))
    finalized = []
    async def finalize():
        finalized.append(1)
    app.plugins.finalize_turn = finalize
    emitted = []
    app._rpc_emit = emitted.append
    async with app.run_test(size=(110, 40)) as pilot:
        hook_host.start_prompt(app, {"content": "go", "source": "typed", "tool_profile": "autonomous"})
        for _ in range(120):
            if any(e.get("stopReason") in ("hook_denied", "stop") for e in emitted):
                break
            await pilot.pause(.05)
        # 🔴 COUNT COMPLETIONS, NOT CALLS (T821). This arm asserted
        # `len(requests)` and was CORRECT when it was written — 4e873f7,
        # 2026-09-12. It went red on 2026-09-16 when `_kick_card_summary`
        # (f2571bf, Ryan's collapsed-card title) started using the same
        # transport for a one-message side call: "Summarise the assistant reply
        # below in ONE short line, at most ten words."
        #
        #     AN ARM CAN BE CORRECT AT ITS TIMESTAMP AND WRONG AT YOURS WITHOUT
        #     ANYONE EDITING EITHER SIDE. Nothing changed in the code it was
        #     asserting about; a feature borrowed the transport underneath it.
        #
        #     A COUNTER OVER A SHARED TRANSPORT COUNTS EVERY FEATURE THAT USES
        #     IT. The arm named "completion requests" and measured "calls
        #     through the client", and the two were the same number on the day
        #     it was written.
        #
        # ⚠️ NOT `== 3`, AND NOT A PROMPT-TEXT MATCH. A number that happens to
        # be right today is the same defect one iteration later, and a string
        # test against CARD_SUMMARY_PROMPT would be a text gate — this file
        # already carries the T816 note about exactly that failure six lines
        # below. The transport now names its own PURPOSE; the side call is
        # excluded because of what it IS, not because of what it says or how
        # many of it there are.
        completions = [r for r in requests if r.get("purpose", "turn") == "turn"]
        summaries = [r for r in requests if r.get("purpose") == "card-summary"]
        assert len(completions) == (2 if accept_after else 4), (
            f"completions={len(completions)} summaries={len(summaries)} "
            f"total={len(requests)}")
        # ⬜ THE FILTER MUST BE DOING WORK. Without this, a build where nothing
        # tagged itself at all — every call defaulting to "turn" — would pass
        # the line above by having nothing to exclude, and the arm would be
        # green for the reason it was red. 0-of-0 and 0-of-1 are the same digit
        # until you say which you expect.
        if accept_after:
            assert len(summaries) == 1, requests
            assert len(requests) == 3, "a completion was mis-tagged"
        else:
            # A denied turn never finalizes, so no card finishes and nothing is
            # summarised. That asymmetry is the point of the whole arm.
            assert summaries == []
        assert finalized == ([1] if accept_after else [])
        # 🔴 THE stopReason, NOT DICT IDENTITY (T816). These were exact-dict
        # membership tests, so `9ca2d46` adding `tps` (and `77faa4d` adding
        # `tpsSource`) to every `turn_end` made both of them False — the event
        # was correct and the assertion could not say so.
        #
        #     AN EXACT-DICT MATCH ON AN EVENT PAYLOAD BREAKS ON EVERY
        #     LEGITIMATE FIELD ADDITION, and it fails in the direction that
        #     looks like the feature regressed.
        #
        # What this arm is actually about is WHICH stopReason a rejected
        # completion produces, so it asks that and nothing else.
        reasons = [e.get("stopReason") for e in emitted if e.get("type") == "turn_end"]
        assert ("stop" in reasons) == bool(accept_after)
        assert ("hook_denied" in reasons) != bool(accept_after)
