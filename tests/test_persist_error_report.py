"""`_report_persist_error` — the app's half of "the store speaks", untested.

🔴 WHY THIS FILE EXISTS: found BLIND by mutation. With `_report_persist_error`
neutralised to a no-op — a save failure never reaches the user at all — the FULL
suite passed: 96 pytest files + 13 scripts, REAL EXIT 0. It is a CALLBACK, wired
once at `app.py:900` as `ConversationRepository(on_error=self._report_persist_error)`
and never called by name, so nothing accidentally covered it.

⚠️ THE NEGATIVE TESTS HERE CANNOT FAIL ALONE, AND THAT IS THE POINT OF PAIRING
THEM. "does not raise when the widget is not mounted" and "reports only once"
are both satisfied PERFECTLY BY A DEAD METHOD — a no-op raises nothing and
reports nothing. Each is meaningful only beside a positive test asserting the
message IS delivered. A suite of absence-assertions measures nothing; it was a
one-sided mutation under-reporting on exactly this shape that made the pairing
explicit here.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import app as app_mod
from litetui.conversation import ConversationRepository

BANNER = "[save failed"


class _App:
    """Borrows the real method. Nothing else about LiteTUI is needed."""

    _report_persist_error = app_mod.LiteTUI._report_persist_error

    def __init__(self, raises=False):
        self.said = []
        self._raises = raises

    def _system(self, text):
        if self._raises:
            raise RuntimeError("not mounted yet")
        self.said.append(text)


# ── the positive arm: the message actually arrives ──────────────────────────

def test_a_save_failure_reaches_the_user():
    a = _App()
    a._report_persist_error("OSError: disk went away")
    assert len(a.said) == 1
    assert BANNER in a.said[0]
    assert "disk went away" in a.said[0], "the store's own text must survive"


def test_it_says_the_conversation_is_memory_only():
    """The banner is the whole point: the turn still works, it is just not
    being saved. A failure the user cannot see is a conversation they think
    is on disk."""
    a = _App()
    a._report_persist_error("boom")
    assert "memory-only" in a.said[0]


# ── the negative arm: PAIRED, see the module docstring ──────────────────────

def test_it_survives_the_widget_not_being_mounted():
    """`_system` raises before the log exists; a save failure must never take
    down the turn that noticed it.

    ⚠️ PAIRED — a dead method passes this too. The two tests above are what
    distinguish 'swallowed the exception' from 'never tried'."""
    a = _App(raises=True)
    a._report_persist_error("boom")          # must not raise
    assert a.said == []


# ── the wiring, which is the reason this was invisible ──────────────────────

def test_the_store_is_constructed_with_this_as_its_error_route():
    """It is reached ONLY as a callback reference. If the wiring is dropped the
    method still exists, still passes every test above, and never runs again --
    so the wiring needs its own assertion."""
    import inspect
    src = inspect.getsource(app_mod.LiteTUI.__init__)
    assert "on_error=self._report_persist_error" in src


def test_the_real_store_routes_a_failure_through_it_once():
    """End to end on the REAL ConversationRepository wired to the REAL method:
    `_raise_to_app` consults `note_error`, which yields text only for the FIRST
    failure, and hands it to `on_error`.

    🔴 THE `on_error` HERE IS `_report_persist_error` ITSELF, NOT A SPY. An
    earlier draft passed `seen.append` — which made the test pass against a
    GUTTED method, because it never ran the method at all. A test named for a
    route has to travel the route; wiring a stand-in into the seam under test
    measures the seam and nothing else.

    ⚠️ The 'once' half is the paired-absence shape again: a dead callback also
    produces 'not twice'. The first assertion is what makes the second mean
    something."""
    a = _App()
    store = ConversationRepository(on_error=a._report_persist_error)
    store._raise_to_app(OSError("disk went away"))
    assert len(a.said) == 1, "the FIRST failure is reported"
    assert BANNER in a.said[0] and "disk went away" in a.said[0]
    store._raise_to_app(OSError("and again"))
    assert len(a.said) == 1, "a store that has gone away must not bury the turn"
