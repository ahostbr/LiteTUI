"""Reviewed system prompt edits use the real owned store, record builders and canonical replay."""
from __future__ import annotations

import copy
import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_rust_backend_catalog import catalog_app as _catalog_app
from test_rust_backend_catalog import forbidden
from test_rust_context_reader import app as owned_app
from test_rust_context_reader import armed as owned_armed
from test_rust_context_reader import switch_app as owned_switch_app

from litetui import appsvc, command_surface, gui_rpc, native_system_prompt
from litetui import background_input as work
from litetui.conversation import ConversationRepository

app, armed, switch_app, command_app = owned_app, owned_armed, owned_switch_app, _catalog_app
pytestmark = pytest.mark.asyncio


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def request(app, action, **fields):
    return {"id": action + "-id", "version": 1, **work.identity(app), "work_instance": work.bridge(app).instance,
            "type": native_system_prompt.PREFIX + action, "document": "system", **fields}


def disk(app):
    return ConversationRepository.read(app.convo_path)


def review(app, text, **fields):
    before = app.conversation[0]["content"]
    return gui_rpc.dispatch(app, request(app, "review", revision=digest(before), text=text, **fields))


async def test_review_apply_readback_preserves_exact_bytes_and_extra_fields(armed):
    old = armed.conversation[0]["content"]
    assert armed.conversation[0]["hidden"] == {"keep": True}
    after = "Line one\r\nTab\there\n  indented 雪🍃\n\n"
    opened = gui_rpc.dispatch(armed, request(armed, "edit", revision=digest(old)))
    assert opened["text"] == old and opened["stored"] == "saved" and "next request" in opened["effect"]
    raw = armed.convo_path.read_bytes()
    r = review(armed, after)
    assert armed.convo_path.read_bytes() == raw and armed.conversation[0]["content"] == old
    assert (r["before_bytes"], r["after_bytes"], r["after_revision"]) == (len(old), len(after.encode()), digest(after))
    apply = request(armed, "apply", token=r["token"])
    a = gui_rpc.dispatch(armed, apply)
    assert a["stored"] == "saved" and a["revision"] == digest(after)
    assert armed.conversation[0] == {"role": "system", "content": after, "hidden": {"keep": True}}
    assert armed._store_injected is False
    written = armed.convo_path.read_bytes()
    assert written.startswith(raw) and written[len(raw):].count(b"\n") == 1
    assert disk(armed)[1][0] == armed.conversation[0]
    assert gui_rpc.dispatch(armed, apply) == a
    assert armed.convo_path.read_bytes() == written
    with pytest.raises(ValueError, match="already applied"):
        gui_rpc.dispatch(armed, {**apply, "id": "different"})
    read = request(armed, "readback", token=r["token"], apply_id=apply["id"])
    saved = gui_rpc.dispatch(armed, read)
    assert saved["kind"] == "context_saved" and saved["stored"] == "saved" and saved["bytes"] == len(after.encode())
    armed.conversation[0]["content"] = "changed behind the review"
    with pytest.raises(ValueError, match="after apply"):
        gui_rpc.dispatch(armed, read)


async def test_readback_reads_the_transcript_not_the_live_row(armed):
    r = review(armed, "Disk must agree")
    apply = request(armed, "apply", token=r["token"])
    gui_rpc.dispatch(armed, apply)
    armed.convo_path.write_bytes(armed.convo_path.read_bytes()[:-1] + b"\n" + b'{"type":"edit","index":0,"message":'
                                 b'{"role":"system","content":"other writer"}}\n')
    with pytest.raises(ValueError, match="transcript does not hold"):
        gui_rpc.dispatch(armed, request(armed, "readback", token=r["token"], apply_id=apply["id"]))


async def test_review_is_pure_and_refuses_stale_unchanged_blank_nul_and_oversize(armed):
    old = armed.conversation[0]["content"]
    raw, rows = armed.convo_path.read_bytes(), copy.deepcopy(armed.conversation)
    for text, match in [(old, "unchanged"), ("", "nonempty"), (" \r\n\t", "nonempty"), ("a\0b", "NUL"),
                        ("x" * (native_system_prompt.MAX_PROMPT + 1), "limit"),
                        ("雪" * (native_system_prompt.MAX_PROMPT // 3 + 1), "limit")]:
        with pytest.raises(ValueError, match=match):
            review(armed, text)
    exact = "z" * native_system_prompt.MAX_PROMPT
    assert review(armed, exact)["after_bytes"] == native_system_prompt.MAX_PROMPT
    with pytest.raises(ValueError, match="changed since"):
        gui_rpc.dispatch(armed, request(armed, "review", revision="0" * 64, text="new"))
    with pytest.raises(ValueError, match="Invalid"):
        gui_rpc.dispatch(armed, request(armed, "review", revision="bad", text="new"))
    assert armed.convo_path.read_bytes() == raw and armed.conversation == rows


async def test_stale_apply_after_outside_change_keeps_the_current_prompt(armed):
    r = review(armed, "Reviewed text")
    armed.conversation[0]["content"] = "someone else"
    raw = armed.convo_path.read_bytes()
    with pytest.raises(ValueError, match="changed since review"):
        gui_rpc.dispatch(armed, request(armed, "apply", token=r["token"]))
    assert armed.conversation[0]["content"] == "someone else" and armed.convo_path.read_bytes() == raw
    with pytest.raises(ValueError, match="Review the current"):
        gui_rpc.dispatch(armed, request(armed, "apply", token=r["token"]))


async def test_streaming_turn_refuses_every_operation_before_mutation(armed, monkeypatch):
    r = review(armed, "Reviewed text")
    monkeypatch.setattr(armed, "_chat_running", lambda: True)
    raw = armed.convo_path.read_bytes()
    for action, fields in [("edit", {"revision": digest(armed.conversation[0]["content"])}),
                           ("review", {"revision": digest(armed.conversation[0]["content"]), "text": "x"}),
                           ("apply", {"token": r["token"]})]:
        with pytest.raises(ValueError, match="operation is active"):
            gui_rpc.dispatch(armed, request(armed, action, **fields))
    assert armed.convo_path.read_bytes() == raw and armed.conversation[0]["content"] != "Reviewed text"


async def test_failed_append_leaves_the_live_prompt_and_reports_an_error(armed, monkeypatch):
    old = copy.deepcopy(armed.conversation[0])
    r = review(armed, "Will not persist")

    def fail(record):
        armed.store.persist_error = "OSError: disk full"

    monkeypatch.setattr(armed.store, "write_record", fail)
    with pytest.raises(OSError, match="not changed"):
        gui_rpc.dispatch(armed, request(armed, "apply", token=r["token"]))
    assert armed.conversation[0] == old
    with pytest.raises(ValueError, match="Review the current"):
        gui_rpc.dispatch(armed, request(armed, "apply", token=r["token"]))
    again = review(armed, "Second attempt")
    with pytest.raises(ValueError, match="persistence is unavailable"):
        gui_rpc.dispatch(armed, request(armed, "apply", token=again["token"]))
    assert armed.conversation[0] == old


async def test_unborn_conversation_reports_session_only_and_the_first_snapshot_carries_it(armed):
    armed.store.pending = True
    old = armed.conversation[0]["content"]
    opened = gui_rpc.dispatch(armed, request(armed, "edit", revision=digest(old)))
    assert opened["stored"] == "session_only"
    raw = armed.convo_path.read_bytes()
    r = review(armed, "Held in memory")
    apply = request(armed, "apply", token=r["token"])
    assert gui_rpc.dispatch(armed, apply)["stored"] == "session_only"
    assert armed.convo_path.read_bytes() == raw
    saved = gui_rpc.dispatch(armed, request(armed, "readback", token=r["token"], apply_id=apply["id"]))
    assert saved["stored"] == "session_only" and armed.conversation[0]["content"] == "Held in memory"
    armed.store.pending = False
    armed._snapshot("materialised on first message")
    assert disk(armed)[1][0]["content"] == "Held in memory"


async def test_missing_prompt_is_inserted_with_a_snapshot_and_other_rows_survive(armed):
    armed.conversation[:] = armed.conversation[1:]
    armed._snapshot("fixture without system")
    rows = copy.deepcopy(armed.conversation)
    opened = gui_rpc.dispatch(armed, request(armed, "edit", revision=None))
    assert opened["text"] == ""
    r = gui_rpc.dispatch(armed, request(armed, "review", revision=None, text="Fresh prompt"))
    assert r["before_bytes"] is None
    gui_rpc.dispatch(armed, request(armed, "apply", token=r["token"]))
    assert armed.conversation == [{"role": "system", "content": "Fresh prompt"}, *rows]
    assert disk(armed)[1] == armed.conversation


async def test_prompt_that_is_not_first_is_not_edited(armed):
    armed.conversation.insert(0, {"role": "user", "content": "before the system row"})
    with pytest.raises(ValueError, match="not the first message"):
        gui_rpc.dispatch(armed, request(armed, "edit", revision=digest(armed.conversation[1]["content"])))


async def test_kept_store_snapshot_is_not_injected_a_second_time(armed):
    with_block = "Base" + appsvc.STORE_OPENING + "### memory.md (current contents)\n\nkept\n"
    r = review(armed, with_block)
    gui_rpc.dispatch(armed, request(armed, "apply", token=r["token"]))
    assert armed._store_injected is True
    assert disk(armed)[0]["store_injected"] is True
    r = review(armed, "Plain text")
    gui_rpc.dispatch(armed, request(armed, "apply", token=r["token"]))
    assert armed._store_injected is False and disk(armed)[0]["store_injected"] is False


async def test_effect_states_the_real_consumer_per_backend(armed):
    revision = digest(armed.conversation[0]["content"])
    assert "sent with your next request" in gui_rpc.dispatch(armed, request(armed, "edit", revision=revision))["effect"]
    # The identity binding forbids switching backends under a live review, so ask the source function directly.
    stub = lambda **backend: SimpleNamespace(backend=SimpleNamespace(**backend))
    assert "sent with your next request" in native_system_prompt._effect(stub(name="codex"))
    assert "additional context on its existing thread" in native_system_prompt._effect(
        stub(name="codex", app_server=object()))
    assert "never sent to Claude" in native_system_prompt._effect(stub(name="claude"))


async def test_wrong_document_identity_fields_and_token_are_refused(armed, tmp_path):
    revision = digest(armed.conversation[0]["content"])
    with pytest.raises(ValueError, match="Only the conversation system prompt"):
        gui_rpc.dispatch(armed, request(armed, "edit", revision=revision, document="memory.md"))
    for extra in ({"path": str(tmp_path)}, {"work_instance": "x"}, {"agent_id": "x"}):
        with pytest.raises(ValueError):
            gui_rpc.dispatch(armed, {**request(armed, "edit", revision=revision), **extra})
    r = review(armed, "Valid")
    for bad in (request(armed, "apply", token="x"), request(armed, "readback", token=r["token"], apply_id="x")):
        with pytest.raises(ValueError):
            gui_rpc.dispatch(armed, bad)


async def test_oversize_prompt_is_not_editable(armed):
    armed.conversation[0]["content"] = "q" * (native_system_prompt.MAX_PROMPT + 1)
    with pytest.raises(ValueError, match="editing limit"):
        gui_rpc.dispatch(armed, request(armed, "edit", revision=digest("q")))


@pytest.mark.parametrize("command_app", [("custom", False)], indirect=True)
async def test_registered_system_command_routes_with_exact_arguments_and_never_mutates(command_app):
    app = command_app
    for token, entry in list(app.plugins.commands.items()):
        replacement = replace(entry, handler=forbidden)
        for alias in entry.tokens:
            app.plugins.commands[alias] = replacement
    catalog = command_surface.catalog(app, {"session_id": app.convo_id})
    row = next(r for r in catalog["entries"] if "/system" in r["tokens"])
    assert row["native_action"] == "system_prompt" and row["accepts_arguments"] is True
    base = {"session_id": app.convo_id, "version": 1, "agent_id": catalog["agent_id"], "snapshot": catalog["snapshot"]}
    before = copy.deepcopy(app.conversation)
    for command, arguments in [("/system", ""), ("/system Be terse.", "Be terse."),
                               ("/system  two\r\n\tlines 雪  ", "two\r\n\tlines 雪  ")]:
        route = command_surface.resolve(app, {**base, "command": command})
        assert (route["native_action"], route["arguments"], route["command"]) == ("system_prompt", arguments, command)
    for command, match in [("/system a\0b", "NUL"), ("/system " + "x" * 65536, "oversized")]:
        with pytest.raises(ValueError, match=match):
            command_surface.resolve(app, {**base, "command": command})
    with pytest.raises(ValueError, match="limit"):
        native_system_prompt.validate_argument("x" * (native_system_prompt.MAX_PROMPT + 1))
    assert app.conversation == before
