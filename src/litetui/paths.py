"""The path anchors — ONE owner for where the data lives.

src/litetui/ is TWO levels below the repo root, where the DATA lives — .convos,
settings.json, skills/. Anchoring too shallow silently re-homes every store
inside the package; test_paths.py proves the anchor by resolution.

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

PROMPTS_DIR (T135): authored prompt text now SHIPS inside the package —
src/litelitui/prompts/ — because an installed wheel has no repo root to read
from. The anchor below prefers the repo-root copy only while one still exists
(a stale checkout); a fresh dev tree and an installed wheel both resolve to the
package directory, where the files live (see pyproject package-data).
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def data_root() -> Path:
    """Opt-in durable data location; resources/workspace retain their anchors.

    Set before process startup. An unset/empty override preserves the legacy
    location exactly; resolving ROOT here would change monkeypatched callers.
    """
    override = os.environ.get("LITETUI_DATA_ROOT")
    return Path(override).expanduser().resolve() if override else ROOT


# ── Prompt text: ships inside the package (T135) ────────────────
_PKG_PROMPTS = Path(__file__).resolve().parent / "prompts"
_REPO_PROMPTS = ROOT / "prompts"
#: Where the prompt text SHIPS. Reads go through `prompt_file()`, which lets a
#: repo-root copy win PER FILE; this stays a directory because two call sites in
#: textfmt.py monkeypatch it in the suite to point at a scratch prompts/ dir.
PROMPTS_DIR = _PKG_PROMPTS


def prompt_file(name: str) -> Path:
    """The prompt named `name`, repo-root copy winning PER FILE (T582).

    The anchor used to be all-or-nothing: a repo-root `prompts/` holding ONLY
    `systemprompt.md` moved EVERY prompt read to that directory, and
    `textfmt.load_prompt` reads its files unguarded. Overriding the one file the
    docstring in `prompts/__init__.py` invites you to override therefore raised
    FileNotFoundError on `compact.md`, `wake-after-compact.md` and
    `tool-denied.md` -- the exact crash class T135 was written to remove,
    reintroduced by the shape of the anchor rather than by a missing file.

    Per-file makes that docstring's promise ("override any file") true, and a
    partial override stays partial.
    """
    repo = _REPO_PROMPTS / name
    return repo if repo.is_file() else PROMPTS_DIR / name


SYSTEM_PROMPT_FILE = prompt_file("systemprompt.md")
#: The tools section of the system prompt. Lived as a string constant in
#: app.py until 2026-08-22 -- authored prompt text belongs on disk beside the
#: rest of it, where it can be read and edited without a source change.
TOOLS_PROMPT_FILE = prompt_file("tools.md")
#: The plan-mode section (T558). Rendered only while plan mode is on, so leaving
#: the mode drops the instruction rather than leaving a stale one in context.
PLAN_PROMPT_FILE = prompt_file("plan-mode.md")

# ── Conversation persistence ─────────────────────────────────────
# .convos/<uuid>/
#     convo.jsonl   append-only transcript
#     memory.md     INDEX the agent maintains — one line per memory
#     soul.md       who this agent is; persists across resumes
#     handoff.md    what is in flight, for whoever picks this up
#     memories/     the actual notes: i-learned-this.md, uncapped
CONVO_DIR = data_root() / ".convos"
MEMORIES_DIR = "memories"

# ── llama.cpp backend working files ──────────────────────────────────
# .llama/
#     litetui-models.ini        generated router preset — derived output,
#                               regenerated from settings; never hand-edited
#     litetui-llama-server.log  the spawned server's whole console — a child
#                               of a TUI must NEVER inherit the terminal
LLAMA_DIR = ROOT / ".llama"
