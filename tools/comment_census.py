"""Comment/docstring census across a code move — the third instrument.

WHY THIS EXISTS. When a body moves between files we check it two ways, and both
are blind to the same thing:

    text diff   SEES a loss, cannot CLASSIFY it   -- a bare `-return` in a
                whitespace-normalised diff reads as deleted control flow when it
                is really a stripped trailing comment.
    AST diff    CLASSIFIES exactly, cannot SEE it -- comments are not in the tree
                at all, so "AST-identical" is a true statement that is silent
                about rationale BY CONSTRUCTION.

Measured on LiteTUI T070, 2026-08-24: the AST comparison returned IDENTICAL,
5 statements both sides, at the exact moment 11 comment lines were missing across
a batch of ten moved bodies. Not close — silent by design. Two of the three
losses were whole-body: every comment in `store_block` (7) and `append_tps_into`
(3) was dropped in the O2 lift, including the note explaining why a live string
must change whenever the store's injection cadence does. THE GUARD-RAIL WAS
REMOVED AND THE CLIFF WAS LEFT.

⇒ Run all three on any move that reformats. This is the one that reads comments.

USAGE
    python tools/comment_census.py
        Re-runs the built-in T070 manifest (the ten bodies moved on 2026-08-24).

    python tools/comment_census.py --move PRE_REF:OLD:NEW_PATH:NEW[:class]
        One --move per body; repeatable. `:class` means the NEW name is a method
        of LiteTUI rather than a module-level function. The OLD name is always
        looked up as a method of LiteTUI in PRE_REF's src/litetui/app.py.

    python tools/comment_census.py --move 22a7834^:_store_block:src/litetui/appsvc.py:store_block

🔴 BOTH SIDES ARE READ FROM COMMITTED BLOBS, NEVER FROM THE WORKING TREE. In a
shared checkout the tree carries another agent's in-flight edits, and a census
that reads it reports on a ref that does not exist. That mistake was made twice
in one session — the second time inside the check of the first — so it is closed
here in code rather than left to discipline: see `blob()`, and note there is no
code path that opens a file for the comparison.
"""
from __future__ import annotations

import argparse
import ast
import io
import subprocess
import sys
import textwrap
import tokenize

APP = "src/litetui/app.py"

# The ten bodies moved on 2026-08-24 (O2 lift, the four returns, S3).
MOVES = [
    ("22a7834^", "_load_skills",      "src/litetui/appsvc.py", "load_skills",       False),
    ("22a7834^", "_load_image_file",  "src/litetui/appsvc.py", "load_image_file",   False),
    ("22a7834^", "_store_block",      "src/litetui/appsvc.py", "store_block",       False),
    ("22a7834^", "_append_tps_into",  "src/litetui/appsvc.py", "append_tps_into",   False),
    ("22a7834^", "_append_to_system", APP,                     "_append_to_system", True),
    ("22a7834^", "_glassbox",         APP,                     "_glassbox",         True),
    ("22a7834^", "_glassbox_tool",    APP,                     "_glassbox_tool",    True),
    ("22a7834^", "_glassbox_rate",    APP,                     "_glassbox_rate",    True),
    ("f299f6f^", "_mcp_server_names", "src/litetui/plugins/settings_ui.py", "mcp_server_names", False),
    ("f299f6f^", "_start_mark",       "src/litetui/plugins/mark_plugin.py", "start_mark",       False),
]


def blob(ref: str, path: str) -> str:
    """The ONLY reader. There is deliberately no open()/read_text() in this file."""
    r = subprocess.run(["git", "show", f"{ref}:{path}"],
                       capture_output=True, text=True, encoding="utf-8", check=False)
    # check=False deliberately: a path that does not exist at that ref is a
    # legitimate outcome (the body had not been created yet), and it must surface
    # as a SKIP row rather than a traceback that stops the whole census.
    return r.stdout


def grab(src: str, name: str, in_class: bool) -> str | None:
    """Source text of a function, decorators included, dedented to its own margin.

    Dedenting matters: a class method sits 4 columns deeper than a module
    function, and comparing raw lines makes every line differ on the offset the
    move MUST introduce. That buries the signal in noise — a checker that flags
    everything is not a stricter checker, it is an unread one.
    """
    tree = ast.parse(src)
    scope = tree.body
    if in_class:
        cls = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LiteTUI"]
        if not cls:
            return None
        scope = cls[0].body
    found = [n for n in scope
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
    if not found:
        return None
    fn = found[0]
    lines = src.split("\n")
    start = (fn.decorator_list[0].lineno - 1) if fn.decorator_list else fn.lineno - 1
    return textwrap.dedent("\n".join(lines[start:fn.end_lineno]))


def comments(body: str) -> list[str]:
    """Standalone AND trailing comments, via the tokenizer.

    Both, because the loss that prompted this tool was a TRAILING one. And the
    tokenizer rather than a `#` scan, because `#` inside a string literal is not
    a comment — a naive count read 2 where the truth was 7.
    """
    # 🔴 NO `except: pass` HERE, AND THAT IS THE POINT. A swallowed failure
    # returns [], and the row then prints "0 -> 0" as though nothing was lost —
    # a FALSE CLEAN produced by the instrument's own failure, in the same format
    # as a real result. This tool exists because instruments lie quietly; it does
    # not get to do it too.
    #
    # ⚠️ MEASURED, NOT ASSUMED: `tokenize` is LOOSER than it looks. Fed an
    # unterminated string literal it returns tokens instead of raising, so this
    # function is NOT the gate. `ast.parse` in `docstring()` IS — it rejects
    # everything tokenize rejects and more — which is why the caller runs it
    # FIRST. An earlier version of this comment claimed the tokenizer caught it;
    # the negative control disagreed, and the control was right.
    return [tok.string.strip()
            for tok in tokenize.generate_tokens(io.StringIO(body).readline)
            if tok.type == tokenize.COMMENT]


def docstring(body: str) -> str:
    """Raises on unparseable input, for the reason given in `comments`."""
    return ast.get_docstring(ast.parse(body).body[0]) or ""


def norm(s: str) -> str:
    return " ".join(s.replace("#", " ").split()).lower()


def census(moves, new_ref: str) -> int:
    lost_total = 0
    for pre_ref, old, new_path, new, in_class in moves:
        before = grab(blob(pre_ref, APP), old, True)
        after = grab(blob(new_ref, new_path), new, in_class)
        label = f"{old} -> {new_path.rsplit('/', 1)[-1]}:{new}"
        if before is None or after is None:
            print(f"   SKIP {label}  (before={before is not None} after={after is not None})")
            continue
        try:
            # STRICT CHECK FIRST: ast.parse rejects more than tokenize does, so
            # this is the call that turns an unreadable body into a loud row.
            db, da = docstring(before), docstring(after)
            cb, ca = comments(before), comments(after)
        except (SyntaxError, tokenize.TokenError, IndentationError) as e:
            # LOUD, and counted as a problem. "I could not read it" must never
            # render as "nothing was lost".
            print(f" FAIL  {label:52} PARSE FAILED: {type(e).__name__}: {e}")
            lost_total += 1
            continue
        can = [norm(c) for c in ca]
        lost = [c for c in cb if norm(c) not in can]
        dan = norm(da)
        dlost = [ln.strip() for ln in db.split("\n") if ln.strip() and norm(ln) not in dan]
        lost_total += len(lost) + len(dlost)
        flag = "LOST" if (lost or dlost) else "  ok"
        print(f" {flag}  {label:52} comments {len(cb):>2} -> {len(ca):<2}  doc {len(db):>4}B -> {len(da):<4}B")
        for c in lost:
            print(f"          - {c}")
        for d in dlost:
            print(f"          - (doc) {d[:88]}")
    return lost_total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--move", action="append", default=[],
                    metavar="PRE_REF:OLD:NEW_PATH:NEW[:class]",
                    help="one moved body; repeatable. Omit to use the built-in T070 manifest.")
    ap.add_argument("--new-ref", default="HEAD",
                    help="ref holding the moved code (default HEAD). Never the working tree.")
    args = ap.parse_args()

    if args.move:
        moves = []
        for spec in args.move:
            parts = spec.split(":")
            if len(parts) not in (4, 5):
                print(f"bad --move {spec!r}: expected PRE_REF:OLD:NEW_PATH:NEW[:class]")
                return 2
            pre, old, path, new = parts[:4]
            moves.append((pre, old, path, new, len(parts) == 5 and parts[4] == "class"))
    else:
        moves = MOVES

    print(f"COMMENT / DOCSTRING CENSUS — {len(moves)} move(s), new side read from {args.new_ref}\n")
    total = census(moves, args.new_ref)
    print(f"\nTOTAL lost comment/doc lines: {total}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
