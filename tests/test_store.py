"""The store is injected ONCE, not per turn (Ryan's ruling, 2026-08-19).

Every-turn injection is affordable at 1M context and is not on a local 27B.
The regression this guards is silent and expensive: the app still works, the
model still sees its store, and the context window just fills up faster than
anyone notices.
"""
import sys
import tempfile
from pathlib import Path

# The repo root, one level up since the tests moved into tests/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import app as m

m.CONVO_DIR = Path(tempfile.mkdtemp(prefix="convos-store-"))

ok = []


def chk(label, cond):
    ok.append(bool(cond))
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")


def make_app(with_store=True):
    a = m.LiteTUI()
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    # Conversations are created LAZILY (on the first user message), so the
    # directory does not exist yet. These tests are about store INJECTION, not
    # about when the store is born — materialise it so there is one to inject.
    a._materialise_convo()
    if with_store:
        (a.convo_dir / "memory.md").write_text("- [thing](t.md) — a pointer\n", encoding="utf-8")
        (a.convo_dir / "soul.md").write_text("Prefers `dir` over `ls` on Windows.\n", encoding="utf-8")
        (a.convo_dir / "handoff.md").write_text("In flight: nothing.\n", encoding="utf-8")
    else:
        for n in ("memory.md", "soul.md", "handoff.md"):
            p = a.convo_dir / n
            if p.exists():
                p.write_text("", encoding="utf-8")
    return a


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
a3._store_injected = False          # the flag dies with the process; the MARKER must carry it
out = a3._request_messages()
chk("resumed convo is not re-injected", out[0]["content"].count(m.STORE_HEADER) == 1)
chk("resumed convo content unchanged", out[0]["content"] == saved)
chk("flag was set from the marker alone", a3._store_injected is True)

print("\n=== an empty store injects nothing, and leaves no marker ===")
b = make_app(with_store=False)
msgs = b._request_messages()
chk("no marker for an empty store", m.STORE_HEADER not in msgs[0]["content"])
chk("still returns the conversation", msgs and msgs[0]["role"] == "system")
chk("not latched, so a later write can still be picked up", b._store_injected is False)

print("\n=== compaction still gets the LIVE view ===")
c = make_app()
live = c._store_block(live=True)
once = c._store_block()
chk("live block reads 'as it stands right now'", "as it stands right now" in live)
chk("live block is not the once-only text", m.STORE_HEADER not in live)
chk("once block carries the marker", m.STORE_HEADER in once)
chk("both carry the actual file contents", "Prefers `dir`" in live and "Prefers `dir`" in once)

print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
