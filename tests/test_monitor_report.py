"""Report rendering: the human summary and the machine JSON.

Canned CheckResults are fed in (no probe/sink needed) so the test is purely
about formatting: the per-target marks, the up/down tally, and that the JSON
carries the same facts a dashboard would want (summary + per-target rows).
"""

from __future__ import annotations

import json

from litetui.monitor.engine import CheckResult
from litetui.monitor.report import format_report, summary, to_json


def _r(name, ok=True, status="ok", load_ms=100.0, expect_met=True,
       regression="none", error=""):
    return CheckResult(target=name, url=f"https://{name}.test", ok=ok,
                       status=status, load_ms=load_ms, text_chars=10,
                       expect_met=expect_met, error=error,
                       regression=regression)


def test_report_shows_up_and_down_marks():
    text = format_report([_r("a"), _r("b", ok=False, status="error",
                                    error="timeout")])
    assert "+ a" in text
    assert "x b" in text
    assert "timeout" in text
    assert "1/2 up" in text


def test_report_empty_results():
    text = format_report([])
    assert "no targets configured" in text


def test_report_flags_visual_change_and_missing_expect():
    text = format_report([
        _r("a", regression="changed"),
        _r("b", expect_met=False),
    ])
    assert "[visual: changed]" in text
    assert "[expect text missing]" in text
    # A stable target does not get a visual annotation.
    stable = format_report([_r("c", regression="stable")])
    assert "[visual:" not in stable


def test_summary_counts():
    s = summary([_r("a"), _r("b", ok=False, status="error"),
                 _r("c", regression="changed"),
                 _r("d", expect_met=False)])
    assert s["total"] == 4
    assert s["up"] == 3
    assert s["down"] == 1
    assert s["changed"] == ["c"]
    # A down target is not also counted as "missing expect".
    assert s["missing_expect"] == ["d"]


def test_to_json_round_trips():
    data = json.loads(to_json([_r("a"), _r("b", ok=False, status="error")]))
    assert data["summary"]["up"] == 1
    assert len(data["results"]) == 2
    assert data["results"][0]["target"] == "a"
    assert data["results"][1]["ok"] is False
