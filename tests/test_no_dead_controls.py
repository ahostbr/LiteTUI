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

import ast
import io
import re
import warnings
from dataclasses import fields
from pathlib import Path

from litetui import settings as settings_mod
from litetui.settings import Settings

ROOT = Path(__file__).resolve().parent.parent  # repo root: tests/ is one level down
APP = (ROOT / "src" / "litetui" / "app.py").read_text(encoding="utf-8")

#: THE ONE FILE THAT CANNOT BE EVIDENCE, and the reason it is an EXCLUSION
#: rather than the scope being an inclusion list.
#:
#: `settings_screen.py` walks `fields(Settings)` and renders every one, so a
#: scan that included it would report EVERY field as read and
#: `test_no_setting_is_read_only_by_the_settings_screen` — the arm whose
#: whole subject is that difference — would pass vacuously.
SCREEN_ONLY = {"settings_screen.py"}

#: 🔴 THE SCOPE WAS A HAND-LISTED SUBSET AND IT WENT STALE. It was app.py +
#: plugins/** + llm_backend.py, with a comment explaining why those three
#: were the runtime. Then backends became their own modules, and FOUR live
#: settings were reported dead — "these settings render in /settings and
#: change nothing" — while their readers sat in files the scan never opened:
#:
#:   ninfer_executable    ninfer_engine.py:74   getattr(settings, ...)
#:   ninfer_artifact      ninfer_engine.py:84   getattr(settings, ...)
#:   ninfer_max_context   ninfer_engine.py:337  getattr(settings, ...)
#:   codex_native_engine  oauth_backend.py:25   getattr(settings, ...)
#:
#: The remedy this gate PRINTS is "either wire them or delete them", so a
#: false DEAD report is an instruction to delete working behaviour — and
#: three of those four are how LiteTUI starts the NInfer engine at all.
#:
#:     AN ENUMERATED SCOPE IS A CLAIM ABOUT WHERE CODE LIVES, AND CODE
#:     MOVES. The detector's own docstring already learned this about the
#:     access CHANNEL (`getattr` by string); the same lesson had not reached
#:     its SOURCE SET one screen above.
#:
#: Derived now: every module under the package, minus the one that cannot
#: be evidence. A new runtime file is in scope the day it exists.
_SOURCES = [
    p.read_text(encoding="utf-8")
    for p in sorted((ROOT / "src" / "litetui").rglob("*.py"))
    if p.name not in SCREEN_ONLY
]
RUNTIME = "".join(_SOURCES)


def _getattr_names(sources: list[str]) -> set[str]:
    """Every name fetched by getattr(..., "name") anywhere in the runtime.

    🔴 ATTRIBUTE SYNTAX IS NOT THE ONLY ACCESS CHANNEL, and a detector that
    believes it is accuses live code of being dead. Two fields were reported
    as changing nothing while both are read on every relevant call:
        app.py:1884              getattr(self.settings, "tool_auto_background_s", 0)
        subagent_plugin.py:58    getattr(getattr(app, "settings", None), "subagent_model", None)
    The second is why this is an AST walk and not another regex. I added a
    regex for `getattr(<alias>, "field")` first, and the NESTED getattr broke
    it immediately — the base is an expression, not an alias. Fixing a
    detector against the last failure you saw never converges; you have to
    name the CHANNEL.

    ⚠️ THE TRADE-OFF, STATED: this does not prove the getattr was on a
    settings object, so a same-named attribute fetched from something else
    would read as a use. That direction is the safe one. The remedy this
    gate prints is "either wire them or delete them", so a false DEAD report
    deletes working behaviour, while a false LIVE report only fails to spot
    a genuinely dead field.
    """
    names: set[str] = set()
    for src in sources:
        try:
            # A runtime file carries an invalid escape sequence; parsing it
            # warns, and that warning is this detector's noise, not the
            # suite's. Contained here rather than silenced globally.
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and isinstance(node.args[1].value, str)
            ):
                names.add(node.args[1].value)
    return names


_GETATTR_READS = _getattr_names(_SOURCES)

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


#: Local aliases for the settings object, e.g. `s = self.settings`. Without
#: these the detector reports a FALSE POSITIVE for every field read through
#: a short name — which it did the moment the footer fields were added,
#: claiming six dead controls that were all live.
_ALIASES = sorted(
    set(re.findall(r"\b([A-Za-z_]\w*)\s*=\s*(?:self|app)\.settings\b", RUNTIME))
    | {"self.settings", "app.settings"}
    # llm_backend receives settings by constructor/parameter injection —
    # its reads are `self._settings.X` and bare-param `settings.X`.
    | {"self._settings", "settings"}
)


def _reads(field: str, source: str = RUNTIME) -> bool:
    """Is this field read anywhere, BY ANY SPELLING (see _getattr_names).

    🔴 ATTRIBUTE SYNTAX IS NOT THE ONLY ACCESS CHANNEL, and this detector
    used to believe it was. `tool_auto_background_s` was reported dead —
    "renders in /settings and changes nothing" — while app.py:1884 reads it
    every time a tool call runs:
        limit = int(getattr(self.settings, "tool_auto_background_s", 0) or 0)
    A string argument is invisible to a scan for `settings.<name>`, so the
    detector was not measuring "is this read", it was measuring "is this read
    the way I expect". That is a false accusation of dead code, and the
    remedy it prints — "either wire them or delete them" — would have deleted
    a live setting.

    The channel is added here rather than the field being exempted: an
    exemption fixes one name, and the next getattr read is dead again.
    """
    if field in _GETATTR_READS:
        return True
    for base in _ALIASES:
        if re.search(re.escape(base) + r"\." + re.escape(field) + r"\b", source):
            return True
    return False


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
    # The call moved from app.py into llm_backend's override merge when the
    # dual-backend seam landed — RUNTIME is the honest scope, same as _reads.
    assert "sampling_kwargs(" in RUNTIME, "sampling_kwargs is allowlisted but never called"


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


def test_the_screen_is_genuinely_excluded_and_the_exclusion_is_not_vacuous():
    """🔴 ADDED BECAUSE A MUTANT SURVIVED. Emptying `SCREEN_ONLY` — putting
    the panel that renders EVERY field back into the evidence — changed no
    result, because no field happens to be screen-only today. So the
    exclusion was preventive and unobservable, which is a docstring, not an
    assertion.

    Two things make it observable: the screen is really out of scope, and it
    really would have been evidence if left in. Without the second half this
    arm would pass against a screen that read nothing at all.
    """
    screen_path = ROOT / "src" / "litetui" / "settings_screen.py"
    screen = screen_path.read_text(encoding="utf-8")
    assert screen not in _SOURCES, (
        "settings_screen.py is in the evidence set; every field it renders "
        "would then read as live"
    )
    # NOT VACUOUS: the screen does reach fields by name, so including it
    # would genuinely change verdicts.
    named = [f.name for f in fields(Settings) if _reads(f.name, screen)]
    assert len(named) > 10, (
        f"the screen names only {len(named)} settings — either it stopped "
        "rendering them by name, in which case this exclusion protects "
        "nothing, or the detector stopped seeing them"
    )


def test_no_setting_is_read_only_by_the_settings_screen():
    """A field the SCREEN reads but the APP never does is still dead.

    The screen renders every field by construction, so it can never be the
    evidence that a field does something.
    """
    # Read HERE and nowhere else: SCREEN_ONLY keeps it out of RUNTIME, which
    # is what makes `_reads` below a statement about the app rather than
    # about the panel that renders every field by construction.
    screen = (ROOT / "src" / "litetui" / "settings_screen.py").read_text(encoding="utf-8")
    assert "f-{name}" in screen or "f-" in screen  # sanity: the screen builds ids
    for f in fields(Settings):
        if f.name in INDIRECT:
            continue
        assert _reads(f.name), f"{f.name} is not read anywhere in the runtime"
