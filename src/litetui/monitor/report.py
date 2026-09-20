"""Human- and machine-readable output for a sweep.

``format_report`` is what a human reading the terminal sees; ``to_json`` is
what a downstream tool or the Grafana path would consume. Both read only the
``CheckResult`` fields, so they can be unit-tested against canned results
without a probe or a sink.
"""

from __future__ import annotations

import json
from typing import Sequence

from litetui.monitor.engine import CheckResult

_MARK = {"ok": "+", "error": "x"}


def _bar(ms: float, width: int = 20, cap_ms: float = 5000.0) -> str:
    frac = 0.0 if cap_ms <= 0 else min(1.0, ms / cap_ms)
    filled = int(round(frac * width))
    return "#" * filled + "." * (width - filled)


def format_report(results: Sequence[CheckResult], title: str = "monitor sweep") -> str:
    lines: list[str] = []
    lines.append(f"== {title} ==")
    if not results:
        lines.append("(no targets configured)")
        return "\n".join(lines)

    for r in results:
        mark = _MARK.get(r.status, "?")
        expect = "" if r.expect_met else "  [expect text missing]"
        reg = f"  [visual: {r.regression}]" if r.regression not in ("none", "stable") else ""
        lines.append(f" {mark} {r.target:<16} {r.status:<5} {r.load_ms:8.0f}ms "
                     f"{_bar(r.load_ms)}  text={r.text_chars}{expect}{reg}")
        if r.error:
            lines.append(f"     error: {r.error}")

    ok = sum(1 for r in results if r.ok)
    lines.append(f"-- {ok}/{len(results)} up --")
    return "\n".join(lines)


def summary(results: Sequence[CheckResult]) -> dict:
    total = len(results)
    up = sum(1 for r in results if r.ok)
    changed = [r.target for r in results if r.regression == "changed"]
    missing_expect = [r.target for r in results if not r.expect_met and r.ok]
    return {
        "total": total,
        "up": up,
        "down": total - up,
        "changed": changed,
        "missing_expect": missing_expect,
    }


def to_json(results: Sequence[CheckResult]) -> str:
    return json.dumps(
        {"summary": summary(results), "results": [r.as_json() for r in results]},
        indent=2,
    )
