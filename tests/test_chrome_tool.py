"""The chrome tool's paths must point at things that exist, and its actions
must be reachable.

🔴 WHY THIS FILE EXISTS. `chrome_tool.py` carries a comment warning that moving
the bridge directory REQUIRES editing the path constant beneath it. When the
move happened, `SCRIPT` was repointed and `SHOT_DIR` — four lines below that
warning — was not. It named `LiteTUI/chrome-bridge`, a directory that no longer
exists, so `shot` wrote nowhere and the action was silently broken.

That is the same defect the warning describes, committed inside the warning's
own blast radius. A comment is not a check.

THE GENERAL ARM is the point: every path constant this module derives is
asserted to EXIST, so the next move fails here instead of removing a capability
without saying anything. `SCRIPT.exists()` is the tool-registration gate, which
means a wrong path does not error — it deletes the tool from the model's list.
"""

from __future__ import annotations

from pathlib import Path

from litetui import chrome_tool


def test_every_path_constant_points_at_something_real():
    # The arm that would have caught SHOT_DIR the day the directory moved.
    for name in ("SCRIPT", "RELAY_DIR", "SHOT_DIR"):
        p = Path(str(getattr(chrome_tool, name)))
        assert p.exists(), f"{name} -> {p} does not exist"


def test_shot_writes_beside_the_bridge_not_at_the_old_root():
    # SHOT_DIR named the pre-move location. Anchoring it to SCRIPT's parent is
    # what stops the two drifting apart again.
    assert chrome_tool.SHOT_DIR == chrome_tool.SCRIPT.parent
    assert "tools" in chrome_tool.SHOT_DIR.parts


def test_the_relay_lifecycle_is_part_of_the_tool():
    """Before this, an agent that hit the idle relay could only report it.

    Ryan: "that should be apart of the agents chrome tool, starting and stoping
    it."
    """
    for a in ("start", "stop", "status"):
        assert a in chrome_tool.ACTIONS, a


def test_write_text_is_offered_and_documented():
    assert "write_text" in chrome_tool.ACTIONS
    props = chrome_tool.CHROME_TOOL_SPEC["function"]["parameters"]["properties"]
    for key in ("text", "clear", "enter"):
        assert key in props, f"write_text parameter {key!r} is not declared"
    # An action the model cannot discover is an action it will not use.
    desc = chrome_tool.CHROME_TOOL_SPEC["function"]["description"]
    assert "write_text" in desc


def test_every_action_is_reachable_from_the_spec_enum():
    """A dispatch branch nobody can select is dead, and an enum value with no
    branch is a promise the tool breaks on first use."""
    enum = set(chrome_tool.CHROME_TOOL_SPEC["function"]["parameters"]["properties"]
               ["action"]["enum"])
    assert enum == set(chrome_tool.ACTIONS)


def test_write_text_refuses_without_text():
    out = chrome_tool.run({"action": "write_text"})
    assert out.startswith("[error]")
    assert "text" in out.lower()


def test_an_unknown_action_names_what_is_valid():
    out = chrome_tool.run({"action": "typo"})
    assert "write_text" in out and "start" in out


def test_the_no_extension_hint_does_not_claim_the_relay_is_up():
    """It fires on the DIRECT-mode path too, where the relay is deliberately
    stopped. The first wording opened "the relay is running but ...", which is
    false there — and a hint that states a wrong fact sends the reader to the
    wrong half of the system, which is the opposite of this module's job."""
    hint = chrome_tool._NO_EXT_HINT
    assert "relay is running" not in hint
    assert chrome_tool._looks_like_no_extension("ChromeError: no extension connected")
    assert not chrome_tool._looks_like_no_extension("some other failure")


def test_scroll_is_offered_and_documented():
    assert "scroll" in chrome_tool.ACTIONS
    props = chrome_tool.CHROME_TOOL_SPEC["function"]["parameters"]["properties"]
    assert "scroll" in props["action"]["enum"]
    for key in ("dy", "to"):
        assert key in props, f"scroll parameter {key!r} is not declared"
    assert "scroll" in chrome_tool.CHROME_TOOL_SPEC["function"]["description"]


def test_scroll_refuses_without_dy_or_to():
    """Neither `dy` nor `to` means there is nothing to move by — refuse before
    spawning a child, like write_text refuses without text."""
    out = chrome_tool.run({"action": "scroll"})
    assert out.startswith("[error]")
    assert "dy" in out and "to" in out


def test_scroll_rejects_a_bad_to():
    out = chrome_tool.run({"action": "scroll", "to": "middle"})
    assert out.startswith("[error]")
    assert "top" in out and "bottom" in out


def test_scroll_rejects_a_non_numeric_dy():
    out = chrome_tool.run({"action": "scroll", "dy": "a lot"})
    assert out.startswith("[error]")
    assert "pixels" in out


def test_a_traceback_is_reduced_to_its_cause():
    # bridge.py surfaces failures as an uncaught ChromeError, so its stderr is a
    # dozen frames ending in the one line that says what happened.
    trace = (
        "Traceback (most recent call last):\n"
        '  File "bridge.py", line 604, in <module>\n'
        "    raise SystemExit(_main(sys.argv[1:]))\n"
        "ChromeError: no extension connected to the relay"
    )
    assert chrome_tool._last_error_line(trace) == (
        "ChromeError: no extension connected to the relay"
    )
