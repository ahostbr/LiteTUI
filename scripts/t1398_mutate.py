"""T1398 mutation arms: apply one mutation, run the named tests, restore from git. Usage:
python scripts/t1398_mutate.py <arm>"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = r"C:\Projects\LiteTUI\.venv\Scripts\python.exe"

ARMS = {
    # NInfer checks UP before down; checking down first sends Qwen's saved high to medium.
    "nearest-down-first": ("src/litetui/effort_scale.py",
                           "        if up < len(EFFORT_ORDER) and EFFORT_ORDER[up] in graded:\n            return EFFORT_ORDER[up]\n        if down >= 0 and EFFORT_ORDER[down] in graded:\n            return EFFORT_ORDER[down]\n",
                           "        if down >= 0 and EFFORT_ORDER[down] in graded:\n            return EFFORT_ORDER[down]\n        if up < len(EFFORT_ORDER) and EFFORT_ORDER[up] in graded:\n            return EFFORT_ORDER[up]\n",
                           ["tests/test_effort_scale.py"]),
    # Off always top-level: the llama.cpp/Inkling 0.9 bug comes back.
    "off-top-level": ("src/litetui/effort_scale.py",
                      "return Resolved(KWARGS if NONE in self.numbers else TOP, NONE)",
                      "return Resolved(TOP, NONE)",
                      ["tests/test_effort_scale.py"]),
    # Drop the settings_service special case: a name would be checked as a float and refused.
    "service-guard-off": ("src/litetui/settings_service.py",
                          "            if change.key in st.THINKING_FIELDS:",
                          "            if False:",
                          ["tests/test_thinking_number_setting.py"]),
    # A 200 alone counts as carrying the number.
    "channel-200-is-enough": ("src/litetui/effort_scale.py",
                              "number_wire = carried is not None and carried == renders[name]",
                              "number_wire = carried is not None",
                              ["tests/test_effort_discovery.py"]),
    # Each render group's highest name, with no check against NInfer's nearest rule.
    "groups-highest-only": ("src/litetui/effort_scale.py",
                            "        if all(nearest_accepted(name, choice) == choice[i] for i, group in enumerate(members) for name in group):",
                            "        if True:",
                            ["tests/test_effort_discovery.py"]),
    # The app's chat turn does not pass the scale (single line; app.py is mixed-EOL).
    "app-turn-ignores-scale": ("src/litetui/app.py",
                               "                effort_scale=effort_scale,",
                               "                effort_scale=None,",
                               ["tests/test_effort_wire_capture.py"]),
    # The re-pin forgets where the chat turn put the effort.
    "pin-ignores-placement": ("src/litetui/turn_engine.py",
                              "        if placement == \"kwargs\":",
                              "        if False:",
                              ["tests/test_effort_scale.py"]),
    # Bools pass as numbers.
    "bool-is-a-number": ("src/litetui/settings.py",
                         "    if isinstance(raw, bool):\n        raise ValueError",
                         "    if False:\n        raise ValueError",
                         ["tests/test_thinking_number_setting.py"]),
    # Prefix 5c1db80f 2: a scale that could not be read is probed again on every turn.
    "miss-not-cached": ("src/litetui/app.py",
                        "        if miss is not None and time.monotonic() - miss[0] < EFFORT_SCALE_MISS_TTL:",
                        "        if False:",
                        ["tests/test_effort_wire_capture.py"]),
    # 1a: the picker loses its number row.
    "no-number-row": ("src/litetui/plugins/misc.py",
                      "        if scale.numbers:\n            # Prefix 5c1db80f",
                      "        if False:\n            # Prefix 5c1db80f",
                      ["tests/test_effort_ui.py"]),
    # 1a: the number dialog takes any number (no /think rule).
    "dialog-skips-rule": ("src/litetui/plugins/misc.py",
                          "        why = number_refusal(number, scale, label)",
                          "        why = None",
                          ["tests/test_effort_ui.py"]),
    # 1b: Settings saves a new number without the /think rule.
    "settings-skips-rule": ("src/litetui/settings_screen.py",
                            "        why = number_refusal(number, app_scale(self.app), backend_label(self.app))",
                            "        why = None",
                            ["tests/test_effort_ui.py"]),
    # 1b: a stored number is re-checked on every Save (blocks Save on a backend with no scale).
    "settings-rechecks-stored": ("src/litetui/settings_screen.py",
                                 "        if is_number(stored) and float(stored) == float(number):",
                                 "        if False:",
                                 ["tests/test_effort_ui.py"]),
    # 1b: Settings ignores the box and saves the Select's value ("manual").
    "settings-ignores-box": ("src/litetui/settings_screen.py",
                             "                setattr(out, name, self._thinking_value(name, widget.value))",
                             "                setattr(out, name, widget.value)",
                             ["tests/test_effort_ui.py"]),
}

arm = sys.argv[1]
path, old, new, tests = ARMS[arm]
target = ROOT / path
text = target.read_bytes().decode("utf-8")
assert text.count(old) == 1, f"{arm}: anchor not found exactly once"
target.write_bytes(text.replace(old, new).encode("utf-8"))
numstat = subprocess.run(["git", "diff", "--numstat", "--", path], cwd=ROOT, capture_output=True, text=True).stdout.strip()
print(f"[{arm}] mutated: {numstat}")
try:
    run = subprocess.run([PY, "-m", "pytest", *tests, "-q", "-p", "no:cacheprovider", "--no-header"],
                         cwd=ROOT, capture_output=True, text=True)
    print(f"[{arm}] " + run.stdout.strip().splitlines()[-1])
finally:
    subprocess.run(["git", "checkout", "--", path], cwd=ROOT, check=True)
    left = subprocess.run(["git", "diff", "--numstat", "--", path], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    print(f"[{arm}] restored: {'clean' if not left else left}")
