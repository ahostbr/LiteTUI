"""The pure half of background tasks: ids, the wake text, the store, LOST on boot."""
import json

from litetui import tasks as tasks_mod


def test_start_text_names_the_id_the_tool_and_the_log(tmp_path):
    t = tasks_mod.new_task("bash", {"command": "sleep 900 && echo hi"}, "c1")
    txt = tasks_mod.start_text(t, tmp_path)
    assert t.id in txt and "bash" in txt and f"output/tasks/{t.id}.log" in txt
    assert t.label == "sleep 900 && echo hi"


def test_ids_are_unique():
    ids = {tasks_mod.new_task("bash", {}, "").id for _ in range(50)}
    assert len(ids) == 50


def test_finish_writes_the_log_and_excerpts_both_ends(tmp_path):
    t = tasks_mod.new_task("powershell", {"command": "x"}, "")
    raw = "HEAD-" + ("a" * 3000) + "-MIDDLE-" + ("b" * 3000) + "-TAIL"
    txt = tasks_mod.finish(t, raw, True, tmp_path)
    assert t.state == tasks_mod.DONE and t.ok is True
    assert (tmp_path / "output" / "tasks" / f"{t.id}.log").read_text(encoding="utf-8") == raw
    assert txt.startswith(f"[task {t.id} done")
    assert "HEAD-" in txt and "-TAIL" in txt and "chars cut" in txt
    assert "-MIDDLE-" not in txt  # the middle is in the log, not the wake


def test_a_killed_task_stays_killed_when_the_child_returns(tmp_path):
    t = tasks_mod.new_task("bash", {"command": "x"}, "")
    t.state = tasks_mod.KILLED
    txt = tasks_mod.finish(t, "[exit 1]", False, tmp_path)
    assert t.state == tasks_mod.KILLED and txt.startswith(f"[task {t.id} killed")


def test_store_round_trips_and_marks_running_rows_lost(tmp_path):
    a = tasks_mod.new_task("bash", {"command": "a"}, "")
    b = tasks_mod.new_task("bash", {"command": "b"}, "")
    tasks_mod.finish(b, "ok", True, tmp_path)
    tasks_mod.save([a, b], tmp_path)
    rows = json.loads((tmp_path / tasks_mod.STORE).read_text(encoding="utf-8"))
    assert {r["id"] for r in rows} == {a.id, b.id} and all("proc" not in r for r in rows)
    back = tasks_mod.load(tmp_path)
    assert back[a.id].state == tasks_mod.LOST
    assert back[b.id].state == tasks_mod.DONE


def test_load_tolerates_a_missing_or_broken_store(tmp_path):
    assert tasks_mod.load(tmp_path) == {}
    (tmp_path / tasks_mod.STORE).write_text("{not json", encoding="utf-8")
    assert tasks_mod.load(tmp_path) == {}


def test_tail_says_running_for_a_live_task_and_reads_the_log_after(tmp_path):
    t = tasks_mod.new_task("bash", {"command": "x"}, "")
    assert "still running" in tasks_mod.tail_text(t, tmp_path)
    tasks_mod.finish(t, "l1\nl2\nl3", True, tmp_path)
    assert tasks_mod.tail_text(t, tmp_path, lines=2) == "l2\nl3"
    assert tasks_mod.tail_text(None, tmp_path) == "no such task"


def test_to_row_never_copies_the_live_child():
    # logs/crash-9-8-2026.txt: `asdict` deep-copied the parked Popen (a
    # _thread.lock inside) and the whole chat worker died. A lock stands in.
    import threading
    t = tasks_mod.new_task("bash", {"command": "sleep 900"}, "c1")
    t.proc = threading.Lock()
    row = t.to_row()
    assert "proc" not in row and row["id"] == t.id and row["state"] == tasks_mod.RUNNING
    json.dumps(row)


def test_wait_or_promote_promotes_a_slow_call_and_keeps_a_fast_one():
    # T517: a foreground call past the threshold is handed over as the SAME future.
    import asyncio

    async def slow():
        await asyncio.sleep(0.3)
        return "late"

    async def fast():
        return "quick"

    async def main():
        done, fut = await tasks_mod.wait_or_promote(slow(), 0.05)
        assert not done and not fut.done()
        assert await fut == "late"          # still the same work, finishing later
        done, fut = await tasks_mod.wait_or_promote(fast(), 1.0)
        assert done and fut.result() == "quick"
        done, fut = await tasks_mod.wait_or_promote(fast(), 0)   # 0 = never promote
        assert done and fut.result() == "quick"

    asyncio.run(main())


def test_start_text_names_the_promotion(tmp_path):
    t = tasks_mod.new_task("bash", {"command": "sleep 900"}, "c1")
    assert "still running after 30s" in tasks_mod.start_text(t, tmp_path, promoted_after=30)
    assert "started" in tasks_mod.start_text(t, tmp_path)


def test_only_a_schema_that_declares_background_is_backgroundable():
    # Ryan: "not everything should be backgroundable ... only what makes sense".
    assert tasks_mod.backgroundable("bash") and tasks_mod.backgroundable("powershell")
    for tool in ("read", "edit", "grep", "write", "ask_user_question", "chrome",
                 "pccontrol", "studio", "web_fetch", "skill", "view_image", "listen", "harness"):
        assert not tasks_mod.backgroundable(tool), tool
    assert not tasks_mod.backgroundable("no-such-tool")
