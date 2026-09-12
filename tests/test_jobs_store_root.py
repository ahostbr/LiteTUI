"""`jobs.json` was written to two paths and read from one (T689, defect 3).

🔴 `paths.ROOT` IS THE REPO ROOT. `paths.data_root()` IS WHERE THE APP KEEPS
ITS DATA, and the two differ the moment `LITETUI_DATA_ROOT` is set (paths.py:34).
Ten call sites split five and five, and `scheduler.load` only ever used
`data_root()` — so under a custom root every write from `app._fire_job` and
from `goal_loop` landed somewhere nothing reads.

WHAT THAT COST, beyond the obvious: `last_fired_slot` is the duplicate-fire
guard, and `app.py`'s comment above that save is at pains about exactly the
failure "you cannot see in a log that only records successes". The guard was
being computed correctly and thrown away, so it did not survive a restart. A
loop's re-armed `next_run_at` went the same way.

SECOND-ORDER, and the reason a guard belongs here too: those writes dumped the
whole job list into the SHARED CHECKOUT root, which is the class of stray
`test_live_state_guard.py` exists for — and it was watching the other file.
"""
import ast
from pathlib import Path
from types import SimpleNamespace

from litetui import app as app_mod
from litetui import paths
from litetui import scheduler as sched_mod


def _sched_store_calls() -> list[tuple[str, int, str]]:
    """Every `scheduler.save`/`load` call under src/, with the root it passes.

    Derived from the AST, not from a grep over source text: a text gate counts
    the PROSE about a call as a call, and this file's own docstring names the
    call sites it is asserting about.
    """
    src = Path(sched_mod.__file__).resolve().parent
    out: list[tuple[str, int, str]] = []
    for py in sorted(src.rglob("*.py")):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr not in ("save", "load"):
                continue
            owner = node.func.value
            if not isinstance(owner, ast.Name) or "sched" not in owner.id.lower():
                continue
            root = node.args[-1] if node.args else None
            out.append((py.name, node.lineno, ast.dump(root) if root is not None else ""))
    return out


def test_every_jobs_store_call_uses_THE_SAME_ROOT() -> None:
    """The writes follow the reads, and `load` was already at `data_root()`."""
    calls = _sched_store_calls()
    assert len(calls) >= 8, f"the AST scan found only {len(calls)} call sites; it broke"

    wrong = [(f, ln) for f, ln, root in calls if "data_root" not in root]
    assert wrong == [], (
        f"{wrong} pass a root that is not paths.data_root() — a jobs.json write "
        f"that lands where nothing loads it is a silent loss, not an error"
    )


def test_the_two_roots_really_are_different_under_an_override(monkeypatch, tmp_path) -> None:
    """The validity gate. If these resolved to one path the arm above would
    pass for the wrong reason and the whole finding would be vacuous."""
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    assert sched_mod.jobs_path(paths.data_root()) != sched_mod.jobs_path(paths.ROOT)


def test_a_fire_persists_where_load_reads_under_a_custom_data_root(
    monkeypatch, tmp_path
) -> None:
    """THE BEHAVIOURAL ARM. Not "the call site says data_root" — that is the
    arm above — but that the bookkeeping a fire produces is still there when
    something loads it, with the two roots actually apart.
    """
    repo, data = tmp_path / "repo", tmp_path / "data"
    repo.mkdir()
    data.mkdir()
    monkeypatch.setattr(app_mod.paths, "ROOT", repo)
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(data))

    job = sched_mod.Job.loop(
        prompt="inspect after the current turn",
        interval_minutes=5,
        owner_convo_id="owner-a",
        now="2026-08-23T12:00:00",
    )
    app = SimpleNamespace(
        convo_id="owner-a",
        jobs=[job],
        _pending_input=[],
        _chat_running=lambda: True,
        _user_bubble=lambda *args, **kwargs: None,
        settings=SimpleNamespace(tool_policy_profile="interactive"),
    )

    app_mod.LiteTUI._fire_job(app, job)

    [restored] = sched_mod.load(paths.data_root())
    assert restored.run_count == 1, "the fire's bookkeeping is not where load reads"
    assert restored.last_fired_slot, "the duplicate-fire guard did not survive the write"
    assert not sched_mod.jobs_path(repo).exists(), (
        "a job write landed in the repo root — the shared checkout, where "
        "test_live_state_guard.py now watches for exactly this"
    )
