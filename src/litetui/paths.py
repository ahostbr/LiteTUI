"""The path anchors — ONE owner for where the data lives.

src/litetui/ is TWO levels below the repo root, where the DATA lives — .convos,
settings.json, skills/, systemprompt.md. Anchoring too shallow silently re-homes
every store inside the package; test_paths.py proves the anchor by resolution.

🔴 THE DEPTH IS COUNTED BY HAND AND IT HAS ALREADY BEEN WRONG ONCE. When this
module moved from src/paths.py to src/litetui/paths.py, `parent.parent` kept
resolving — to src/ instead of the repo root — and 13 test files failed with
`FileNotFoundError: src/tools/harness.json`. Nothing warned; a path anchor that
is off by one directory is still a valid Path.

⚠️ FIVE OTHER MODULES COMPUTE THE SAME ANCHOR THEMSELVES rather than importing
ROOT from here — app.py, chrome_tool.py, pccontrol_tool.py, seat_guard.py and
settings.py — so the count below is duplicated six ways and every copy must be
edited together. The line under this one claims plugins never compute ROOT
themselves; that is true of plugins/ and false of those five. Consolidating them
onto this ROOT would delete the whole class, and is deliberately NOT done in a
rename commit.
Plugins import these; they never compute ROOT themselves.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
PROMPTS_DIR = ROOT / "prompts"
SYSTEM_PROMPT_FILE = PROMPTS_DIR / "systemprompt.md"
#: The tools section of the system prompt. Lived as a string constant in
#: app.py until 2026-08-22 -- authored prompt text belongs on disk beside the
#: rest of it, where it can be read and edited without a source change.
TOOLS_PROMPT_FILE = PROMPTS_DIR / "tools.md"

# ── Conversation persistence ─────────────────────────────────────
# .convos/<uuid>/
#     convo.jsonl   append-only transcript
#     memory.md     INDEX the agent maintains — one line per memory
#     soul.md       who this agent is; persists across resumes
#     handoff.md    what is in flight, for whoever picks this up
#     memories/     the actual notes: i-learned-this.md, uncapped
CONVO_DIR = ROOT / ".convos"
MEMORIES_DIR = "memories"

# ── llama.cpp backend working files ──────────────────────────────────
# .llama/
#     litetui-models.ini        generated router preset — derived output,
#                               regenerated from settings; never hand-edited
#     litetui-llama-server.log  the spawned server's whole console — a child
#                               of a TUI must NEVER inherit the terminal
LLAMA_DIR = ROOT / ".llama"
