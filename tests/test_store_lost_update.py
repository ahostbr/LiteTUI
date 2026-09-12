"""`background-tasks.json` lost a second instance's rows (T689, defects 1-2).

The store is a whole-file writer: each instance loads it once at boot and
rewrites it in full on every transition, which is correct for one process and
lossy for two. A adds a row and saves; B — whose memory predates that row and
therefore cannot contain it — saves, and the file holds only B's.

🔴 THE FIX IS A DELTA AGAINST THE SNAPSHOT THIS INSTANCE LOADED, NOT A MERGE.
A merge keyed on id looks equivalent here and is not: it would also let B's
STALE copy of a row it never touched overwrite the newer copy A just wrote.
Under a delta, a row this instance did not change is not in its delta at all,
so disk keeps whatever the owner last said about it. The same helper serves
`jobs.json`, where a removal is a delta entry and therefore cannot be
resurrected — which a merge would do, and which is why `row_store` takes a
baseline rather than being a smarter `save`.

⚠️ AND THE DELTA ALONE WOULD HAVE MADE A SECOND DEFECT DURABLE. `tasks.load`
marked every `running` row LOST at boot, on the stated assumption that a task
cannot survive the app — true of the app that STARTED it, false of a sibling's.
So B's boot rewrote A's live row as LOST, that counts as a change, and the
delta would then carry the mislabel to disk where it looks authoritative. Hence
the owner pid: LOST is stamped only when the owner is actually gone.
"""
import json
from contextlib import contextmanager

from litetui import router_record, row_store
from litetui import tasks as tasks_mod


class _Instance:
    """One LiteTUI process's view of the store, including its own baseline.

    🔴 THE BASELINE IS PER PROCESS, AND ONE PYTEST PROCESS IS NOT TWO. What the
    fix relies on is that A and B each have their own `row_store._BASELINE`; a
    test that merely calls `save` twice is not two instances, it is ONE
    instance that dropped a row — and the delta correctly reads that as a
    deletion. Modelling the boundary is the difference between an arm that
    measures the fix and one that measures the test harness.

    ⚠️ IT REACHES INTO `_BASELINE` DELIBERATELY. The module has no public
    swap-my-whole-identity call because nothing in the app needs one — a
    process has exactly one — and adding an API only a test uses would be a
    worse lie than this comment.
    """

    def __init__(self, root):
        self.root = root
        self.baseline: dict = {}
        self.tasks: dict = {}

    @contextmanager
    def _switched_in(self):
        row_store._BASELINE.clear()
        row_store._BASELINE.update(self.baseline)
        try:
            yield
        finally:
            self.baseline = dict(row_store._BASELINE)

    def boot(self):
        """No memory of the file, then read it — what a real process does."""
        self.baseline = {}
        with self._switched_in():
            self.tasks = tasks_mod.load(self.root)
        return self

    def save(self):
        with self._switched_in():
            tasks_mod.save(list(self.tasks.values()), self.root)

    def start(self, command):
        t = tasks_mod.new_task("bash", {"command": command}, "")
        self.tasks[t.id] = t
        self.save()
        return t


def _on_disk(root):
    """What a THIRD reader sees — never either instance's memory."""
    row_store.forget(root / tasks_mod.STORE)
    return tasks_mod.load(root)


# ── the cross-instance overwrite ─────────────────────────────────────────


def test_a_second_instance_does_not_erase_the_first_instances_rows(tmp_path):
    """THE CARD'S REPRODUCTION, from the T688 F+G commit body: A adds `task-A`
    and saves; B, which booted earlier, adds `task-B`; the file held only B's."""
    a, b = _Instance(tmp_path).boot(), _Instance(tmp_path).boot()

    task_a = a.start("a")
    task_b = b.start("b")

    assert set(_on_disk(tmp_path)) == {task_a.id, task_b.id}


def test_the_writers_own_transition_reaches_disk(tmp_path):
    """THE DISCRIMINATOR. Without it the arm above is satisfied by a save that
    keeps disk verbatim for every id — which would freeze every task at the
    state it was first written with, a worse store than the one we have."""
    a = _Instance(tmp_path).boot()
    t = a.start("x")                                   # running

    tasks_mod.finish(t, "ok", True, tmp_path)
    a.save()                                           # done

    assert _on_disk(tmp_path)[t.id].state == tasks_mod.DONE


def test_a_stale_copy_of_a_row_we_never_touched_does_not_overwrite_it(tmp_path):
    """🔴 THE ARM THAT SEPARATES A DELTA FROM A MERGE, and the one a merge-by-id
    fails. B boots while A's task is running, A finishes it, then B saves for a
    reason of its own. B still holds the `running` copy it read at boot — and
    it did not change that row, so it must have no opinion about it.
    """
    a = _Instance(tmp_path).boot()
    task_a = a.start("a")

    b = _Instance(tmp_path).boot()                     # B sees it running
    assert task_a.id in b.tasks

    tasks_mod.finish(task_a, "ok", True, tmp_path)
    a.save()                                           # A records `done`

    b.start("b")                                       # B saves its own work

    after = _on_disk(tmp_path)
    assert after[task_a.id].state == tasks_mod.DONE, (
        "B's stale copy overwrote a row it never touched"
    )
    assert len(after) == 2


def test_a_row_we_hold_is_not_written_twice(tmp_path):
    """A delta applied by position rather than by id duplicates instead of
    updating, and a duplicate is invisible to `load` — the dict swallows it —
    so this asserts on the FILE."""
    a = _Instance(tmp_path).boot()
    t = a.start("x")
    a.save()

    rows = json.loads((tmp_path / tasks_mod.STORE).read_text(encoding="utf-8"))
    assert [r["id"] for r in rows] == [t.id]


def test_an_unreadable_store_still_writes_our_own_rows(tmp_path):
    """The fix adds a READ to a path that previously only wrote. A store that
    cannot be parsed must not take the save down with it: losing the history is
    survivable, losing the row for the task running right now is not."""
    (tmp_path / tasks_mod.STORE).write_text("{ this is not json", encoding="utf-8")
    a = _Instance(tmp_path).boot()

    t = a.start("x")

    assert set(_on_disk(tmp_path)) == {t.id}


# ── LOST belongs to the owner's death, not to our boot ───────────────────


def test_a_running_row_whose_owner_is_ALIVE_is_not_marked_lost(tmp_path, monkeypatch):
    """Ryan's two windows: B booting must not report A's in-flight task as
    killed. Before the owner pid, every boot stamped every running row."""
    monkeypatch.setattr(router_record, "pid_is_live", lambda pid: True)
    a = _Instance(tmp_path).boot()
    t = a.start("sleep 900")

    assert _on_disk(tmp_path)[t.id].state == tasks_mod.RUNNING


def test_a_running_row_whose_owner_is_GONE_is_marked_lost(tmp_path, monkeypatch):
    """The original truth, and it must survive the fix: the child sits in the
    owner's Job Object, so when that process went, the work went."""
    monkeypatch.setattr(router_record, "pid_is_live", lambda pid: False)
    a = _Instance(tmp_path).boot()
    t = a.start("sleep 900")

    back = _on_disk(tmp_path)[t.id]
    assert back.state == tasks_mod.LOST and back.ended is not None


def test_the_lost_stamping_reaches_DISK_and_is_not_only_in_memory(tmp_path, monkeypatch):
    """The stamping is a change this instance made, so it belongs in its delta.

    ⚠️ THIS IS THE ARM FOR THE ORDER `tasks.load` TAKES ITS BASELINE IN. Taken
    after the stamping, the row would match the baseline, the delta would be
    empty, and LOST would be recomputed on every boot forever while the file
    still said `running` — green everywhere, wrong on disk.
    """
    monkeypatch.setattr(router_record, "pid_is_live", lambda pid: False)
    a = _Instance(tmp_path).boot()
    t = a.start("sleep 900")

    b = _Instance(tmp_path).boot()           # B boots, owner is gone
    b.save()

    rows = json.loads((tmp_path / tasks_mod.STORE).read_text(encoding="utf-8"))
    assert [r["state"] for r in rows if r["id"] == t.id] == [tasks_mod.LOST]


def test_a_row_written_before_this_change_keeps_TODAYS_answer(tmp_path, monkeypatch):
    """No pid on the row means no claim about an owner, and the honest fallback
    is the behaviour that shipped: assume it died with the app that wrote it.
    The `pid_is_live` stub returning True proves the row is not merely taking
    the live branch by accident."""
    monkeypatch.setattr(router_record, "pid_is_live", lambda pid: True)
    a = _Instance(tmp_path).boot()
    t = a.start("sleep 900")

    p = tmp_path / tasks_mod.STORE
    rows = json.loads(p.read_text(encoding="utf-8"))
    for r in rows:
        r.pop("owner_pid", None)
    p.write_text(json.dumps(rows), encoding="utf-8")

    assert _on_disk(tmp_path)[t.id].state == tasks_mod.LOST


def test_the_owner_pid_is_THIS_process() -> None:
    """The validity gate for the three arms above. If `new_task` stamped
    nothing, `owner_pid` would be None on every row and all three would be
    exercising the legacy branch while appearing to test the new one."""
    import os

    assert tasks_mod.new_task("bash", {"command": "x"}, "").owner_pid == os.getpid()
