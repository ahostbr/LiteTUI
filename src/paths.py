"""The path anchors — ONE owner for where the data lives.

src/ is one level below the repo root, where the DATA lives — .convos,
settings.json, skills/, systemprompt.md. Anchoring to __file__.parent
would silently re-home every store into src/; this module sits at the same
depth app.py does, and test_paths.py proves the anchor by resolution.
Plugins import these; they never compute ROOT themselves.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"
SYSTEM_PROMPT_FILE = PROMPTS_DIR / "systemprompt.md"

# ── Conversation persistence ─────────────────────────────────────
# .convos/<uuid>/
#     convo.jsonl   append-only transcript
#     memory.md     INDEX the agent maintains — one line per memory
#     soul.md       who this agent is; persists across resumes
#     handoff.md    what is in flight, for whoever picks this up
#     memories/     the actual notes: i-learned-this.md, uncapped
CONVO_DIR = ROOT / ".convos"
MEMORIES_DIR = "memories"
