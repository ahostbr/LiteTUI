"""Uncertain resumed deliveries hold startup input without replay or seat failure."""
import asyncio
from types import SimpleNamespace

import pytest

from litetui import hook_host
from litetui.app import LiteTUI
from litetui.claude_persistence import ClaudeLedger
from litetui.claude_turn import command
from litetui.lifecycle_hooks import Hook, Snapshot


def resumed_host(tmp_path, reached):
    ledger = ClaudeLedger(tmp_path)
    segment = ledger.select_segment(str(tmp_path.resolve()))
    old = ledger.prepare(segment["id"], "already sent", "strict", "harness")
    ledger.update_delivery(old["id"], "submitted")
    if reached in ("acknowledged", "uncertain"):
        ledger.update_delivery(old["id"], reached)
    before = ledger.file.read_bytes()
    app = SimpleNamespace(
        convo_dir=tmp_path, convo_id="saved", _hook_workspace=tmp_path,
        backend=SimpleNamespace(name="claude", owns_native_turns=True, session=None),
        _materialise_convo=lambda: None, _chat_running=lambda: False,
        _pending_input=[], _stop_requested=False, notices=[], appended=[], streams=[],
    )
    hook_host.initialize(app)
    app._hook_workspace = tmp_path
    app._system = app.notices.append
    app._append = app.appended.append
    app._stream = lambda: app.streams.append("started")
    app._flush_pending_input = lambda: LiteTUI._flush_pending_input(app)
    return app, old, before


@pytest.mark.parametrize("reached", ["submitted", "acknowledged", "uncertain"])
@pytest.mark.parametrize("queued", [False, True])
def test_restart_uncertainty_holds_startup_input_until_explicit_resolution(tmp_path, reached, queued):
    app, old, before = resumed_host(tmp_path, reached)
    item = {"content": r"Read C:\a\t1\n2", "source": "harness", "tool_profile": "strict"}
    if queued:
        app._pending_input.append(item)
        app._flush_pending_input()
    else:
        hook_host.start_prompt(app, item)
    assert app.streams == [] and app.appended == []
    assert len(app._pending_input) == 1
    held = app._pending_input[0]
    assert held["content"] == item["content"] and held["source"] == "harness"
    assert any("/claude resolve" in notice for notice in app.notices)
    ledger = app._claude_ledger
    assert ledger.pending(ledger.selected["id"])[0]["state"] == "uncertain"
    assert ledger.file.read_bytes() == before, "holding must not rewrite delivery evidence"
    app._flush_pending_input()
    notice_count = len(app.notices)
    for _ in range(10):
        app._flush_pending_input()
    assert len(app.notices) == notice_count, "repeated flush must not flood warnings"
    assert app.streams == [] and len(app._pending_input) == 1
    assert ledger.file.read_bytes() == before
    command(app, "continue")
    assert app.streams == [] and len(app._pending_input) == 1
    assert ledger.file.read_bytes() == before

    command(app, "resolve")
    app._flush_pending_input()
    assert app.streams == ["started"] and app._pending_input == []
    assert [row["content"] for row in app.appended] == [item["content"]]
    assert app.appended[0]["claude_delivery"]["id"] != old["id"]
    pending = ledger.pending(ledger.selected["id"])
    assert len(pending) == 1 and pending[0]["state"] == "prepared"
    assert pending[0]["content"] == item["content"], "the old uncertain input was never replayed"
    app._flush_pending_input()
    assert app.streams == ["started"], "no duplicate turn"


@pytest.mark.parametrize("switch", ["conversation", "segment", "backend", "workspace"])
def test_held_unadmitted_input_never_moves_to_another_owner(tmp_path, switch):
    app, _, _ = resumed_host(tmp_path, "submitted")
    hook_host.start_prompt(app, {"content": "startup mail", "source": "harness"})
    held = app._pending_input[0]
    if switch == "conversation":
        app.convo_id = "other"
    elif switch == "segment":
        app._claude_ledger.select_segment(str(tmp_path.resolve()), new=True)
    elif switch == "backend":
        app.backend = SimpleNamespace(name="codex", owns_native_turns=False)
    else:
        app._hook_workspace = tmp_path / "other-workspace"
    app._flush_pending_input()
    assert app._pending_input == [held]
    assert app.streams == [] and app.appended == []
    assert any("held for its original" in notice for notice in app.notices)


@pytest.mark.asyncio
async def test_hook_worker_surfaces_uncertainty_and_retains_input(tmp_path, monkeypatch):
    app, _, before = resumed_host(tmp_path, "submitted")
    state = Snapshot(hooks=(Hook("observe", ("prompt_before",), "unused"),))
    monkeypatch.setattr(hook_host, "snapshot", lambda app: state)
    calls = []

    async def invoke(*args, **kwargs):
        from litetui.lifecycle_hooks import HookResult
        calls.append("hook")
        return HookResult(True)

    monkeypatch.setattr(hook_host, "invoke", invoke)
    workers = []
    app.run_worker = lambda coro, **kwargs: workers.append(coro)
    hook_host.start_prompt(app, {"content": "startup mail", "source": "harness"})
    await workers[0]
    assert calls == ["hook"]
    assert app.streams == [] and app.appended == []
    assert len(app._pending_input) == 1
    assert any("/claude resolve" in notice for notice in app.notices)
    app._flush_pending_input()
    assert len(workers) == 1, "holding must not create a worker/retry loop"
    assert app._claude_ledger.file.read_bytes() == before


def test_uncertainty_preserves_fifo_for_existing_and_new_startup_input(tmp_path):
    app, _, _ = resumed_host(tmp_path, "submitted")
    for content in ("first", "second"):
        app._pending_input.append({"content": content, "source": "harness"})
    app._flush_pending_input()
    hook_host.start_prompt(app, {"content": "third", "source": "harness"})
    assert [item["content"] for item in app._pending_input] == ["first", "second", "third"]
    assert app.streams == []
    command(app, "resolve")
    for _ in range(4):
        app._flush_pending_input()
    assert [row["content"] for row in app.appended] == ["first", "second", "third"]
    assert app.streams == ["started"] * 3
    assert len({row["claude_delivery"]["id"] for row in app.appended}) == 3


def test_unrelated_admission_value_error_is_not_hidden(tmp_path, monkeypatch):
    app, _, _ = resumed_host(tmp_path, "submitted")

    def invalid(*args):
        raise ValueError("not an uncertainty refusal")

    monkeypatch.setattr("litetui.claude_turn.accept_input", invalid)
    with pytest.raises(ValueError, match="not an uncertainty"):
        hook_host.start_prompt(app, {"content": "mail", "source": "harness"})
    assert not app._pending_input


@pytest.mark.asyncio
async def test_cli_first_prompt_is_held_nonfatally_after_ledger_reload(tmp_path):
    app, _, before = resumed_host(tmp_path, "submitted")
    app.available_models = ["fixture"]
    app._connect_settled = True
    app._cli_initial_model = None
    app._cli_system_prompt = None
    app._first_prompt = r"Read C:\a\t1\n2"
    app._cli_args_done = asyncio.Event()
    app._resume_cli_convo = lambda: True

    async def ready(**kwargs):
        return True

    app._ensure_chat_ready = ready
    app.pending_image = None
    app._split_image_path = lambda text: (None, text)
    app._looks_like_image_path = lambda text: False
    app._oversize_refusal = lambda content: None
    app._refuse_submit = lambda *args: None
    app._submit_text = lambda text, **kwargs: LiteTUI._submit_text(app, text, **kwargs)
    await LiteTUI._apply_cli_args.__wrapped__(app)
    assert app._cli_launch_error is None and app._cli_args_done.is_set()
    assert app.streams == [] and app.appended == []
    assert [item["content"] for item in app._pending_input] == [app._first_prompt]
    assert any("/claude resolve" in note for note in app.notices)
    assert app._claude_ledger.file.read_bytes() == before
    command(app, "resolve")
    command(app, "continue")
    assert app.streams == ["started"]
    assert [row["content"] for row in app.appended] == [app._first_prompt]


@pytest.mark.asyncio
async def test_real_textual_seat_survives_startup_inbox_uncertainty(tmp_path):
    _, _, before = resumed_host(tmp_path, "submitted")
    app = LiteTUI()
    app._connect = lambda: None
    app._fetch_ctx_window = lambda: None
    app.jobs[:] = []
    app._stream = lambda: pytest.fail("no Claude turn may start while delivery is uncertain")
    async with app.run_test(size=(110, 40)) as pilot:
        app.convo_dir = tmp_path
        app.convo_id = "saved"
        app._hook_workspace = tmp_path
        app._materialise_convo = lambda: None
        app._backend = SimpleNamespace(name="claude", owns_native_turns=True, session=None)
        app._deliver_inbox({"from": "leader", "body": "startup mail"})
        await pilot.pause()
        assert app.is_running
        assert len(app._pending_input) == 1
        assert "startup mail" in app._pending_input[0]["content"]
        assert not any("startup mail" in row.get("content", "") for row in app.conversation)
        notes = [str(widget.content) for widget in app.query(".system-msg")]
        assert any("/claude resolve" in note for note in notes)
        for _ in range(5):
            app._flush_pending_input()
        await pilot.pause()
        assert app.is_running and len(app._pending_input) == 1
        assert app._claude_ledger.file.read_bytes() == before
        await pilot.press("x")
        assert app.query_one("#message-input").value.endswith("x"), "input remains responsive"
        app._backend = None  # fake runtime has no shutdown surface


@pytest.mark.asyncio
@pytest.mark.parametrize("hooks_enabled", [False, True])
@pytest.mark.parametrize("switch", ["backend", "conversation", "segment"])
async def test_local_round_boundary_never_consumes_wrong_owner_claude_input(tmp_path, monkeypatch, hooks_enabled, switch):
    app, _, before = resumed_host(tmp_path, "submitted")
    hook_host.start_prompt(app, {"content": "held startup mail", "source": "harness"})
    held = app._pending_input[0]
    later = {"content": "later local input", "source": "typed"}
    app._pending_input.append(later)
    if switch == "backend":
        app.backend = SimpleNamespace(name="lmstudio", owns_native_turns=False)
    elif switch == "conversation":
        app.convo_id = "other"
    else:
        app._claude_ledger.select_segment(str(tmp_path.resolve()), new=True)
    app._deliver_queued_input = lambda: LiteTUI._deliver_queued_input(app)
    calls = []

    async def invoke(*args, **kwargs):
        from litetui.lifecycle_hooks import HookResult
        calls.append("hook")
        return HookResult(True)

    monkeypatch.setattr(hook_host, "invoke", invoke)
    if hooks_enabled:
        state = Snapshot(hooks=(Hook("observe", ("prompt_before",), "unused"),))
        monkeypatch.setattr(hook_host, "snapshot", lambda app: state)
    assert not await hook_host.queued_prompt(app)
    assert app._pending_input == [held, later], "wrong-owner head must block, not rotate FIFO"
    assert app.appended == [] and app.streams == []
    assert calls == [], "ownership refusal must happen before dispatching prompt hooks"
    notice_count = len(app.notices)
    for _ in range(5):
        assert not await hook_host.queued_prompt(app)
    assert len(app.notices) == notice_count
    assert app._pending_input == [held, later]
    if switch != "segment":
        assert app._claude_ledger.file.read_bytes() == before
    app.convo_id = "saved"
    app.backend = SimpleNamespace(name="claude", owns_native_turns=True, session=None)
    if switch == "segment":
        return  # A new segment never inherits the old queue.
    command(app, "resolve")
    assert await hook_host.queued_prompt(app)
    assert [row["content"] for row in app.appended] == [held["content"]]
    assert app._pending_input == [later]
    assert calls == (["hook"] if hooks_enabled else [])
