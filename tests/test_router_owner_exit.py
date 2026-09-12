"""T688 F — when the router's OWNER exits, the attached instance is stranded.

LiteTUI attaches to a router another APP owns — in practice LiteSuite's — via
`llm_backend._ensure_running_sync`'s `record is not None and not record.is_mine`
branch. That part is correct and already has arms in
`test_router_coexistence.py`.

⚠️ THIS FILE IS ABOUT THE LiteTUI -> LiteSuite CASE, AND DELIBERATELY NOT ABOUT
TWO LiteTUIs. Measured 2026-09-12: `router_record` accepts exactly two owner
values, "litesuite" and "litetui" (`_OWNERS`), and `is_mine` is
`owner == OWNER_SELF` — so the record names the APP, not the INSTANCE. A second
LiteTUI reads the first's live record, `is_mine` answers True, and it never
attaches at all (`attached=False`, router fully manageable). The stranding this
file fixes is therefore unreachable between two LiteTUIs; what happens there
instead is worse and is reported separately, because the record cannot tell a
crashed instance's orphaned server from a live sibling's.

🔴 WHAT NOBODY OWNS IS THE OWNER LEAVING. `shutdown()` tree-kills the server it
spawned and calls `router_record.remove_if_mine` — so from the attached
instance's side the host stops answering and the record it attached to simply
disappears. Its `_attached_host` still points at a dead port, and nothing puts
it back: `ensure_running` is called from `connect()` and from the preset
rebuild, neither of which a failed chat turn reaches. The user meets "The
llama.cpp server seems closed — start it, or switch backends", which is advice
for a situation that is not theirs: nothing needs starting by hand, because this
instance is perfectly able to spawn its own.

⚠️ EVERY ARM RUNS ON A PATCHED `record_path`. That file is
`~/.litesuite/llm/router.json` — NOT under the data root, and SHARED WITH
LiteSuite — so a redirected LITETUI_DATA_ROOT is not isolation. See the fixture.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from litetui import llm_backend, router_record
from litetui import settings as st


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """🔴 `record_path()` IS NOT UNDER THE DATA ROOT — it is
    `~/.litesuite/llm/router.json`, SHARED WITH LiteSuite. A first draft of this
    file redirected LITETUI_DATA_ROOT and believed that was isolation; it would
    have written and unlinked the record Ryan's two live instances are using.
    The only thing that isolates it is patching the function itself, which is
    what `test_router_coexistence.py` already does."""
    path = tmp_path / "router.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(router_record, "record_path", lambda: path)
    return tmp_path


def _backend(host: str = "http://127.0.0.1:7470") -> llm_backend.LlamaCppBackend:
    s = st.Settings()
    s.llama_host = host
    s.llama_attach_hosts = []
    return llm_backend.LlamaCppBackend(s)


def _attach_to_a_foreign_owner(monkeypatch: pytest.MonkeyPatch, b) -> None:
    """Put `b` in the state the second instance reaches: attached, owner live."""
    router_record.write(pid=os.getpid(), port=7470, ini="x.ini", owner="litesuite")
    # "litesuite" is the only FOREIGN owner this record can express, and it is
    # foreign for real — nothing about ownership is stubbed here.
    assert router_record.read() is not None
    monkeypatch.setattr(router_record, "is_live", lambda r: True)
    monkeypatch.setattr(llm_backend, "_healthy", lambda host: True)
    monkeypatch.setattr(llm_backend.LlamaCppBackend, "_probe_shape", lambda self: "router")
    b._ensure_running_sync()


def test_the_attached_instance_reaches_the_attached_state(home, monkeypatch) -> None:
    """The control. Without it, every arm below could pass on a backend that
    simply never attached, and "not stranded" would mean "never boarded"."""
    b = _backend()
    _attach_to_a_foreign_owner(monkeypatch, b)
    assert b.attached is True
    assert b.attached_owner == "litesuite"


def test_owner_exit_is_recognised_as_such_and_not_as_a_closed_server(
    home, monkeypatch
) -> None:
    """The measurement: owner gone, port dead — does anything say so?

    `owner_exited()` is the predicate that separates this from the situation the
    error copy currently describes. It must be FALSE while the owner is alive
    (or the app would offer to take over a server that is working) and TRUE only
    when this instance is attached, the record is gone, and the host has stopped
    answering.
    """
    b = _backend()
    _attach_to_a_foreign_owner(monkeypatch, b)
    assert b.owner_exited() is False, "the owner is alive and the port answers"

    # The owner quits: tree-kill plus remove_if_mine.
    router_record.record_path().unlink()
    monkeypatch.setattr(llm_backend, "_healthy", lambda host: False)

    assert b.owner_exited() is True


def test_a_dead_port_with_the_owner_STILL_REGISTERED_is_not_an_owner_exit(
    home, monkeypatch
) -> None:
    """The discriminator, and the reason `owner_exited` reads two things.

    A server can stop answering while its owner is very much alive — mid-restart,
    swapping a model, a long stall. Taking that over would spawn a SECOND router
    on a port another app still claims, which is the collision the whole
    ownership record exists to prevent.
    """
    b = _backend()
    _attach_to_a_foreign_owner(monkeypatch, b)
    monkeypatch.setattr(llm_backend, "_healthy", lambda host: False)
    assert b.owner_exited() is False, "the record is still there — the owner lives"


def test_an_OWNING_instance_never_reports_an_owner_exit(home, monkeypatch) -> None:
    """We are the owner. Our own dead server is a crash to report, not a
    takeover to perform — `recover_owner_exit` must not fire for it."""
    b = _backend()
    monkeypatch.setattr(llm_backend, "_healthy", lambda host: False)
    assert b.attached is False
    assert b.owner_exited() is False


def test_recovery_becomes_the_owner_and_says_so(home, monkeypatch) -> None:
    """The fix: re-enter `ensure_running`, which spawns, and return a line the
    status bar can print. The point of the message is that the old copy was
    WRONG for this case — nothing needs starting by hand."""
    b = _backend()
    _attach_to_a_foreign_owner(monkeypatch, b)
    router_record.record_path().unlink()
    monkeypatch.setattr(llm_backend, "_healthy", lambda host: False)

    spawned: list[bool] = []

    def _fake_spawn(self):
        spawned.append(True)
        # What a real spawn leaves behind: we own it, nothing is attached.
        self._attached_host = None
        self._attached_owner = None
        return "spawned pid 4242"

    monkeypatch.setattr(llm_backend.LlamaCppBackend, "_spawn", _fake_spawn)

    said = b.recover_owner_exit()

    assert spawned == [True], "it must actually re-enter the spawn path"
    assert b.attached is False, "this instance is the owner now"
    assert said is not None and "own" in said.lower()

    # And it is not a thing that fires twice: the second call has nothing to do.
    assert b.owner_exited() is False
    assert b.recover_owner_exit() is None
