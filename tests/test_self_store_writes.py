"""The agent may always write ITS OWN STORE. T083, P1.

Ryan, watching a LiteTUI seat compact: "the agent just got denied writing to
the workspace during compaction this must never happen".

WHAT HE SAW:
    write .convos/<id>/handoff.md  -> [policy denied] scheduled profile does
    write .convos/<id>/memories/*     not grant workspace_write.

🔴 THE REFUSAL WAS CORRECT AND THAT IS THE PROBLEM. It refused perfectly, told
the model not to retry and not to ask, and the turn completed looking fine. The
durable state simply was not there on the far side. **A guard that fails by
quietly discarding exactly the thing it was protecting is worse than no guard.**

THE MECHANISM. Compaction is the agent writing down what survives it, and it
runs on whatever profile the LAST TURN left behind — nothing in `_compact` sets
one. So:

    after a scheduled turn   -> `scheduled`   -> read-only  -> DENIED, silently
    after an ordinary turn   -> `interactive` -> CONFIRM    -> an approval modal
                                                               MID-COMPACTION

Both are the same root cause as the AUTONOMOUS hang: a profile meeting a path
it was never designed for. The fix is structural, not a special case — the
convo directory is the agent's own store, not the user's workspace, and
`classify_write` could not tell those apart because it only asked
inside-workspace vs outside.

⚠️ CONTROL 2 IS THE ONE THAT MATTERS (Sentinel): a write to `src/**` must STILL
be refused under `scheduled`. Without it this fix is indistinguishable from
just granting `workspace_write`, which is the failure mode rather than the fix.
"""
from __future__ import annotations

import pytest

from litetui import paths
from litetui.tool_policy import (
    ALLOW,
    AUTONOMOUS,
    CONFIRM,
    DENY,
    INTERACTIVE,
    SCHEDULED,
    SELF_STORE,
    WORKSPACE_WRITE,
    WRITE_POLICY,
    classify_write,
    evaluate,
)

ROOT = paths.ROOT


def _store():
    """Read at CALL time, never captured at import.

    🔴 Many test modules reassign `paths.CONVO_DIR` to a temp dir when they
    import, so a module-level constant here binds to whichever directory
    happened to be current when THIS file was imported — and whether that
    matches what the classifier sees depends on collection order. It passed
    alone and failed in the suite, which is the same snapshot trap the
    classifier itself was fixed for one commit earlier.
    """
    return paths.CONVO_DIR


def _act(profile, path):
    return evaluate(profile, WRITE_POLICY, {"path": str(path)}, ROOT, tool_name="write").action


def _caps(path):
    return set(classify_write({"path": str(path)}, ROOT))


# ── control 1: the agent can persist itself, on every profile ──────────────

@pytest.mark.parametrize("profile", [SCHEDULED, INTERACTIVE, AUTONOMOUS])
@pytest.mark.parametrize(
    "rel",
    ["abc123/handoff.md", "abc123/memory.md", "abc123/soul.md",
     "abc123/memories/i-learned-this.md", "abc123/convo.jsonl"],
)
def test_the_agent_may_always_write_its_own_store(profile, rel):
    assert _act(profile, _store() / rel) == ALLOW


def test_the_scheduled_denial_ryan_saw_is_gone():
    """The exact two paths from his screenshot."""
    assert _act(SCHEDULED, _store() / "abc123" / "handoff.md") == ALLOW
    assert _act(SCHEDULED, _store() / "abc123" / "memories" / "tool-policy-denials.md") == ALLOW


def test_an_interactive_compaction_does_not_open_a_modal():
    """The half nobody reported. Under `interactive` a self-store write was
    CONFIRM, so an autocompact firing at 80% context interrupted with an
    approval modal — a prompt at a moment no one expects to be asked."""
    assert _act(INTERACTIVE, _store() / "abc123" / "handoff.md") == ALLOW


# ── control 2: THE ONE THAT MATTERS ────────────────────────────────────────

def test_the_workspace_is_STILL_refused_under_scheduled():
    """Without this, the fix is indistinguishable from granting workspace_write
    to every cron and /loop turn — which is the failure mode, not the fix."""
    for rel in ("src/litetui/app.py", "settings.json", "prompts/systemprompt.md"):
        assert _act(SCHEDULED, ROOT / rel) == DENY, rel


def test_the_workspace_still_CONFIRMS_under_interactive():
    assert _act(INTERACTIVE, ROOT / "src" / "litetui" / "app.py") == CONFIRM


def test_outside_the_workspace_is_untouched():
    outside = ROOT.parent / "not-mine" / "x.txt"
    assert _act(SCHEDULED, outside) == DENY
    assert _act(INTERACTIVE, outside) == CONFIRM


# ── control 3: the escape ──────────────────────────────────────────────────

@pytest.mark.parametrize(
    "escape",
    [
        "abc123/../../src/litetui/app.py",
        "abc123/../../../outside.txt",
        "abc123/./../../settings.json",
    ],
)
def test_a_path_escaping_the_store_is_not_a_self_store_write(escape):
    """`_resolve_path` resolves BEFORE either test, so a path that climbs out
    of the store is classified as whatever it actually reaches."""
    target = _store() / escape
    assert SELF_STORE not in _caps(target), f"{escape} escaped into the store"
    assert _act(SCHEDULED, target) == DENY


# ── the classifier's three answers ─────────────────────────────────────────

def test_the_classifier_has_three_answers_not_two():
    assert _caps(_store() / "abc" / "handoff.md") == {SELF_STORE}
    assert _caps(ROOT / "src" / "x.py") == {WORKSPACE_WRITE}
    assert SELF_STORE not in _caps(ROOT.parent / "x.py")


def test_the_store_check_comes_FIRST_when_the_store_is_INSIDE_the_workspace(monkeypatch):
    """The ordering test, and it needs the PRODUCTION layout to be meaningful.

    In production `.convos` lives inside the repo, so a self-store path is ALSO
    a workspace path. If `classify_write` asked "inside the workspace?" first it
    would answer yes for every self-store write and the third case would be
    unreachable — the fix would look present and do nothing.

    🔴 THE STORE IS PINNED HERE ON PURPOSE. Many test modules redirect
    `paths.CONVO_DIR` to a temp dir OUTSIDE the repo, and under that layout the
    two tests cannot overlap, so the ordering is not exercised at all: the test
    would pass without ever posing the question. Pinning the production shape
    is what makes this an assertion rather than an accident of collection order.

    ⚠️ DO NOT "TIDY" THIS TO USE `_store()` LIKE ITS NEIGHBOURS. Under the
    redirected fixture the store sits outside the repo, the two cases cannot
    overlap, and this test would still pass — while no longer asking the
    question it exists to ask. It is the only thing standing between a
    reversed ordering and a fix that looks present and does nothing.
    """
    production_store = ROOT / ".convos"
    monkeypatch.setattr(paths, "CONVO_DIR", production_store)
    assert production_store.resolve().is_relative_to(ROOT.resolve()), (
        "premise changed: .convos is no longer inside the repo"
    )
    target = production_store / "abc" / "handoff.md"
    assert _caps(target) == {SELF_STORE}, (
        "a path inside BOTH the store and the workspace classified as the "
        "workspace — the store check is no longer first"
    )
    assert _act(SCHEDULED, target) == ALLOW


def test_self_store_is_a_declared_capability():
    """A capability the vocabulary does not know raises in `classify`, so this
    would fail loudly rather than mysteriously — but the failure would be at
    the first write, not at import."""
    from litetui.tool_policy import CAPABILITIES
    assert SELF_STORE in CAPABILITIES
