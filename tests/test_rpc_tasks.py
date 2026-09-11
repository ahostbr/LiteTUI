"""T571 — `tasks.*` over `--rpc` against REAL Task rows.

🔴 THE HANDLER WAS WRITTEN AGAINST A SHAPE THAT NEVER EXISTED. `app.bg_tasks`
holds `tasks.Task` DATACLASSES (`_start_background` puts them there), and
`_handle_tasks` did `{"id": k, **v}` and `v["status"] = "killed"` — both of which
need a mapping. Every call raised, and the surrounding `except Exception` turned
the raise into a polite rpc error, so `tasks.list` has answered
`{"ok": false, "error": "argument of type 'Task' is not a mapping"}` for the life
of the verb. Nothing crashed and nothing was logged: a caller sees a failure that
looks like "no tasks" or "not supported yet".

⚠️ WHICH IS WHY THESE ARMS DRIVE THE HANDLER WITH A LIVE `Task` AND ASSERT
`ok is True`. An arm built on a dict — the shape the handler expected — passes
against the broken code and proves nothing. The fixture here is
`tasks.new_task`, the same constructor the runner uses, so the arms cannot drift
from what the app actually stores.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from litetui import app as m
from litetui import rpc as rpc_mod
from litetui import tasks as tasks_mod


def make_app():
    a = m.LiteTUI()
    a.available_models = ["a-model"]
    a.model_id = "a-model"
    a._connect = lambda: None
    a._fetch_ctx_window = lambda: None
    a.bg_tasks = {}
    # `_kill_background` posts its refusal into the chat, which needs a mounted
    # app ("No screens on stack"). Captured rather than silenced: over `--rpc`
    # that line is the second half of the answer, the same way `/think`'s
    # headless branch uses `system_message` as its output channel.
    a.said = []
    a._system = a.said.append
    return a


@pytest.fixture
def wire(monkeypatch):
    """Every `_respond` on the wire, in order."""
    out: list[dict] = []
    monkeypatch.setattr(rpc_mod, "rpc_emit", out.append)
    return out


def drive(app, wire, verb: str, **cmd) -> dict:
    rpc_mod._handle_tasks(app, f"tasks.{verb}", {"type": f"tasks.{verb}", **cmd}, 7)
    assert wire, f"tasks.{verb} emitted nothing at all"
    return wire[-1]


def add(app, tool="bash", **args):
    t = tasks_mod.new_task(tool, args or {"command": "sleep 30"}, "convo-1")
    app.bg_tasks[t.id] = t
    return t


# ── list ───────────────────────────────────────────────────────────────────

def test_list_answers_with_the_rows_not_an_error(wire):
    a = make_app()
    t = add(a)
    resp = drive(a, wire, "list")
    assert resp["ok"] is True, f"tasks.list failed: {resp.get('error')!r}"
    assert [r["id"] for r in resp["result"]] == [t.id]


def test_a_listed_row_carries_what_a_caller_needs_to_act(wire):
    """`to_row` is the serialiser the store already uses — same field set on the
    wire and on disk, so a caller and a restart cannot see different tasks."""
    a = make_app()
    t = add(a, "subagent", prompt="count the leaves")
    row = drive(a, wire, "list")["result"][0]
    assert row["id"] == t.id
    for field in ("id", "tool", "label", "state", "started", "prompt"):
        assert field in row, f"the row has no {field!r}: {sorted(row)}"
    assert row["state"] == tasks_mod.RUNNING
    assert row["prompt"] == "count the leaves"


def test_the_live_child_never_reaches_the_wire(wire):
    """`proc` is a Popen holding a `_thread.lock`. `to_row` excludes it for the
    store (deep-copying one was a crash); the wire needs the same exclusion or
    `json.dumps` falls back to `default=str` and ships a repr of a subprocess."""
    a = make_app()
    t = add(a)
    t.proc = object()
    row = drive(a, wire, "list")["result"][0]
    assert "proc" not in row


def test_the_row_survives_json_without_default_str(wire):
    """`rpc_emit` dumps with `default=str`, which is a SILENT fallback: an
    unserialisable field does not raise, it ships as a repr and the caller parses
    a string where it expected a number. Dumped strictly here so a field that
    cannot cross the wire fails loudly in this arm instead of quietly on it."""
    import json

    a = make_app()
    t = add(a, "subagent", prompt="count the leaves")
    t.proc = object()
    row = drive(a, wire, "list")["result"][0]
    json.dumps(row)  # no default= : raises if anything is not JSON
    assert isinstance(row["started"], float)


def test_an_empty_store_is_an_empty_list_not_a_failure(wire):
    a = make_app()
    resp = drive(a, wire, "list")
    assert resp["ok"] is True
    assert resp["result"] == []


# ── kill ───────────────────────────────────────────────────────────────────

def test_kill_moves_the_REAL_field_and_says_so(wire):
    """The old line wrote `["status"]`, which is not even the field's name —
    `state` is. A caller that read `state` back would have seen `running` after
    a kill it was told succeeded."""
    a = make_app()
    t = add(a)
    t.proc = object()
    a._kill_background_tree = lambda task: None

    resp = drive(a, wire, "kill", task_id=t.id)
    assert resp["ok"] is True, f"kill failed: {resp.get('error')!r}"
    assert t.state == tasks_mod.KILLED
    assert not hasattr(t, "status")


def test_killing_something_already_finished_is_refused_not_confirmed(wire):
    """`ok: True` on a task that was never running tells the caller it stopped
    work that had already stopped — and hides that its `task_id` was stale."""
    a = make_app()
    t = add(a)
    t.state = tasks_mod.DONE
    resp = drive(a, wire, "kill", task_id=t.id)
    assert resp["ok"] is False
    assert t.id in resp["error"]
    assert tasks_mod.DONE in resp["error"], "the refusal does not say WHY"
    assert a.said and t.id in a.said[-1], "the chat channel heard nothing"

    assert t.state == tasks_mod.DONE, "a refused kill still moved the state"


def test_killing_an_unknown_id_is_refused(wire):
    a = make_app()
    resp = drive(a, wire, "kill", task_id="t-nope")
    assert resp["ok"] is False
    assert "t-nope" in resp["error"]


def test_kill_goes_through_the_app_body_not_its_own_copy(monkeypatch, wire):
    """`_kill_background` is what `/tasks kill` calls: it checks the state AND
    takes the process tree down. A second implementation on this side would set
    a field and leave the child running — a kill that reports success and kills
    nothing."""
    a = make_app()
    t = add(a)
    called: list[str] = []
    monkeypatch.setattr(type(a), "_kill_background", lambda self, tid: called.append(tid))
    drive(a, wire, "kill", task_id=t.id)
    assert called == [t.id]


# ── tail ───────────────────────────────────────────────────────────────────

def test_tail_exists_because_the_docstring_already_promised_it(wire, tmp_path, monkeypatch):
    """`_handle_tasks` has said "Route tasks.list/kill/tail" since it was
    written, and there was no tail branch: the verb answered "unknown tasks
    verb". A docstring naming a capability that is not there is a contract
    someone can code against and lose to."""
    from litetui import paths

    monkeypatch.setattr(paths, "ROOT", tmp_path)
    a = make_app()
    t = add(a)
    tasks_mod.finish(t, "line one\nline two\n", True, tmp_path)

    resp = drive(a, wire, "tail", task_id=t.id)
    assert resp["ok"] is True, f"tasks.tail failed: {resp.get('error')!r}"
    assert "line two" in resp["result"]["tail"]


def test_tail_of_an_unknown_id_says_so(wire):
    a = make_app()
    resp = drive(a, wire, "tail", task_id="t-nope")
    assert resp["ok"] is False
    assert "t-nope" in resp["error"]


# ── the shape of a refusal ─────────────────────────────────────────────────

def test_an_unknown_verb_is_still_refused_by_name(wire):
    a = make_app()
    resp = drive(a, wire, "explode")
    assert resp["ok"] is False
    assert "explode" in resp["error"]


def test_rpc_tail_uses_durable_root_not_workspace(tmp_path, monkeypatch, wire):
    from litetui import paths
    data = tmp_path / "durable"
    data.mkdir()
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(data))
    monkeypatch.setattr(paths, "ROOT", tmp_path / "resources")
    a = make_app()
    task = add(a)
    tasks_mod.finish(task, "distinct durable output", True, data)
    response = drive(a, wire, "tail", task_id=task.id)
    assert response["ok"] is True
    assert "distinct durable output" in response["result"]["tail"]
