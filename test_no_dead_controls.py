"""Every declared setting must actually be read by the app.

WHY THIS EXISTS. The /settings panel shipped with 10 of its 30 controls read
NOWHERE — they rendered, accepted input, validated, saved to disk, and changed
nothing. Every one of them looked like it worked. That is the failure mode the
whole settings change set was written to prevent, and it shipped inside the fix.

A control that lies is worse than a missing one: a missing control sends you to
look elsewhere, a lying one teaches you the app is broken and that your input
does not matter.

⚠️ THIS TEST'S OWN INSTRUMENT IS THE THING MOST LIKELY TO BE WRONG. The first
audit used a bare `settings.<field>` grep and reported 19 dead — 9 of those were
FALSE POSITIVES, consumed indirectly through `sampling_kwargs()`. So the
indirect consumers are declared explicitly below rather than pattern-matched,
and the test carries a NEGATIVE CONTROL proving the detector can still fail.
"""

from __future__ import annotations

import io
import re
from dataclasses import fields
from pathlib import Path

import settings as settings_mod
from settings import Settings

ROOT = Path(__file__).resolve().parent
APP = (ROOT / "app.py").read_text(encoding="utf-8")

#: Fields consumed through a helper rather than by name. Each entry names the
#: helper, so a reader can check the claim instead of trusting the list.
INDIRECT: dict[str, str] = {
    "temperature": "sampling_kwargs()",
    "top_p": "sampling_kwargs()",
    "top_k": "sampling_kwargs()",
    "min_p": "sampling_kwargs()",
    "repeat_penalty": "sampling_kwargs()",
    "presence_penalty": "sampling_kwargs()",
    "frequency_penalty": "sampling_kwargs()",
    "seed": "sampling_kwargs()",
    "stop": "sampling_kwargs()",
}


def _reads(field: str, source: str = APP) -> bool:
    return re.search(r"settings\." + re.escape(field) + r"\b", source) is not None


def test_every_setting_is_read_somewhere():
    dead = [
        f.name
        for f in fields(Settings)
        if f.name not in INDIRECT and not _reads(f.name)
    ]
    assert dead == [], (
        "These settings render in /settings and change nothing:\n  "
        + "\n  ".join(dead)
        + "\n\nEither wire them or delete them. A control that accepts input and "
        "does nothing is worse than one that is absent."
    )


def test_the_indirect_helper_is_actually_called():
    """The INDIRECT allowlist is only honest while its helper is invoked.

    Without this, adding a name to INDIRECT would be a way to silence the test
    above — the allowlist would become the loophole rather than the exception.
    """
    assert "sampling_kwargs(" in APP, "sampling_kwargs is allowlisted but never called"


def test_indirect_fields_really_are_in_the_helper():
    """And the helper must genuinely emit each allowlisted field."""
    s = Settings()
    for name in INDIRECT:
        setattr(s, name, ["x"] if name == "stop" else 1)
    emitted = set(settings_mod.sampling_kwargs(s))
    missing = sorted(set(INDIRECT) - emitted)
    assert missing == [], f"allowlisted as indirect but sampling_kwargs never emits: {missing}"


def test_detector_can_still_fail():
    """NEGATIVE CONTROL — a source that reads nothing must be reported dead.

    The audit that produced this test was wrong in both directions on its first
    run. A detector nobody has seen fail is not evidence.
    """
    empty = "print('this module reads no settings at all')"
    assert not _reads("tool_iterations", empty)
    assert _reads("tool_iterations", "x = self.settings.tool_iterations")


def test_no_setting_is_read_only_by_the_settings_screen():
    """A field the SCREEN reads but the APP never does is still dead.

    The screen renders every field by construction, so it can never be the
    evidence that a field does something.
    """
    screen = (ROOT / "settings_screen.py").read_text(encoding="utf-8")
    assert "f-{name}" in screen or "f-" in screen  # sanity: the screen builds ids
    for f in fields(Settings):
        if f.name in INDIRECT:
            continue
        assert _reads(f.name), f"{f.name} is not read by app.py"
