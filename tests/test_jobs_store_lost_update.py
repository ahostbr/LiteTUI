"""Two windows editing `jobs.json` (T689, defect 1 for the other store).

Same whole-file write, same lost update — and one thing `background-tasks.json`
cannot test, because it has no deletion path at all.

🔴 THIS IS THE FILE WHERE A MERGE WOULD RESURRECT. `/cron rm` (cron.py:70),
a loop stopping (goal_loop.py:406) and the rpc `jobs.delete` all remove rows.
Under a merge keyed on id, the instance that did NOT delete the job hands it
back from its own memory on its next save — worse than the bug being fixed,
and invisible to a green suite because both instances behave exactly as
written. Under a delta a removal is an entry: it applies once and stays
applied.
"""
from contextlib import contextmanager
from types import SimpleNamespace

from litetui import row_store
from litetui import scheduler as sched_mod


class _Instance:
    """One LiteTUI process's view of the job list, including its baseline.

    The baseline is per process; a test that just calls `save` twice is one
    instance that dropped a row, not two instances. See the longer note in
    `test_store_lost_update.py` — same reasoning, same reach into `_BASELINE`.
    """

    def __init__(self, root):
        self.root = root
        self.baseline: dict = {}
        self.jobs: list = []

    @contextmanager
    def _switched_in(self):
        row_store._BASELINE.clear()
        row_store._BASELINE.update(self.baseline)
        try:
            yield
        finally:
            self.baseline = dict(row_store._BASELINE)

    def boot(self):
        self.baseline = {}
        with self._switched_in():
            self.jobs = sched_mod.load(self.root)
        return self

    def save(self):
        with self._switched_in():
            sched_mod.save(self.jobs, self.root)

    def add(self, prompt, schedule="0 9 * * *"):
        job = sched_mod.Job(prompt=prompt, schedule=schedule)
        self.jobs.append(job)
        self.save()
        return job

    def find(self, job_id):
        return next(j for j in self.jobs if j.id == job_id)


def _on_disk(root):
    """What a third reader sees — never either instance's memory."""
    row_store.forget(sched_mod.jobs_path(root))
    return sched_mod.load(root)


def test_a_second_instance_does_not_erase_the_first_instances_job(tmp_path):
    a, b = _Instance(tmp_path).boot(), _Instance(tmp_path).boot()

    ja = a.add("morning standup")
    jb = b.add("nightly backup")

    assert {j.id for j in _on_disk(tmp_path)} == {ja.id, jb.id}


def test_a_removal_in_A_is_not_resurrected_by_an_edit_in_B(tmp_path):
    """🔴 SENTINEL'S ARM, AND THE ONE A MERGE FAILS. A removes a job; B, which
    booted while both existed, edits the OTHER one and saves. The removal must
    stick and B's edit must land."""
    seed = _Instance(tmp_path).boot()
    gone = seed.add("delete me")
    kept = seed.add("keep me")

    a, b = _Instance(tmp_path).boot(), _Instance(tmp_path).boot()

    a.jobs.remove(a.find(gone.id))
    a.save()

    b.find(kept.id).enabled = False
    b.save()

    after = {j.id: j for j in _on_disk(tmp_path)}
    assert gone.id not in after, "a removed job came back from the other instance's memory"
    assert after[kept.id].enabled is False, "B's edit was lost"


def test_an_edit_in_A_survives_an_unrelated_save_in_B(tmp_path):
    """The other direction, and the one a merge also fails: B holds a stale
    copy of the job A just edited, and must have no opinion about it."""
    seed = _Instance(tmp_path).boot()
    one = seed.add("job one")
    two = seed.add("job two")

    a, b = _Instance(tmp_path).boot(), _Instance(tmp_path).boot()

    a.find(one.id).run_count = 7
    a.save()

    b.find(two.id).enabled = False
    b.save()

    after = {j.id: j for j in _on_disk(tmp_path)}
    assert after[one.id].run_count == 7, "B's stale copy overwrote a job it never touched"
    assert after[two.id].enabled is False


def test_a_job_the_loader_could_not_parse_is_left_alone(tmp_path):
    """The baseline is the rows `load` KEPT. A row it skipped is not one we
    hold, and a baseline claiming it would make the next save read it as a
    deletion and erase a job we simply failed to understand."""
    import json

    p = sched_mod.jobs_path(tmp_path)
    p.write_text(
        json.dumps([{"id": "junk", "note": "no prompt, no schedule"}]), encoding="utf-8"
    )

    a = _Instance(tmp_path).boot()
    assert a.jobs == []
    a.add("a real one")

    rows = json.loads(p.read_text(encoding="utf-8"))
    assert "junk" in [r.get("id") for r in rows]


def test_the_rpc_handler_edits_the_SAME_list_the_ui_holds(tmp_path, monkeypatch):
    """🔴 THE SECOND HOLDER INSIDE ONE PROCESS. `_handle_jobs` used to read its
    own copy off disk, so an rpc-created job was invisible to `app.jobs` and
    the UI's next save wrote the list back out without it. The delta reconciles
    two PROCESSES; it cannot reconcile two holders in one, and should not have
    to."""
    from litetui import paths, rpc

    monkeypatch.setattr(paths, "ROOT", tmp_path)
    monkeypatch.delenv("LITETUI_DATA_ROOT", raising=False)
    replies: list = []
    monkeypatch.setattr(rpc, "_respond", lambda cid, **kw: replies.append(kw))

    app = SimpleNamespace(jobs=[])
    rpc._handle_jobs(
        app, "jobs.create", {"prompt": "from rpc", "schedule": "0 9 * * *"}, 1
    )

    assert [j.prompt for j in app.jobs] == ["from rpc"], (
        "an rpc-created job never reached the list the UI renders and saves"
    )
    assert replies and replies[-1]["ok"] is True
