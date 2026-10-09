"""One owned test seat for every test that builds the app.

The app's constructor refuses to build without an owned agent session, so a
test that wrote `LiteTUI()` never reached the code it is there to guard. This
is the one place a test gets a real seat:

    from _owned_seat import owned_app
    app = owned_app()                 # was: LiteTUI()

The seat is opened the way the CLI opens it: `agent_launch_context.ordinary`
over `paths.data_root()` with the settings the app itself would load. conftest
points the data root at the test's tmp_path, so the seat's home lives there and
goes away with it. Nothing is patched: the constructor's guard, the store and
the session are the product's own.

Release is per test, whichever module built the app: `tests/conftest.py` calls
`release_all()` after every test.

What it does not do: it starts no conversation and persists nothing, as a
LiteTUI that has not been sent a prompt has not. A test that needs a turn inside
a persisted conversation (the approval audit does) sets that up itself; see
`approval_store_fixture_t0340.bind_origin`.
"""
from __future__ import annotations

_OPEN: list = []    # the sessions the current test opened


def owned_app(*args, **kwargs):
    """`LiteTUI(*args, **kwargs)` on an owned test seat in this test's data root.

    A second app in one test finds the default seat held and gets a seat of its
    own, as a second LiteTUI on one data root does."""
    from litetui import app as app_mod
    from litetui import paths, settings
    from litetui.agent_launch_context import ordinary

    session = ordinary(paths.data_root(), settings.load(), notice=lambda _text: None,
                       backend=kwargs.get("initial_backend"), model=kwargs.get("initial_model"))
    _OPEN.append(session)
    return app_mod.LiteTUI(*args, agent_session=session, **kwargs)


def release_all() -> None:
    while _OPEN:
        _OPEN.pop().release()
