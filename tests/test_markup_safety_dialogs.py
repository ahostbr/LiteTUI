"""Tool arguments and model-authored question text must not be markup-parsed.

The crash this guards, seen live 2026-09-04 (logs/crash-9-4-2026.txt):

    MarkupError: Expected markup value (found '\\"searchbox\\"]"\\n')

A tool was called with a CSS attribute selector in its arguments,
`[data-testid="searchbox"]`. `approval_preview` (tool_policy.py) renders the
args with `json.dumps`, WHICH ESCAPES THE QUOTES, and the result went into a
bare `Static(...)`. Textual markup-parses a plain str, reads `[data-testid=`
as a tag opening an attribute, wants a value, finds `\\"` and raises -- during
LAYOUT, which is why the traceback bottoms out in `get_content_height` rather
than anywhere near the call site.

⚠️ THE TRIGGER IS NARROWER THAN IT LOOKS, AND GUESSING IT WRONG COSTS A DAY.
Measured against Textual 8.0.2, `.visual` forced so the parse actually runs:

    pick [data-testid="searchbox"]      -> OK, renders literally
    pick [data-testid=\\"searchbox\\"]    -> MarkupError
    hello [foo] world                   -> OK
    hello [b                            -> OK
    try [href^="http"]                  -> OK

So a model writing a bracketed attribute in PROSE does not crash anything.
It takes the BACKSLASH-ESCAPED quotes -- which is to say, it takes JSON. That
makes `approval_preview`'s `json.dumps` the mechanism, not an aggravating
factor: any path that serialises to JSON and hands the result to a
markup-parsing Static is the hazard, and prose is mostly not.

    THE SERIALISER, NOT THE AUTHOR, IS WHAT MAKES THE STRING HOSTILE.

⭐ WHY tests/test_markup_safety.py DID NOT CATCH THIS, WHICH IS THE REAL
LESSON. That file is good: it reproduces the hazard, keeps a positive
control, and explains the grammar. Then it checks FOUR HAND-LISTED LINES in
app.py. `tool_approval.py` and `ask_user_question.py` were never in its
scope, so the guard could not have fired however hostile the input got.

    A GUARD THAT ENUMERATES ITS SITES BY HAND CANNOT SEE A SITE THAT DID NOT
    EXIST WHEN IT WAS WRITTEN. Its green is a statement about the list, not
    about the codebase.

Hence the scan below rather than a fifth hardcoded line: it walks the AST of
the two dialogs that render tool- and model-supplied text and requires
`markup=False` on every `Static(...)` whose content is not an author-written
literal. A NEW Static added to either dialog is caught without anyone
remembering to extend a list. It is an AST walk, not a grep, because a text
gate counts the prose ABOUT the thing -- a comment mentioning `markup=False`
would satisfy a string search perfectly.
"""
import ast
from pathlib import Path

import pytest
from textual.widgets import Static

SRC = Path(__file__).resolve().parent.parent / "src" / "litetui"

# The dialogs that render text this process did not author: tool arguments
# (arbitrary, JSON-serialised) and question/option text (model-authored).
UNTRUSTED_DIALOGS = ["tool_approval.py", "ask_user_question.py"]


def _visual(content, **kwargs):
    """Force the markup pass. Static.__init__ is LAZY -- it stores the string
    and parses in the `visual` property, which is what get_content_height
    reaches. Constructing a Static with hostile content therefore raises
    NOTHING, and a test that only constructs one proves nothing at all."""
    return Static(content, **kwargs).visual


# --- the hazard is real, in this toolkit, right now -------------------------
def test_the_2026_09_04_crash_reproduces():
    """POSITIVE CONTROL for this file. If this stops raising, Textual changed
    and the source scan below is guarding a ghost -- delete or rewrite it
    rather than enjoying the green."""
    import json

    payload = json.dumps({"selector": '[data-testid="searchbox"]'}, indent=2)
    with pytest.raises(Exception) as exc:
        _visual(payload)
    assert "markup value" in str(exc.value).lower()


def test_markup_false_survives_the_same_payload():
    """The fix, on the exact failing input."""
    import json

    payload = json.dumps({"selector": '[data-testid="searchbox"]'}, indent=2)
    assert _visual(payload, markup=False) is not None


def test_plain_bracket_prose_never_crashed_which_is_why_json_is_the_story():
    """Guards against the over-claim: it is tempting to say 'a model writing
    a bracketed attribute crashes the dialog'. It does not. Anyone narrowing
    the fix on the theory that prose is the hazard should read this."""
    assert _visual('pick [data-testid="searchbox"]') is not None
    assert _visual("hello [foo] world") is not None
    assert _visual("hello [b") is not None


# --- every dynamic Static in the untrusted dialogs is opted out -------------
def _dynamic_statics(path: Path):
    """Yield (lineno, source-snippet) for Static(...) calls whose first
    positional argument is NOT an author-written string literal."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", None)
        if name != "Static" or not node.args:
            continue
        first = node.args[0]
        # A plain literal is author-controlled and cannot surprise us.
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            continue
        opted_out = any(
            kw.arg == "markup"
            and isinstance(kw.value, ast.Constant)
            and kw.value.value is False
            for kw in node.keywords
        )
        if not opted_out:
            yield node.lineno, ast.unparse(first)[:60]


@pytest.mark.parametrize("filename", UNTRUSTED_DIALOGS)
def test_no_dynamic_static_is_markup_parsed(filename):
    offenders = list(_dynamic_statics(SRC / filename))
    assert not offenders, (
        f"{filename}: Static(...) with non-literal content and no markup=False:\n"
        + "\n".join(f"  line {ln}: {snippet}" for ln, snippet in offenders)
    )


@pytest.mark.parametrize("filename", UNTRUSTED_DIALOGS)
def test_the_scan_actually_finds_statics(filename):
    """NEGATIVE CONTROL for the scan itself. A walk that matched nothing --
    a renamed import, a refactor to a subclass -- would pass the test above
    vacuously and for ever. Assert it can still see the widgets it audits."""
    tree = ast.parse((SRC / filename).read_text(encoding="utf-8"))
    found = sum(
        1
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", None))
        == "Static"
    )
    assert found >= 3, f"{filename}: scan found only {found} Static calls"
