"""Claude selection and refusal contracts; default tests never call the SDK."""
import asyncio
from types import SimpleNamespace

import pytest

from litetui import llm_backend, model_transport
from litetui.claude_backend import ClaudeBackend, settle_close, track_close
from litetui.launch_options import LaunchOptions
from litetui.settings import Settings


def test_backend_factory_is_lazy_and_native():
    backend = llm_backend.make_backend(Settings(backend="claude"))
    assert isinstance(backend, ClaudeBackend)
    assert backend.owns_native_turns
    assert not hasattr(backend, "app_server")
    assert llm_backend.backend_label("claude") == "Claude Agent"


def test_legacy_transport_and_sidecalls_never_fall_back():
    app = SimpleNamespace(backend=ClaudeBackend(Settings(backend="claude")))
    with pytest.raises(model_transport.ProviderError, match="legacy transport"):
        model_transport.for_app(app)
    with pytest.raises(model_transport.ProviderError, match="sidecalls"):
        model_transport.complete_sidecall(app, {})


@pytest.mark.parametrize("values", [
    {"base_url": "http://localhost:1234"}, {"context_length": 8192},
    {"max_tokens": 123}, {"server_executable": "claude"},
    {"model_path": "x.gguf"}, {"load_model": True}, {"server_mode": "start"},
    {"server_mode": "connect"}, {"api_key_env": "KEY"},
])
def test_local_launch_flags_refused_before_maps(values):
    with pytest.raises(ValueError, match="Claude owns"):
        LaunchOptions(**values).overrides(Settings(), "claude", "default")


def test_normal_launch_has_no_local_overrides():
    assert LaunchOptions().overrides(Settings(), "claude", "default") == {}


@pytest.mark.asyncio
async def test_loading_refuses_without_gpu_effect():
    backend = ClaudeBackend(Settings(backend="claude"))
    with pytest.raises(llm_backend.BackendError, match="remotely"):
        await backend.load("default")
    with pytest.raises(llm_backend.BackendError, match="local model"):
        await backend.unload("default")


# -- owned cleanup is accumulated, settled once, and never lost -------------

class _Recorder:
    """A backend whose close is observable without an SDK anywhere near it."""

    owns_native_turns = True
    name = "claude"

    def __init__(self, *, fails=None, blocks=False):
        self.session = object()
        self._closing = None
        self.closes = 0
        self._fails = fails
        self._blocks = blocks

    async def close(self):
        self.closes += 1
        self.session = None
        if self._blocks:
            await asyncio.Event().wait()
        if self._fails:
            raise RuntimeError(self._fails)

    shutdown = ClaudeBackend.shutdown


@pytest.mark.asyncio
async def test_two_switches_never_drop_a_close():
    # The assignment this replaced overwrote the first handle; asyncio keeps
    # only a weak reference to a task, so the dropped one could be collected
    # mid-flight and its claude.exe stopped being tracked.
    app = SimpleNamespace()
    first, second = _Recorder(), _Recorder()
    track_close(app, first.shutdown())
    track_close(app, second.shutdown())
    assert await settle_close(app) == []
    assert (first.closes, second.closes) == (1, 1)


@pytest.mark.asyncio
async def test_a_none_shutdown_does_not_erase_a_live_handle():
    # shutdown() returns None once close() has cleared the session. Assigning
    # that None used to wipe a close that was still running.
    app = SimpleNamespace()
    live = _Recorder()
    track_close(app, live.shutdown())
    spent = _Recorder()
    spent.session = None
    assert spent.shutdown() is None
    track_close(app, spent.shutdown())
    assert await settle_close(app) == []
    assert live.closes == 1


@pytest.mark.asyncio
async def test_shutdown_hands_back_the_live_handle_instead_of_closing_twice():
    backend = _Recorder()
    first = backend.shutdown()
    second = backend.shutdown()
    assert first is second
    app = SimpleNamespace()
    track_close(app, first)
    track_close(app, second)
    assert await settle_close(app) == []
    assert backend.closes == 1


@pytest.mark.asyncio
async def test_every_cleanup_failure_is_reported_and_reported_once():
    app = SimpleNamespace()
    track_close(app, _Recorder(fails="reader stuck").shutdown())
    track_close(app, _Recorder(fails="disconnect timed out").shutdown())
    failures = await settle_close(app)
    assert len(failures) == 2
    assert any("reader stuck" in f for f in failures)
    assert any("disconnect timed out" in f for f in failures)
    # Settled means settled: the same failures must not surface again.
    assert await settle_close(app) == []


@pytest.mark.asyncio
async def test_a_failed_close_does_not_poison_later_cleanup():
    # The handle used to be cleared only after a successful await, so a close
    # that raised stayed in place and re-raised on every later turn and every
    # connect — the backend was unusable until the app restarted.
    app = SimpleNamespace()
    track_close(app, _Recorder(fails="boom").shutdown())
    assert await settle_close(app) != []
    assert getattr(app, "_claude_closing", None) is None
    healthy = _Recorder()
    track_close(app, healthy.shutdown())
    assert await settle_close(app) == []
    assert healthy.closes == 1


@pytest.mark.asyncio
async def test_a_wedged_close_is_bounded_rather_than_hanging_the_caller():
    app = SimpleNamespace()
    task = _Recorder(blocks=True).shutdown()
    track_close(app, task)
    failures = await settle_close(app, timeout=0.05)
    assert failures and "did not settle" in failures[0]
    task.cancel()


@pytest.mark.asyncio
async def test_a_cancelled_close_reports_unverified_cleanup():
    app = SimpleNamespace()
    task = _Recorder(blocks=True).shutdown()
    track_close(app, task)
    task.cancel()
    assert any("cancelled" in error for error in await settle_close(app))


@pytest.mark.asyncio
async def test_settle_deadline_retains_cancellation_resistant_cleanup():
    app = SimpleNamespace()
    release = asyncio.Event()

    async def cleanup():
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()

    task = asyncio.create_task(cleanup())
    await asyncio.sleep(0)
    track_close(app, task)
    waiter = asyncio.create_task(settle_close(app, timeout=0.01))
    try:
        done, _ = await asyncio.wait([waiter], timeout=0.1)
        assert waiter in done, "settle_close waited for cancellation instead of bounding the wait"
        assert "did not settle" in waiter.result()[0]
        assert task in app._claude_closing
        assert not task.done()
    finally:
        release.set()
        await asyncio.gather(task, waiter, return_exceptions=True)
    assert await settle_close(app) == []


# -- a real Textual exit must release an owned runtime ---------------------


def _exit_app(tmp_path, monkeypatch):
    """A real LiteTUI, wired the way the backend-switch suite wires one."""
    from litetui import app as app_mod
    from litetui import paths

    monkeypatch.setattr(paths, "CONVO_DIR", tmp_path)
    app = app_mod.LiteTUI()
    app.settings = Settings()
    app.connect = lambda: None
    app._connect = lambda: None
    app._system = lambda *a, **k: None
    app._persist = lambda *a, **k: None
    return app


@pytest.mark.asyncio
async def test_app_exit_closes_an_owned_claude_runtime(tmp_path, monkeypatch):
    # 🔴 THE EXIT BRANCH WAS UNREACHABLE. It lived inside
    # `if backend.name == "codex"`, and ClaudeBackend.name is "claude", so a
    # normal exit never closed the owned claude.exe. Driven through the real
    # Textual shutdown rather than by calling _shutdown directly, because the
    # bug was in which branch runs, not in what the branch does.
    app = _exit_app(tmp_path, monkeypatch)
    backend = _Recorder()
    async with app.run_test():
        app.backend = backend
    assert backend.closes == 1


@pytest.mark.asyncio
async def test_app_exit_settles_a_close_left_by_a_backend_switched_away(tmp_path, monkeypatch):
    # Owned cleanup outlives the backend that started it: switching off Claude
    # leaves its close on the app, and exiting still has to wait for it.
    app = _exit_app(tmp_path, monkeypatch)
    abandoned = _Recorder()
    async with app.run_test():
        track_close(app, abandoned.shutdown())
        app.backend = SimpleNamespace(name="llamacpp")
    assert abandoned.closes == 1
    assert getattr(app, "_claude_closing", None) is None


@pytest.mark.asyncio
async def test_app_exit_survives_a_cleanup_failure(tmp_path, monkeypatch):
    app = _exit_app(tmp_path, monkeypatch)
    backend = _Recorder(fails="disconnect timed out")
    async with app.run_test():
        app.backend = backend
    assert backend.closes == 1


# -- /backend lists Claude; /model offers every spelling the CLI accepts ------

def test_backend_picker_lists_every_registered_backend_including_claude(monkeypatch):
    """Ryan 2026-09-24: "slash backend list doesnt list claude you have to
    manually type it". The picker had its own row list; it now reads BACKENDS."""
    from litetui import gpu_gate
    from litetui.plugins import model_switch

    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: True)
    monkeypatch.setattr(model_switch, "_ninfer_mark", lambda app: "n/a")
    seen = {}
    monkeypatch.setattr(model_switch, "pick", lambda app, title, rows, cb, current=None: seen.update(rows=rows))
    app = SimpleNamespace(settings=Settings(), backend=SimpleNamespace(name="lmstudio"))
    model_switch._cmd_backend(app, "/backend", "")
    keys = [k for k, _ in seen["rows"]]
    assert keys == list(llm_backend.BACKEND_NAMES)
    claude = dict(seen["rows"])["claude"]
    assert claude.startswith("Claude Agent  · ")
    assert claude.split("· ")[1] in (
        "OAuth signed in", "subscription login required", "SDK not installed — uv sync --extra claude")


@pytest.mark.asyncio
async def test_catalog_keeps_cli_rows_first_and_adds_every_context_variant(monkeypatch):
    from litetui import claude_backend

    queried = ["default", "opus[1m]", "claude-fable-5-1[1m]", "sonnet", "haiku"]

    class _Session:
        def __init__(self, options):
            pass
        async def start(self):
            return {"models": [{"value": v} for v in queried]}
        async def close(self):
            pass

    async def _options(self, **values):
        return values

    monkeypatch.setattr(claude_backend, "ClaudeSession", _Session)
    monkeypatch.setattr(ClaudeBackend, "_options", _options)
    backend = ClaudeBackend(Settings(backend="claude"))
    keys = [row.key for row in await backend.list_models()]
    assert keys[:len(queried)] == queried
    for key in ("claude-opus-5-5", "claude-opus-5-5[1m]", "claude-sonnet-5[1m]",
                "claude-fable-5-1", "claude-haiku-4-5-20251001", "fable", "opus"):
        assert key in keys
    assert len(keys) == len(set(keys))
    assert "claude-haiku-4-5-20251001[1m]" not in keys and "haiku[1m]" not in keys
    await backend.ensure_chat_ready("claude-sonnet-5[1m]")   # selectable, not refused


def test_effort_levels_come_from_cli_metadata_with_a_static_fallback():
    from litetui.claude_backend import STATIC_EFFORT
    backend = ClaudeBackend(Settings(backend="claude"))
    backend.models = {
        "sonnet": {"value": "sonnet", "resolvedModel": "claude-sonnet-5", "supportedEffortLevels": ["low", "high"]},
        "haiku": {"value": "haiku", "resolvedModel": "claude-haiku-4-5-20251001"},
        "claude-opus-5-5": {"value": "claude-opus-5-5"},
        "claude-haiku-4-5-20251001": {"value": "claude-haiku-4-5-20251001"},
    }
    assert backend.reasoning_levels("sonnet") == ["low", "high"]
    assert backend.reasoning_levels("haiku") == []
    assert backend.reasoning_levels("claude-opus-5-5") == list(STATIC_EFFORT)
    assert backend.reasoning_levels("claude-haiku-4-5-20251001") == []
    assert backend.reasoning_levels("nope") == []


def test_backend_rows_is_the_one_source_for_the_picker_and_the_sidecar(monkeypatch):
    from litetui import gpu_gate
    from litetui.plugins import model_switch

    monkeypatch.setattr(gpu_gate, "is_rtx_5090", lambda: False)
    app = SimpleNamespace(settings=Settings(), backend=SimpleNamespace(name="lmstudio"))
    rows = model_switch.backend_rows(app)
    assert [k for k, _ in rows] == [k for k, _ in llm_backend.visible_backends()]
    assert dict(rows)["claude"].startswith("Claude Agent  · ")
    seen = {}
    monkeypatch.setattr(model_switch, "pick", lambda app, title, rows, cb, current=None: seen.update(rows=rows))
    model_switch._cmd_backend(app, "/backend", "")
    assert seen["rows"] == rows


def _identity_app(monkeypatch):
    """A real LiteTUI (conftest isolates its data) with a store and a registered seat."""
    from litetui import app as app_mod
    app = app_mod.LiteTUI()
    app._materialise_convo()
    (app.convo_dir / "soul.md").write_text("SOUL-MARKER-4471", encoding="utf-8")
    (app.convo_dir / "handoff.md").write_text("HANDOFF-MARKER-9902", encoding="utf-8")
    real = app._all_tools
    monkeypatch.setattr(app, "_all_tools", lambda: [*real(), {"type": "function", "function": {"name": "harness"}}])
    return app


def test_claude_is_told_it_is_litetui_not_claude_code(monkeypatch):
    """Ryan 2026-09-24 (plan claude-backend-litetui-identity, phase 1): "Replace with
    LiteTUI's prompt" = systemprompt.md (tool section for Claude's built-ins) + the
    soul/memory/handoff snapshot + harness identity."""
    from litetui import paths
    from litetui.claude_backend import COMPACT_MARKER
    from litetui.claude_turn import ledger_for, system_prompt_for

    app = _identity_app(monkeypatch)
    segment = ledger_for(app).select_segment("ws")
    prompt = system_prompt_for(app, segment)
    first_line = paths.SYSTEM_PROMPT_FILE.read_text(encoding="utf-8").strip().splitlines()[0]
    assert isinstance(prompt, str) and first_line[:60] in prompt
    assert "SOUL-MARKER-4471" in prompt and "HANDOFF-MARKER-9902" in prompt
    assert app.seat.agent_id in prompt and COMPACT_MARKER in prompt
    assert str(app.convo_dir).replace("\\", "/") in prompt, "the store folder is named"
    assert "mcp__litetui__" in prompt and "Git Bash" in prompt, "the tool section is Claude's"
    assert "call `subagent`" not in prompt, "LiteTUI's own tool section is swapped out"
    assert "You are Claude Code" not in prompt


def test_the_prompt_is_fixed_for_the_life_of_the_segment(monkeypatch):
    from litetui.claude_turn import ledger_for, system_prompt_for

    app = _identity_app(monkeypatch)
    ledger = ledger_for(app)
    segment = ledger.select_segment("ws")
    first = system_prompt_for(app, segment)
    (app.convo_dir / "soul.md").write_text("CHANGED-LATER", encoding="utf-8")
    again = system_prompt_for(app, ledger.segment(segment["id"]))
    assert again == first, "a resume carries the same prefix (cache)"
    fresh = ledger.select_segment("ws", new=True)
    assert "CHANGED-LATER" in system_prompt_for(app, fresh), "a new session sees the new store"


def test_a_segment_already_bound_under_the_preset_keeps_it(monkeypatch):
    from litetui.claude_turn import ledger_for, system_prompt_for

    app = _identity_app(monkeypatch)
    ledger = ledger_for(app)
    segment = ledger.select_segment("ws")
    ledger.bind_session(segment["id"], "native-legacy")
    assert system_prompt_for(app, ledger.segment(segment["id"])) is None


def test_open_session_sends_the_prompt_as_a_plain_string_with_auto_memory_off(monkeypatch):
    from types import SimpleNamespace as NS

    from litetui import claude_backend as cb

    seen = []
    monkeypatch.setattr(cb, "sdk_module", lambda: NS(ClaudeAgentOptions=lambda **kw: seen.append(kw) or kw))

    class FakeSession:
        def __init__(self, options):
            self.options = options

        async def start(self):
            return {}
    monkeypatch.setattr(cb, "ClaudeSession", FakeSession)
    backend = cb.ClaudeBackend(NS(claude_executable=""))
    backend.models = {"sonnet": {"value": "sonnet"}}
    asyncio.run(backend.open_session({"id": "s1", "workspace": ".", "session_id": None}, "sonnet",
                                     system_prompt="LITETUI PROMPT"))
    assert seen[-1]["system_prompt"] == "LITETUI PROMPT"
    assert seen[-1]["env"]["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"


@pytest.mark.asyncio
async def test_a_session_opened_before_the_seat_registers_waits_for_it(monkeypatch):
    """Review of phase 1: the seat registers in the background a few seconds after launch,
    and a prompt fixed before that would carry no harness identity for the segment's life
    (the host re-syncs its own system message; a fixed Claude prompt cannot)."""
    from litetui.claude_turn import ledger_for, prompt_for_new_session

    app = _identity_app(monkeypatch)
    app._seat_started = False
    real = app._all_tools
    monkeypatch.setattr(app, "_all_tools", lambda: [*real()] if not app._seat_started else
                        [*real(), {"type": "function", "function": {"name": "harness"}}])

    async def register_late():
        await asyncio.sleep(0.3)
        app._seat_started = True
    task = asyncio.ensure_future(register_late())
    prompt = await prompt_for_new_session(app, ledger_for(app).select_segment("ws"))
    await task
    assert app.seat.agent_id in prompt, "the late seat still made it into the fixed prompt"
