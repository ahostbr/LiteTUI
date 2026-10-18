"""Manual owner compaction takes fresh text once, without changing active snapshot."""
from test_owner_compact import (
    Handoff,
    _bypass_owned_seat,
    _summary,
    _write_call,
    _write_now,
)
from test_owner_compact import make_app as _make_app
from test_owner_compact import owner_dir as _owner_dir

from litetui import claude_compact, owner_mode
from litetui import prompt_overrides as prompts

make_app = _make_app
owner_dir = _owner_dir


def seed_idle_edit(app):
    path = app._agent_session.memory_root / "prompts" / "compact-owner.md"
    path.write_text("OLD ACTIVE SNAPSHOT {dir}\nSTEP 2 old", encoding="utf-8")
    prompts.refresh(app)
    active = app._prompt_snapshot
    path.write_text("LIVE IDLE TEMPLATE {dir}\nSTEP 2 new", encoding="utf-8")
    return path, active


def test_manual_local_compact_reads_idle_edit_once_and_retry_stays_pinned(make_app, owner_dir, monkeypatch):
    from test_compaction_ui import _run, _scripted_create, _seed, _settle
    _bypass_owned_seat(monkeypatch)
    app = make_app()
    path, active = seed_idle_edit(app)
    assert "OLD ACTIVE SNAPSHOT" in owner_mode.compact_prompt(owner_dir, app=app)
    app._system = lambda *args, **kwargs: None

    async def execute(name, args, hooks_enabled=True):
        _write_now(owner_dir)
        return "ok", True

    app._execute_tool = execute

    def first_round():
        path.write_text("MID OPERATION MUST NOT RELOAD {dir}\nSTEP 2 changed", encoding="utf-8")
        yield from _write_call(owner_dir)

    async def body():
        # One real app event loop, as in the running product. Re-running a
        # closed Textual app under a second asyncio.run is not a user path.
        async with app.run_test(size=(120, 40)) as pilot:
            _seed(app)
            create, calls = _scripted_create([first_round(), _summary(), _summary("retry summary")])
            monkeypatch.setattr(owner_mode, "run_handoff", Handoff([], outcomes=(False, True)))
            app.client.chat.completions.create = create
            app._compact()
            await _settle(app, pilot, ticks=20)
            assert len(calls) == 3
            for call in calls:
                text = "\n".join(str(message.get("content", "")) for message in call["messages"])
                assert "LIVE IDLE TEMPLATE" in text and "MID OPERATION MUST NOT RELOAD" not in text
            assert app._prompt_snapshot is active
            path.write_text("NEXT MANUAL TEMPLATE {dir}\nSTEP 2 next", encoding="utf-8")
            _seed(app)
            create, second = _scripted_create([_write_call(owner_dir), _summary()])
            monkeypatch.setattr(owner_mode, "run_handoff", Handoff([]))
            app.client.chat.completions.create = create
            app._compact()
            await _settle(app, pilot, ticks=20)
            assert len(second) == 2
            assert "NEXT MANUAL TEMPLATE" in second[0]["messages"][-1]["content"]
            assert app._prompt_snapshot is active

    _run(body())


def test_manual_claude_request_reads_fresh_owner_template_without_snapshot_mutation(make_app, owner_dir):
    app = make_app()
    path, active = seed_idle_edit(app)
    request = claude_compact.request(owner_dir=owner_dir, app=app)
    assert "LIVE IDLE TEMPLATE" in request and "OLD ACTIVE SNAPSHOT" not in request
    assert app._prompt_snapshot is active
    path.write_text("NEXT CLAUDE REQUEST {dir}\nSTEP 2 next", encoding="utf-8")
    assert "NEXT CLAUDE REQUEST" in claude_compact.request(owner_dir=owner_dir, app=app)
    assert app._prompt_snapshot is active


def test_manual_codex_owner_step_reads_fresh_template_then_retry_consistently(make_app, owner_dir):
    import asyncio
    app = make_app()
    path, active = seed_idle_edit(app)
    app._owner_compact_t0 = 123
    app._system = lambda _: None
    seen = []

    async def drive(text, **kwargs):
        seen.append(text)
        path.write_text("MID CODEX MUST NOT RELOAD {dir}\nSTEP 2 changed", encoding="utf-8")
        return True

    outcomes = iter([(False, "retry reason"), (True, "ok")])

    async def gate(_timestamp):
        return next(outcomes)

    app._owner_drive_turn, app._owner_gate = drive, gate
    assert asyncio.run(app._owner_codex_step_one())[0]
    assert len(seen) == 2 and "LIVE IDLE TEMPLATE" in seen[0]
    assert "MID CODEX MUST NOT RELOAD" not in "\n".join(seen)
    assert app._prompt_snapshot is active
