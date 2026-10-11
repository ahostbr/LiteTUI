"""The store is injected ONCE, not per turn (the user's ruling, 2026-08-19).

Every-turn injection is affordable at 1M context and is not on a local 27B.
The regression this guards is silent and expensive: the app still works, the
model still sees its store, and the context window just fills up faster than
anyone notices.
"""
import sys
import os
import tempfile
from pathlib import Path

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import _script_guard  # tests/ is sys.path[0] when a file is run as a script

# This script boots real owned Apps before pytest fixtures can run. Redirect
# settings first; make_app supplies a temporary full data root and explicit
# session for each App, with home-scoped memory and finally release below.
# Never read the developer's config or write a live agent home.
_script_guard.pin_first_boot_env()
_script_guard.redirect_live_settings()

# Deliberately a SECOND import block: the env pin and the settings redirect must
# run BETWEEN these imports, not before or after them.
from litetui import app as m
from litetui import paths
from litetui import appsvc, settings
from litetui.agent_launch_context import ordinary

_sandboxes = []
_apps = []

ok = []
#: The labels that FAILED. `ok` is bools, which is all an exit status needed; a
#: pytest arm has to be able to say WHICH check failed, and a bool cannot.
#: Recorded alongside rather than by changing `ok`, so `sum(ok)` / `all(ok)` /
#: `len(ok)` keep meaning exactly what they meant (T700).
failures: list[str] = []


def chk(label, cond):
    ok.append(bool(cond))
    if not cond:
        failures.append(label)
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")


def make_app(with_store=True):
    sandbox = tempfile.TemporaryDirectory(prefix="owned-store-")
    _sandboxes.append(sandbox)
    root = Path(sandbox.name)
    # Script imports execute before pytest fixtures: redirect the entire data
    # root here, not merely the historical transcript directory.
    os.environ["LITETUI_DATA_ROOT"] = str(root)
    paths.CONVO_DIR = root / ".convos"
    session = ordinary(root, settings.load())
    try:
        a = m.LiteTUI(agent_session=session)
    except BaseException:
        session.release()
        raise
    _apps.append(a)
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    # The staged child is materialized only for the transcript. Store files
    # belong to the owned agent home, never to a conversation child.
    a._materialise_convo()
    if with_store:
        (session.memory_root / "memory.md").write_text("- [thing](t.md) — a pointer\n", encoding="utf-8")
        (session.memory_root / "soul.md").write_text("Prefers `dir` over `ls` on Windows.\n", encoding="utf-8")
        (session.memory_root / "handoff.md").write_text("In flight: nothing.\n", encoding="utf-8")
    else:
        for n in ("memory.md", "soul.md", "handoff.md"):
            p = session.memory_root / n
            if p.exists():
                p.write_text("", encoding="utf-8")
    return a


def _run_checks():
    print("=== injected exactly once ===")
    a = make_app()
    chk("system message exists and has no store yet", m.STORE_HEADER not in a.conversation[0]["content"])

    first = a._request_messages()
    sys0 = first[0]["content"]
    chk("store landed in message 0", m.STORE_HEADER in sys0)
    chk("soul.md content is present", "Prefers `dir`" in sys0)
    chk("memory.md content is present", "a pointer" in sys0)

    second = a._request_messages()
    chk("second turn: content is IDENTICAL (not re-appended)", second[0]["content"] == sys0)
    chk("marker appears exactly once", second[0]["content"].count(m.STORE_HEADER) == 1)

    third = a._request_messages()
    chk("tenth-ish turn: still identical", third[0]["content"] == sys0)
    chk("no message count growth from injection", len(third) == len(first))

    print("\n=== the injected text does not promise something false ===")
    chk("does NOT claim a per-turn refresh", "next turn" not in sys0)
    chk("says it is a snapshot", "SNAPSHOT" in sys0 or "snapshot" in sys0)
    chk("tells the model how to get current contents", "`read` tool" in sys0)

    print("\n=== it is persisted, so a resume does not double it ===")
    a2 = make_app()
    a2._request_messages()
    saved = a2.conversation[0]["content"]
    # simulate a resume: a fresh object whose system message already carries the block
    a3 = make_app()
    a3.conversation[0] = {"role": "system", "content": saved}
    out = a3._request_messages()
    chk("resumed convo is not re-injected", out[0]["content"].count(m.STORE_HEADER) == 1)
    chk("resumed convo content unchanged", out[0]["content"] == saved)
    chk("a second request leaves it alone too", a3._request_messages()[0]["content"] == saved)

    print("\n=== an empty store injects nothing, and leaves no marker ===")
    b = make_app(with_store=False)
    msgs = b._request_messages()
    chk("no marker for an empty store", m.STORE_HEADER not in msgs[0]["content"])
    chk("still returns the conversation", msgs and msgs[0]["role"] == "system")
    (b._agent_session.memory_root / "soul.md").write_text("Written after the first request.\n", encoding="utf-8")
    later = b._request_messages()[0]["content"]
    chk("nothing latched, so a later write is still picked up", "Written after the first request." in later)
    chk("and the marker appears exactly once", later.count(m.STORE_HEADER) == 1)

    print("\n=== compaction still gets the LIVE view ===")
    c = make_app()
    live = appsvc.store_block(c, live=True)
    once = appsvc.store_block(c, )
    chk("live block reads 'as it stands right now'", "as it stands right now" in live)
    chk("live block is not the once-only text", m.STORE_HEADER not in live)
    chk("once block carries the marker", m.STORE_HEADER in once)
    chk("both carry the actual file contents", "Prefers `dir`" in live and "Prefers `dir`" in once)


_prior_data_root = os.environ.get("LITETUI_DATA_ROOT")
_prior_convo_dir = paths.CONVO_DIR
try:
    _run_checks()
finally:
    try:
        for app in reversed(_apps):
            try:
                app.store.release()
            finally:
                app._agent_session.release()
    finally:
        paths.CONVO_DIR = _prior_convo_dir
        for sandbox in reversed(_sandboxes):
            sandbox.cleanup()
        if _prior_data_root is None:
            os.environ.pop("LITETUI_DATA_ROOT", None)
        else:
            os.environ["LITETUI_DATA_ROOT"] = _prior_data_root


# ── the same checks, as a pytest arm (T700) ─────────────────────────────
#
# 🔴 THIS FILE IS NAMED `test_*` AND NOTHING HAS EVER RUN IT. A module-level
# `sys.exit` raises SystemExit during collection, which pytest reports as
# INTERNALERROR and which abandons the WHOLE invocation — not just this file.
# Ten files in this directory were in that state (T699 fixed two, T700 the
# rest); each abort hid the others, which is why the class kept looking small.


def test_every_check_in_this_file_passed() -> None:
    assert ok, "no check ran — the body above did not execute"
    assert failures == [], failures


if __name__ == "__main__":
    print(f"\n{sum(ok)}/{len(ok)} passed")
    sys.exit(0 if all(ok) else 1)
