"""The router ownership record — the one fact that stops two apps restarting
each other's llama-server.

Everything here is about a file two DIFFERENT programs read and write, so the
tests care most about the cases where the file is not what we hoped: absent,
truncated mid-write, hand-edited, or naming a process that has since died.
"""

import json
import os
import sys

import pytest

from litetui import router_record as rr


def _rec(tmp_path, **over):
    """A record file with sane defaults, overridable field by field."""
    payload = {
        "version": 1,
        "owner": "litesuite",
        "pid": os.getpid(),
        "port": 7470,
        "ini": "C:/Users/x/.litesuite/llm/models.ini",
        "startedAt": "2026-08-26T05:00:00+00:00",
    }
    payload.update(over)
    p = tmp_path / "router.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def test_write_then_read_round_trips_every_field(tmp_path):
    p = tmp_path / "router.json"
    rr.write(pid=4242, port=7470, ini="models.ini", build_tag="cuda:b9360", path=p)
    got = rr.read(p)
    assert got is not None
    assert (got.version, got.owner, got.pid, got.port) == (1, "litetui", 4242, 7470)
    assert got.ini == "models.ini"
    assert got.build_tag == "cuda:b9360"
    assert got.is_mine is True
    # startedAt is written for humans and for staleness triage, so it must be
    # a real timestamp rather than a placeholder.
    assert got.started_at.startswith("20")


def test_the_file_on_disk_matches_the_contract_spelling(tmp_path):
    """LiteSuite parses this with a schema that keys on camelCase. A Python
    writer that emitted `started_at` would round-trip fine HERE and be
    unreadable THERE, which is the failure this whole record exists to avoid.
    """
    p = tmp_path / "router.json"
    rr.write(pid=1, port=7470, ini="i.ini", build_tag="t", path=p)
    raw = json.loads(p.read_text(encoding="utf-8"))
    assert set(raw) == {"version", "owner", "pid", "port", "ini", "startedAt", "buildTag"}


def test_buildTag_is_omitted_rather_than_null_when_unknown(tmp_path):
    # The contract marks it optional. Absent and null are different things to
    # a schema, and only one of them is in the contract.
    p = tmp_path / "router.json"
    rr.write(pid=1, port=7470, ini="i.ini", path=p)
    assert "buildTag" not in json.loads(p.read_text(encoding="utf-8"))


def test_the_write_is_atomic_and_leaves_no_tmp(tmp_path):
    p = tmp_path / "router.json"
    rr.write(pid=1, port=7470, ini="i.ini", path=p)
    rr.write(pid=2, port=7470, ini="i.ini", path=p)
    assert [f.name for f in tmp_path.iterdir()] == ["router.json"]
    assert rr.read(p).pid == 2


@pytest.mark.parametrize(
    "bad",
    [
        pytest.param({"version": 2}, id="a future version we cannot claim to understand"),
        pytest.param({"owner": "external"}, id="an owner outside the contract"),
        pytest.param({"owner": "LiteSuite"}, id="owner is case-sensitive"),
        pytest.param({"pid": "4242"}, id="a pid as a string"),
        pytest.param({"pid": True}, id="a bool pid — an int in Python, and not a process"),
        pytest.param({"pid": 0}, id="pid zero"),
        pytest.param({"pid": -1}, id="a negative pid"),
        pytest.param({"port": None}, id="a null port"),
        pytest.param({"ini": 7470}, id="an ini that is not a path"),
    ],
)
def test_a_record_that_does_not_conform_reads_as_no_record(tmp_path, bad):
    assert rr.read(_rec(tmp_path, **bad)) is None


def test_missing_key_truncated_json_and_a_bare_list_all_read_as_none(tmp_path):
    p = tmp_path / "router.json"
    assert rr.read(p) is None                                   # never written
    p.write_text('{"version": 1, "owner": "litetui"', encoding="utf-8")
    assert rr.read(p) is None                                   # truncated mid-write
    p.write_text("[1, 2, 3]", encoding="utf-8")
    assert rr.read(p) is None                                   # valid JSON, wrong shape
    p.write_text("", encoding="utf-8")
    assert rr.read(p) is None


def test_an_unknown_extra_field_is_TOLERATED(tmp_path):
    """Forward compatibility runs both ways. If LiteSuite adds a field next
    week, LiteTUI must keep reading the record rather than deciding the
    router is unowned and restarting it.
    """
    got = rr.read(_rec(tmp_path, somethingAddedLater={"deep": True}))
    assert got is not None and got.port == 7470


def test_our_own_pid_is_live_and_a_freed_one_is_not(tmp_path):
    assert rr.is_live(rr.read(_rec(tmp_path, pid=os.getpid()))) is True
    assert rr.is_live(None) is False
    # A pid that cannot be running: the very top of the range, never assigned.
    assert rr.is_live(rr.read(_rec(tmp_path, pid=0x7FFFFFFF))) is False


@pytest.mark.skipif(sys.platform != "win32", reason="Windows access-denied semantics")
def test_a_process_we_may_not_open_counts_as_ALIVE(tmp_path):
    """🔴 THE BIAS IS THE FEATURE, AND IT IS THE OPPOSITE OF `jobkill.alive`.

    That helper answers "did my kill land?", so an unopenable handle is False
    — gone, or not ours to see, same answer. Here the question is "may I take
    this router over?", and *not ours to see* is the strongest possible NO.
    Reading access-denied as dead would have LiteTUI restart an ELEVATED
    LiteSuite's router, which is the exact accident the record prevents.

    pid 4 is the Windows System process: always running, never openable by a
    normal user.
    """
    assert rr.is_live(rr.read(_rec(tmp_path, pid=4))) is True


def test_remove_if_mine_retracts_our_claim_and_nobody_else_s(tmp_path):
    p = tmp_path / "router.json"
    rr.write(pid=4242, port=7470, ini="i.ini", path=p)
    # Not our pid: between our spawn and our shutdown someone else may have
    # taken the port. Deleting their record would announce a live router as
    # unowned and the next app to start would restart it.
    assert rr.remove_if_mine(9999, p) is False
    assert p.exists()
    assert rr.remove_if_mine(4242, p) is True
    assert not p.exists()
    assert rr.remove_if_mine(4242, p) is False          # already gone, no raise


def test_remove_if_mine_never_deletes_another_app_s_record(tmp_path):
    p = _rec(tmp_path, owner="litesuite", pid=4242)
    # Same pid, different owner — still not ours to retract.
    assert rr.remove_if_mine(4242, p) is False
    assert p.exists()


def test_the_default_path_is_the_one_both_apps_agreed_on():
    # Not configurable on purpose: two apps agreeing on a port but not on
    # where the record lives would each read an empty answer and conclude the
    # other was not there.
    assert rr.record_path().parts[-3:] == (".litesuite", "llm", "router.json")


# -- T217: one file, two ports ------------------------------------------------
#
# 🔴 THE RECORD PATH IS DELIBERATELY NOT CONFIGURABLE, so there is exactly ONE
# record for however many routers are running. `write` used to overwrite
# unconditionally, so an app spawning on a second port ERASED the live owner's
# claim on the first — and an erased claim does not read as "someone else owns
# it", it reads as UNOWNED. The next app to start then sees a healthy router
# with no record, decides it is a plausible orphan of its own, and restarts a
# live server. The bookkeeping that exists to prevent that accident was causing
# it.
#
# ⬜ THE PORT TERM IS WHAT KEEPS THE REFUSAL HONEST. A live foreign claim on OUR
# port is unreachable from `write` — `_ensure_running_sync` attaches to their
# router and never spawns — so the refusal is scoped to a DIFFERENT port, and
# the arms below pin each term of the condition separately.


def test_a_live_foreign_claim_on_another_port_SURVIVES_our_write(tmp_path):
    """The one that was broken: our write must not erase their claim."""
    p = _rec(tmp_path, owner="litesuite", pid=os.getpid(), port=7470)

    result = rr.write(pid=4242, port=7471, ini="ours.ini", path=p)

    assert result is None, "write reported success while refusing"
    kept = rr.read(p)
    assert kept is not None
    # Every field theirs, not merely the owner: a partial overwrite would leave
    # a record naming their app and our pid, which is worse than either.
    assert (kept.owner, kept.pid, kept.port) == ("litesuite", os.getpid(), 7470)
    assert kept.ini == "C:/Users/x/.litesuite/llm/models.ini"


def test_a_DEAD_foreign_claim_on_another_port_is_overwritten(tmp_path):
    """⬜ CONTROL: staleness must not lock the record forever.

    Without this, "never touch a foreign record" would pass the arm above and
    leave a crashed LiteSuite's record owning the file until someone deleted it
    by hand — so LiteTUI could never announce a router again.
    """
    # 0x7FFFFFFF is the never-assigned pid this file already uses for "dead".
    p = _rec(tmp_path, owner="litesuite", pid=0x7FFFFFFF, port=7470)

    result = rr.write(pid=4242, port=7471, ini="ours.ini", path=p)

    assert result == p
    got = rr.read(p)
    assert got is not None
    assert (got.owner, got.pid, got.port) == ("litetui", 4242, 7471)


def test_our_OWN_live_claim_on_another_port_is_overwritten(tmp_path):
    """⬜ CONTROL: the owner term, not just liveness.

    A LiteTUI record for a port we have just moved off is ours to replace —
    refusing here would strand our own claim on a port nothing is serving.
    Uses our own live pid, so only the OWNER differs from the arm above.
    """
    p = _rec(tmp_path, owner="litetui", pid=os.getpid(), port=7470)

    result = rr.write(pid=4242, port=7471, ini="ours.ini", path=p)

    assert result == p
    got = rr.read(p)
    assert got is not None
    assert (got.owner, got.pid, got.port) == ("litetui", 4242, 7471)


def test_a_live_foreign_claim_on_the_SAME_port_is_still_overwritten(tmp_path):
    """⬜ CONTROL: the port term.

    Reaching `write` at all means we spawned, which means we did not attach,
    which means their claim on this port is stale bookkeeping about a port we
    are now serving. Pinned because widening the refusal to "any live foreign
    record" would silently stop LiteTUI ever announcing a router on the shared
    port — the failure would look like coexistence working.
    """
    p = _rec(tmp_path, owner="litesuite", pid=os.getpid(), port=7470)

    result = rr.write(pid=4242, port=7470, ini="ours.ini", path=p)

    assert result == p
    got = rr.read(p)
    assert got is not None
    assert (got.owner, got.pid, got.port) == ("litetui", 4242, 7470)


def test_a_write_into_an_EMPTY_directory_still_returns_the_path(tmp_path):
    """⬜ CONTROL: no record is not a refusal.

    The happy path reads the file first now, and `read` returns None for both
    "absent" and "corrupt" — neither may be mistaken for a live foreign claim.
    """
    p = tmp_path / "nested" / "router.json"
    assert rr.write(pid=4242, port=7470, ini="ours.ini", path=p) == p
    first = rr.read(p)
    assert first is not None and first.pid == 4242

    p.write_text("{ truncated", encoding="utf-8")
    assert rr.write(pid=99, port=7470, ini="ours.ini", path=p) == p
