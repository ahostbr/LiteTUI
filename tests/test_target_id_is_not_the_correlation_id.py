"""T633 — the correlation id and the target id stop sharing one key.

🔴 THE OVERLOAD, AND WHY IT WAS A ONE-LINE BREAK OF TWO FEATURES. `id` on an RPC
command is the CORRELATION id: "reply to me on this", read at the top of
`_dispatch` and echoed by `_respond`. For `answer` and `approve` — the only two
commands that name something they RESOLVE — the same key was also read as the
target. A host that built its envelope with the correlation id last
(`LiteTuiAdapter.sendCommand`, before LiteSuite a7b904826) therefore retargeted
every Answer and every Allow at its own `cmd_N`, and this child refused them
CORRECTLY while nothing anywhere said the two ids were the same field. Ryan's
walks at 14:07 and 14:17 on 2026-09-xx: the card never cleared, the turn never
continued, and the child's log said "no ask is waiting on id 'cmd_3'".

    A CORRELATION ID IS UNIQUE PER COMMAND; AN ASK ID IS UNIQUE PER QUESTION.
    One key cannot be both without making one of them wrong.

⚠️ THE HOST FIX AT a7b904826 STOPPED THE CLOBBER; IT DID NOT REMOVE THE
OVERLOAD. The two meanings still share `id` on the wire, so the next host that
touches its envelope can do it again, and — measured while scouting this card —
`state.pending` on the host is keyed on that same value, giving `answer` and
`approve` no correlation id of their own at all. This is the child half: it
makes the target NAMEABLE. The host half (LiteSuite) is what makes it named.

⬜ COMPATIBILITY IS THE WHOLE REASON THIS IS TWO LANDINGS. The child must accept
the new key BEFORE any host sends it, or the first host build to ship it talks
to a child that ignores it and every Answer misses. So `id` keeps working, and
the arm that says so is not a courtesy — it is the ordering constraint, written
down where it will go red.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import ask_user_question as auq
from litetui import rpc as rpc_mod

ONE_QUESTION = {
    "questions": [
        {
            "label": "Approach",
            "question": "Which approach?",
            "options": [{"title": "A"}, {"title": "B"}],
        }
    ]
}


class FakeRpcApp:
    """The wire and the liveness flag — the same shape T632's arms use."""

    def __init__(self) -> None:
        self._rpc = True
        self.is_running = True
        self.emitted: list[dict] = []

    def _rpc_emit(self, data: dict) -> None:
        self.emitted.append(data)


def _ask_in_thread(app, args=ONE_QUESTION):
    out: list[str] = []
    t = threading.Thread(target=lambda: out.append(auq.run(args, app)), daemon=True)
    t.start()
    return t, out


def _wait_for_ask(app, timeout: float = 3.0) -> dict:
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if app.emitted:
            return app.emitted[0]
        time.sleep(0.01)
    raise AssertionError("no user_input_requested was emitted")


def _join(t, out, timeout: float = 3.0) -> str:
    t.join(timeout=timeout)
    assert not t.is_alive(), "the parked tool call never returned"
    return out[0] if out else ""


# ---------------------------------------------------------------------------
# the reader, in isolation
# ---------------------------------------------------------------------------

def test_the_dedicated_key_is_read_when_present() -> None:
    assert rpc_mod._target_id({"ask_id": "ask_7"}, "ask_id") == ("ask_7", "ask_id")


def test_a_bare_id_still_names_the_target() -> None:
    """The compatibility path, and the reason the child lands first.

    A host that has not been updated sends only `id`. If this stopped working,
    every existing build's Answer and Allow would miss the moment this merged.
    """
    assert rpc_mod._target_id({"id": "ask_7"}, "ask_id") == ("ask_7", "id")


def test_the_dedicated_key_WINS_when_the_two_disagree() -> None:
    """🔴 THE T558 CLOBBER, NOW HARMLESS.

    A disagreement between the two is not ambiguity to be resolved carefully —
    it is the exact signature of an envelope that overwrote the target with a
    correlation id. The dedicated key is the one the caller chose ON PURPOSE.
    """
    assert rpc_mod._target_id({"id": "cmd_3", "ask_id": "ask_7"}, "ask_id") == (
        "ask_7",
        "ask_id",
    )


def test_an_empty_dedicated_key_falls_through_rather_than_blanking_the_target() -> None:
    """`""` is not a choice. A host that sends the key empty has said nothing,
    and blanking the target there would turn a working bare-`id` command into a
    refusal for a reason nobody could see."""
    assert rpc_mod._target_id({"id": "ask_7", "ask_id": ""}, "ask_id") == ("ask_7", "id")


def test_neither_key_is_the_empty_answer_the_caller_must_refuse() -> None:
    assert rpc_mod._target_id({"type": "answer"}, "ask_id") == ("", "id")


# ---------------------------------------------------------------------------
# answer, end to end over the dispatch
# ---------------------------------------------------------------------------

def test_answer_resolves_the_ask_named_by_ask_id(monkeypatch) -> None:
    """🔴 THE CASE THAT WAS IMPOSSIBLE TO EXPRESS BEFORE: a correlation id and a
    target that are DIFFERENT VALUES, both present, both honoured."""
    replies: list[dict] = []
    monkeypatch.setattr(rpc_mod, "rpc_emit", lambda d: replies.append(d))

    app = FakeRpcApp()
    t, out = _ask_in_thread(app)
    ask = _wait_for_ask(app)

    rpc_mod._dispatch(
        app,
        {
            "type": "answer",
            "id": "cmd_3",
            "ask_id": ask["id"],
            "action": "submit",
            "answers": [{"selected": [1], "note": "B"}],
        },
    )

    reply = next(r for r in replies if r.get("id") == "cmd_3")
    assert reply["ok"] is True, reply
    assert reply["result"]["answered"] == ask["id"]
    # WHICH KEY IT READ, in the reply. A host whose envelope is clobbering the
    # target can see that here instead of deducing it from a refusal about an
    # id it never chose.
    assert reply["result"]["id_key"] == "ask_id"
    assert "SUBMITTED" in _join(t, out)


def test_answer_still_works_with_a_bare_id(monkeypatch) -> None:
    """The compatibility arm at the dispatch level. THIS IS THE ONE THAT MUST
    NOT GO RED: every shipped host sends exactly this shape today."""
    replies: list[dict] = []
    monkeypatch.setattr(rpc_mod, "rpc_emit", lambda d: replies.append(d))

    app = FakeRpcApp()
    t, out = _ask_in_thread(app)
    ask = _wait_for_ask(app)

    rpc_mod._dispatch(
        app,
        {
            "type": "answer",
            "id": ask["id"],
            "action": "submit",
            "answers": [{"selected": [0]}],
        },
    )

    reply = next(r for r in replies if r.get("id") == ask["id"])
    assert reply["ok"] is True, reply
    assert reply["result"]["id_key"] == "id"
    assert "SUBMITTED" in _join(t, out)


def test_the_refusal_names_the_key_it_looked_in(monkeypatch) -> None:
    """🔴 THE T558 MESSAGE WAS TRUE, CORRECT AND USELESS.

    "no ask is waiting on id 'cmd_3'" described a value the host never chose as
    a target, so it read as a child fault. Naming the KEY is what lets a reader
    join the refusal to the thing they sent.
    """
    replies: list[dict] = []
    monkeypatch.setattr(rpc_mod, "rpc_emit", lambda d: replies.append(d))

    app = FakeRpcApp()
    rpc_mod._dispatch(
        app, {"type": "answer", "id": "cmd_4", "ask_id": "nobody", "action": "submit"}
    )

    reply = next(r for r in replies if r.get("id") == "cmd_4")
    assert reply["ok"] is False
    assert "ask_id='nobody'" in reply["error"], reply["error"]


def test_a_missing_target_says_which_keys_it_accepts(monkeypatch) -> None:
    """⚠️ REACHABLE ONLY WITH BOTH KEYS ABSENT, and the first draft of this
    arm got that wrong — it sent a correlation id and no `ask_id` and expected
    the "you named nothing" error. It cannot fire there, and that is a property
    of the compatibility path rather than a gap:

        WHILE A BARE `id` IS A VALID TARGET, A BARE `id` THAT WAS MEANT AS A
        CORRELATION ID IS INDISTINGUISHABLE FROM ONE THAT WAS MEANT AS A TARGET.

    The child cannot tell an old host from a new host that forgot `ask_id`, so
    it does the only honest thing: treats it as the target and refuses by name.
    That is the T558 shape exactly, and it is why the fallback is a MIGRATION
    rather than a permanent feature, and why the reply carries `id_key`.
    """
    replies: list[dict] = []
    monkeypatch.setattr(rpc_mod, "rpc_emit", lambda d: replies.append(d))

    app = FakeRpcApp()
    rpc_mod._dispatch(app, {"type": "answer", "action": "submit"})

    reply = replies[-1]
    assert reply["ok"] is False
    assert "ask_id" in reply["error"] and "id" in reply["error"]


def test_a_bare_correlation_id_is_TREATED_AS_A_TARGET_and_refused_by_name(
    monkeypatch,
) -> None:
    """The other half of the arm above, stated as behaviour rather than a gap.

    This is precisely what T558 looked like from the child's side, and it still
    looks like that to a host that has not adopted `ask_id` — the difference is
    that the refusal now names the key, so the reader can see the child took
    `cmd_5` as a TARGET rather than wonder why it mentioned an id they only ever
    used as an envelope.
    """
    replies: list[dict] = []
    monkeypatch.setattr(rpc_mod, "rpc_emit", lambda d: replies.append(d))

    app = FakeRpcApp()
    rpc_mod._dispatch(app, {"type": "answer", "id": "cmd_5", "action": "submit"})

    reply = next(r for r in replies if r.get("id") == "cmd_5")
    assert reply["ok"] is False
    assert "id='cmd_5'" in reply["error"], reply["error"]


# ---------------------------------------------------------------------------
# approve — the second half of the same one-line break
# ---------------------------------------------------------------------------

def test_approve_resolves_the_request_named_by_approval_id(monkeypatch) -> None:
    """T577's approval card died of the SAME line. Both commands, both keys."""
    replies: list[dict] = []
    monkeypatch.setattr(rpc_mod, "rpc_emit", lambda d: replies.append(d))

    resolved: list[tuple] = []
    from litetui import tool_approval as approval_mod

    monkeypatch.setattr(
        approval_mod,
        "resolve_over_rpc",
        lambda app, approval_id, allow, remember=False: (
            resolved.append((approval_id, allow, remember)) or True
        ),
    )

    app = FakeRpcApp()
    rpc_mod._dispatch(
        app,
        {
            "type": "approve",
            "id": "cmd_8",
            "approval_id": "appr_2",
            "allow": True,
        },
    )

    assert resolved == [("appr_2", True, False)]
    reply = next(r for r in replies if r.get("id") == "cmd_8")
    assert reply["ok"] is True
    assert reply["result"]["approved"] == "appr_2"
    assert reply["result"]["id_key"] == "approval_id"


def test_approve_still_works_with_a_bare_id(monkeypatch) -> None:
    replies: list[dict] = []
    monkeypatch.setattr(rpc_mod, "rpc_emit", lambda d: replies.append(d))

    resolved: list[tuple] = []
    from litetui import tool_approval as approval_mod

    monkeypatch.setattr(
        approval_mod,
        "resolve_over_rpc",
        lambda app, approval_id, allow, remember=False: (
            resolved.append((approval_id, allow, remember)) or True
        ),
    )

    app = FakeRpcApp()
    rpc_mod._dispatch(app, {"type": "approve", "id": "appr_9", "allow": False})

    assert resolved == [("appr_9", False, False)]
    reply = next(r for r in replies if r.get("id") == "appr_9")
    assert reply["result"]["id_key"] == "id"


def test_approve_refusal_names_the_key_too(monkeypatch) -> None:
    replies: list[dict] = []
    monkeypatch.setattr(rpc_mod, "rpc_emit", lambda d: replies.append(d))

    from litetui import tool_approval as approval_mod

    monkeypatch.setattr(
        approval_mod, "resolve_over_rpc", lambda *a, **k: False
    )

    app = FakeRpcApp()
    rpc_mod._dispatch(
        app,
        {"type": "approve", "id": "cmd_6", "approval_id": "gone", "allow": True},
    )

    reply = next(r for r in replies if r.get("id") == "cmd_6")
    assert reply["ok"] is False
    assert "approval_id='gone'" in reply["error"], reply["error"]


def test_CONTROL_approve_still_refuses_a_missing_allow(monkeypatch) -> None:
    """The separation must not have loosened the OTHER guard. `allow` is still
    not defaulted, because both guesses are wrong: deny ends someone's turn on a
    malformed message, allow runs a tool nobody approved."""
    replies: list[dict] = []
    monkeypatch.setattr(rpc_mod, "rpc_emit", lambda d: replies.append(d))

    app = FakeRpcApp()
    rpc_mod._dispatch(app, {"type": "approve", "id": "cmd_7", "approval_id": "appr_1"})

    reply = next(r for r in replies if r.get("id") == "cmd_7")
    assert reply["ok"] is False
    assert "allow" in reply["error"]


def test_CONTROL_a_command_with_no_target_is_unaffected(monkeypatch, tmp_path) -> None:
    """`prompt` has one id and one meaning. An arm that only ever looked at
    answer/approve could not tell a separation from a change to every command."""
    replies: list[dict] = []
    monkeypatch.setattr(rpc_mod, "rpc_emit", lambda d: replies.append(d))
    from litetui import paths
    from litetui.conversation import ConversationRepository
    monkeypatch.setattr(paths, "CONVO_DIR", tmp_path / ".convos")

    class PromptApp(FakeRpcApp):
        def __init__(self) -> None:
            super().__init__()
            self.submitted: list[str] = []
            self.store = ConversationRepository()
            self.store.stage("fixture")

        def _submit_text(self, text: str, alt_chord: bool = False, *, source="typed") -> None:
            assert source == "rpc"
            self.submitted.append(text)

    app = PromptApp()
    rpc_mod._dispatch(app, {"type": "prompt", "id": "cmd_1", "message": "hello"})

    reply = next(r for r in replies if r.get("id") == "cmd_1")
    assert reply["ok"] is True
    assert app.submitted == ["hello"]
