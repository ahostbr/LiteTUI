"""The differential run of T0408-L: the same tests, counting OFF and counting ON.

Not a test module. It is two things in one file:

1. A pytest plugin (`-p judge_differential`). With JUDGE_DIFF_OUT set, every call that
   reaches the REAL approval door, `LiteTUI._authorize_action`, in any test of the run,
   is written as one JSON line: who called, with what, what the door answered, and what
   it did to the world on the way (asked the spawner, logged a relay outcome, opened a
   human door, set the stop flags). No test body is edited. With JUDGE_DIFF_MODE=on the
   plugin switches the count-only setting ON for the app of each call, just before the
   door runs; with "off" it leaves every app as the test built it.

2. A comparer (`python tests/judge_differential.py OFF.jsonl ON.jsonl`). The two files
   must agree call for call in everything except the rows of the one observation event,
   which only the ON run may hold. It prints the two totals and the rows, by kind.

The claim it checks is stage 1's whole promise: switching the counting on changes no
authorization, no modal, no relay message and no stop. It cannot show more than the
tests it is pointed at exercise; the files are named on the command line, never the
whole suite.

DO NOT point it at tests/test_permission_judge.py or the ORDER rows of
tests/test_plain_read.py. Those switch the setting off and on THEMSELVES, call by
call, and assert what an off call does; forcing every call on makes them fail by
construction (measured: the first run of this tool, judge/stage1-differential-01.log).
They are the differential for the calls they make. This tool is for every OTHER test
that reaches the door.
"""
from __future__ import annotations

import functools
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

import pytest

OUT = os.environ.get("JUDGE_DIFF_OUT", "")
MODE = os.environ.get("JUDGE_DIFF_MODE", "off")
_IDENT = re.compile(r"appr-[0-9a-f]{12}")
#: Fields the two runs must agree on, call for call.
SAME = ("name", "args", "door", "profile", "source", "route", "result", "stop", "relay",
        "relay_log", "humans")


def _plain(value):
    """Text with the relay's random request id removed. Temporary paths are NOT removed:
    run both halves with the same `--basetemp`, so they are the same text in both."""
    if not isinstance(value, str):
        return value
    return _IDENT.sub("appr-#", value)


def _digest(value) -> str:
    try:
        text = json.dumps(value, sort_keys=True, default=repr)
    except (TypeError, ValueError):
        text = repr(value)
    return hashlib.sha256(_IDENT.sub("appr-#", text).encode("utf-8", "replace")).hexdigest()[:16]


def _write(record: dict) -> None:
    with open(OUT, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True, default=repr) + "\n")


@pytest.fixture(autouse=True)
def _judge_differential(request, monkeypatch):
    if not OUT:
        yield
        return
    from litetui import app as app_mod
    from litetui import approval_relay, plain_read, seat_authority, tool_approval
    from litetui import permission_judge as pj

    real_door = app_mod.LiteTUI._authorize_action
    calls = {"n": 0}
    open_calls: list[dict] = []                       # the door calls in flight, innermost last

    def note(key: str, value) -> None:
        if open_calls:
            open_calls[-1][key].append(value)

    real_ask = approval_relay.ask_spawner

    async def ask(app, name, args, decision, source):
        status = await real_ask(app, name, args, decision, source)
        note("relay", [str(name), _digest(args), decision.action, decision.profile,
                       _plain(decision.reason), str(source), status])
        return status

    real_record = approval_relay.record

    def record(app, status, name, source, ident="none"):
        note("relay_log", [str(status), str(name), str(source)])
        return real_record(app, status, name, source, ident)

    def human(kind, real):
        async def opened(*args, **kwargs):
            note("humans", kind)
            return await real(*args, **kwargs)

        return opened

    real_row = pj._row

    def row(*args, **kwargs):
        made = real_row(*args, **kwargs)
        if made is not None:
            note("rows", made)
        return made

    real_assess = plain_read.assess

    @functools.wraps(real_assess)                    # its signature is itself under test
    def assess(*args, **kwargs):
        note("assessed", 1)
        return real_assess(*args, **kwargs)

    async def door(self, name, args, policy, **kwargs):
        index = calls["n"]
        calls["n"] += 1
        if MODE == "on":
            try:
                self.settings.permission_judge = pj.COUNT
            except Exception:  # noqa: BLE001 - a host with no settings object cannot be switched
                pass
        try:
            route = seat_authority.confirm_route(self)
        except Exception as error:  # noqa: BLE001
            route = f"raised:{type(error).__name__}"
        current = {
            "test": request.node.nodeid, "index": index, "name": str(name), "args": _digest(args),
            "door": sorted((key, repr(value) if key != "workspace" else value is not None)
                           for key, value in kwargs.items() if key != "profile"),
            "profile": [str(kwargs.get("profile")), str(getattr(self, "_active_tool_profile", None))],
            "source": str(getattr(self, "_hook_source", None)), "route": route,
            "relay": [], "relay_log": [], "humans": [], "rows": [], "assessed": [],
        }
        open_calls.append(current)
        try:
            result = await real_door(self, name, args, policy, **kwargs)
            current["result"] = "authorized" if result is None else ["refused", _plain(str(result[0]))]
            return result
        except BaseException as error:
            current["result"] = ["raised", type(error).__name__]
            raise
        finally:
            open_calls.pop()
            current["stop"] = [bool(getattr(self, "_stop_requested", False)),
                               _plain(getattr(self, "_stop_reason", None)),
                               getattr(self, "_stop_cause", None)]
            current["assessed"] = len(current["assessed"])
            _write(current)

    monkeypatch.setattr(approval_relay, "ask_spawner", ask)
    monkeypatch.setattr(approval_relay, "record", record)
    monkeypatch.setattr(app_mod, "show_dialog", human("modal", app_mod.show_dialog))
    monkeypatch.setattr(tool_approval, "approve_over_rpc", human("rpc", tool_approval.approve_over_rpc))
    monkeypatch.setattr(pj, "_row", row)
    monkeypatch.setattr(plain_read, "assess", assess)
    monkeypatch.setattr(app_mod.LiteTUI, "_authorize_action", door)
    yield


def pytest_runtest_logreport(report):
    if OUT and (report.when == "call" or report.outcome != "passed"):
        _write({"verdict": report.nodeid, "when": report.when, "outcome": report.outcome})


# ── the comparer ─────────────────────────────────────────────────────────────


def _load(path: Path):
    calls, verdicts = {}, {}
    for line in path.read_text(encoding="utf-8").splitlines():
        record = json.loads(line)
        if "verdict" in record:
            verdicts[(record["verdict"], record["when"])] = record["outcome"]
        else:
            calls[(record["test"], record["index"])] = record
    return calls, verdicts


def compare(off_path: Path, on_path: Path) -> int:
    off, off_verdicts = _load(off_path)
    on, on_verdicts = _load(on_path)
    only_off, only_on = sorted(set(off) - set(on)), sorted(set(on) - set(off))
    differing = [key for key in sorted(set(off) & set(on))
                 if any(off[key].get(field) != on[key].get(field) for field in SAME)]
    off_rows = [row for record in off.values() for row in record["rows"]]
    on_rows = [row for record in on.values() for row in record["rows"]]
    kinds = Counter((row["status"], row.get("error_type") or row.get("exit_code") or "",
                     row["name"]) for row in on_rows)
    changed_verdicts = sorted(key for key in set(off_verdicts) | set(on_verdicts)
                              if off_verdicts.get(key) != on_verdicts.get(key))
    failed = Counter(outcome for (_, when), outcome in off_verdicts.items() if when == "call")
    print(f"door calls recorded, counting OFF: {len(off)}")
    print(f"door calls recorded, counting ON:  {len(on)}")
    print(f"calls in only one run: off-only={len(only_off)} on-only={len(only_on)}")
    print(f"calls that differ in {', '.join(SAME)}: {len(differing)}")
    print(f"observation rows, OFF run: {len(off_rows)}")
    print(f"observation rows, ON run:  {len(on_rows)}")
    for (status, detail, tool_class), count in sorted(kinds.items(), key=str):
        print(f"  {status}{':' + str(detail) if detail != '' else ''} [{tool_class}] = {count}")
    print(f"calls to the check, OFF run: {sum(r['assessed'] for r in off.values())}; "
          f"ON run: {sum(r['assessed'] for r in on.values())}")
    print(f"test verdicts (call phase), OFF run: {dict(sorted(failed.items()))}")
    print(f"tests whose verdict differs between the runs: {len(changed_verdicts)}")
    for key in changed_verdicts[:40]:
        print(f"  {key[0]} [{key[1]}] off={off_verdicts.get(key)} on={on_verdicts.get(key)}")
    for key in (only_off + only_on + differing)[:20]:
        print(f"  DIFF {key}: off={off.get(key)} on={on.get(key)}")
    equal = not (only_off or only_on or differing or off_rows)
    print("EQUAL but for the observation rows" if equal else "NOT EQUAL")
    return 0 if equal else 1


if __name__ == "__main__":
    sys.exit(compare(Path(sys.argv[1]), Path(sys.argv[2])))
