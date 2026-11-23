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
    # Bools pass as numbers.
    "bool-is-a-number": ("src/litetui/settings.py",
                         "    if isinstance(raw, bool):\n        raise ValueError",
                         "    if False:\n        raise ValueError",
                         ["tests/test_thinking_number_setting.py"]),
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
