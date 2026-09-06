"""T219 — the one path every tool result crosses redacted ANSI and nothing else.

🔴 THE MEASURED INCIDENT, 2026-09-03 (card T219): an env listing / `cat .env` /
`grep` through the `bash` tool printed `LITESUITE_JWT_SECRET` and
`OPENAI_API_KEY` VERBATIM into the transcript, into the model's context, and into
a screenshot. Ryan rotated the keys. `sanitize.strip_escapes` was applied at
`app.py` — the single hygiene point for every tool result — and it strips escape
bytes only, so nothing between a subprocess's stdout and the model ever looked at
the payload.

    THE ONE PLACE THAT ALREADY SEES EVERY TOOL RESULT WAS THE PLACE THAT COULD
    HAVE CAUGHT IT, AND IT WAS ONLY LOOKING FOR ESCAPE BYTES.

⚠️ THE NAME LIST IS DERIVED, NOT REMEMBERED. Every secret-shaped variable
actually present in this machine's environment (NAMES only — no values were read
or written anywhere):

    CLAUDE_CODE_MESSAGING_TOKEN   LITESUITE_JWT_SECRET   OPENAI_API_KEY
    OPENCLAW_GATEWAY_TOKEN        STITCH_API_KEY

All five END with the keyword, and that is the rule: the keyword must terminate
the name.

🔴 AND THE REASON IT MUST, MEASURED IN THIS REPO. A naive "contains TOKEN" rule
would shred this app's own output — LiteTUI is full of token ACCOUNTING:

    32x prompt_tokens   29x max_tokens   16x max_tokens_tools   15x theme_tokens
    14x compact_max_tokens   8x completion_tokens   4x first_token_s

`prompt_tokens` ends in TOKENS (plural, not the keyword); `first_token_s` ends in
`_s`. Neither terminates in a secret keyword, so neither is touched. The control
arms below are the ones that matter most in review: a redactor that eats the
token counters is worse than none, because it corrupts every turn instead of
leaking on the rare one.

⬜ WHAT THIS DELIBERATELY DOES NOT DO: a free-floating high-entropy sweep. This
repo's ordinary output is full of git shas, hashes and base64 — an unanchored
entropy rule would redact them and make tool results unreadable. Entropy is used
ONLY to catch a value that follows a secret NAME in a form the separators miss.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from litetui import sanitize

ok = []


def chk(label, cond):
    ok.append(bool(cond))
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")


# Fabricated values that LOOK like the real shapes. Nothing here was read from
# the environment; a test that carried a real secret would be the defect.
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.ZmFrZS1wYXlsb2Fk.c2lnbmF0dXJlLW5vdC1yZWFs"
FAKE_SK = "sk-proj-FAKE0000aaaaBBBBccccDDDDeeeeFFFFgggg1111"

print("=== the incident shape: an env listing reaching the model ===")
env_listing = (
    "PATH=C:\\Windows\\System32\n"
    f"LITESUITE_JWT_SECRET={FAKE_JWT}\n"
    f"OPENAI_API_KEY={FAKE_SK}\n"
    "LITETUI_BACKEND=lmstudio\n"
)
red = sanitize.redact_secrets(env_listing)
chk("the JWT secret's VALUE is gone", FAKE_JWT not in red)
chk("the API key's VALUE is gone", FAKE_SK not in red)
chk("the KEY stays visible (LITESUITE_JWT_SECRET)", "LITESUITE_JWT_SECRET" in red)
chk("the KEY stays visible (OPENAI_API_KEY)", "OPENAI_API_KEY" in red)
chk("a marker says something was removed", sanitize.REDACTED_MARKER in red)
chk("non-secret lines untouched (PATH)", "PATH=C:\\Windows\\System32" in red)
chk("non-secret lines untouched (LITETUI_BACKEND)", "LITETUI_BACKEND=lmstudio" in red)

print("\n=== the other forms the same secret arrives in ===")
chk(
    "JSON form",
    FAKE_SK not in sanitize.redact_secrets(f'{{"api_key": "{FAKE_SK}", "n": 1}}'),
)
chk(
    "JSON form keeps the key and the rest of the object",
    '"api_key"' in sanitize.redact_secrets(f'{{"api_key": "{FAKE_SK}", "n": 1}}')
    and '"n": 1' in sanitize.redact_secrets(f'{{"api_key": "{FAKE_SK}", "n": 1}}'),
)
chk(
    "shell export form",
    FAKE_JWT not in sanitize.redact_secrets(f"export LITESUITE_JWT_SECRET={FAKE_JWT}"),
)
chk(
    "colon form (yaml / .ini / prose listing)",
    FAKE_SK not in sanitize.redact_secrets(f"OPENAI_API_KEY: {FAKE_SK}"),
)
chk(
    "lower-case name",
    FAKE_SK not in sanitize.redact_secrets(f"openai_api_key={FAKE_SK}"),
)
chk(
    "every secret-shaped name present on this box is covered",
    all(
        "SEKRIT" not in sanitize.redact_secrets(f"{n}=SEKRIT")
        for n in (
            "CLAUDE_CODE_MESSAGING_TOKEN",
            "LITESUITE_JWT_SECRET",
            "OPENAI_API_KEY",
            "OPENCLAW_GATEWAY_TOKEN",
            "STITCH_API_KEY",
        )
    ),
)

print("\n=== 🔴 CONTROL — this app's OWN output must survive byte-identical ===")
# If these fail, every turn is corrupted; the leak is rare, this would be constant.
counters = (
    '{"prompt_tokens": 1234, "completion_tokens": 88, "total_tokens": 1322}\n'
    "max_tokens=4096\n"
    "max_tokens_tools=2048\n"
    "compact_max_tokens=8000\n"
    "first_token_s=0.42\n"
    "theme_tokens: 15\n"
)
chk("token COUNTERS are untouched", sanitize.redact_secrets(counters) == counters)

ordinary = (
    "diff --git a/src/litetui/app.py b/src/litetui/app.py\n"
    "index 9f69742..a1b2c3d 100644\n"
    "    result = sanitize.strip_escapes(result)\n"
    "Traceback (most recent call last):\n"
    "  File \"C:/Projects/LiteTUI/src/litetui/app.py\", line 4190, in _dispatch\n"
    "the quick brown fox jumps over the lazy dog 0123456789\n"
)
chk("ordinary tool output is byte-identical", sanitize.redact_secrets(ordinary) == ordinary)
chk("empty string is byte-identical", sanitize.redact_secrets("") == "")
chk(
    "a git sha is not mistaken for a secret",
    sanitize.redact_secrets("commit 9f697421bd3e4c5a6f8d0e1a2b3c4d5e6f708192")
    == "commit 9f697421bd3e4c5a6f8d0e1a2b3c4d5e6f708192",
)
chk(
    "the word 'key' alone does not trigger redaction",
    sanitize.redact_secrets("key=value") == "key=value",
)

print("\n=== the escape stripper still does its own job ===")
chk(
    "strip_escapes is unchanged by this card",
    sanitize.strip_escapes("\x1b[31mred\x1b[0m") == "red",
)

print("\n=== 🔴 THE CALL SITE — a redactor nothing calls is not a fix ===")
# T407's lesson, in another repo: a perfectly tested predicate proves nothing
# about whether anything calls it. app.py:4190 is the ONE hygiene point every
# tool result crosses; if redaction is not applied there it is applied nowhere.
app_src = (Path(__file__).resolve().parent.parent / "src" / "litetui" / "app.py").read_text(
    encoding="utf-8"
)
chk("app.py calls redact_secrets on the tool-result path", "redact_secrets(" in app_src)
dispatch = app_src[app_src.find("strip_escapes(result)") - 2000 : app_src.find("strip_escapes(result)") + 2000]
chk("...in the same block as strip_escapes", "redact_secrets(" in dispatch)

print(f"\n{sum(ok)}/{len(ok)} passed")
sys.exit(0 if all(ok) else 1)
