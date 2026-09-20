"""Visual regression: is the page I just screenshotted the same as before?

The approach is deliberately coarse and cheap — a SHA-256 over the screenshot
bytes — because the job is to catch *something changed* (a layout shift, a
broken image, a missing banner), not to compute a pixel-diff score. A hash
mismatch says "the capture is different, look at it"; it never says "the
design is wrong". That keeps the signal honest and the cost near-zero.

Each target holds one baseline hash. The first capture sets the baseline
(``"baseline"``); later captures that match it are ``"stable"``; a mismatch is
``"changed"``. The baseline is persisted to a small JSON file so it survives
between runs and can be reset by deleting it.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

BASELINE = "baseline"
STABLE = "stable"
CHANGED = "changed"
NONE = "none"  # no screenshot was taken, so nothing to compare


def fingerprint_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fingerprint_path(path: str | Path) -> str | None:
    p = Path(path)
    if not p.exists():
        return None
    return fingerprint_bytes(p.read_bytes())


@dataclass
class RegressionResult:
    target: str
    verdict: str  # one of the module constants
    baseline: str
    current: str
    baseline_set: bool  # True when this call established the baseline

    def as_metric(self) -> float:
        """A 0/1/2 code a dashboard can plot. 2 (changed) is the interesting one."""
        return {NONE: 0.0, BASELINE: 0.0, STABLE: 0.0, CHANGED: 1.0}[self.verdict]


class RegressionTracker:
    """Holds a per-target baseline and classifies each new capture.

    ``state_path`` is optional; when given, baselines are loaded on start and
    saved after each new baseline so they persist across runs.
    """

    def __init__(self, state_path: str | Path | None = None) -> None:
        self.state_path = Path(state_path) if state_path else None
        self._baseline: dict[str, str] = {}
        if self.state_path is not None and self.state_path.exists():
            self._baseline = json.loads(self.state_path.read_text(encoding="utf-8"))

    def _save(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self._baseline, indent=2, sort_keys=True), encoding="utf-8"
        )

    def observe(self, target: str, fp: str | None) -> RegressionResult:
        if fp is None:
            return RegressionResult(target, NONE, "", "", False)

        base = self._baseline.get(target)
        if base is None:
            self._baseline[target] = fp
            self._save()
            return RegressionResult(target, BASELINE, fp, fp, True)

        verdict = STABLE if base == fp else CHANGED
        return RegressionResult(target, verdict, base, fp, False)
