#!/usr/bin/env python
"""Pre-cost a refactor BEFORE anyone sits down: members, state, and the sweep.

    python tools/move_cost.py _cron_command _cron_add _cron_list
    python tools/move_cost.py --ref HEAD --class LiteTUI _inbox_monitor

WHY THIS EXISTS, in one line: T070 step O3 was pre-costed at 9 test sites in 3
files and the real sweep was 55 sites in 18. Every missed site was a reference to
`_jobs` -- THE STATE THE SERVICE EXISTED TO TAKE OWNERSHIP OF. Counting the
members costs almost nothing; the field is the scope.

It reports four things a member-only census cannot:

  1. WRITE CHANNELS, enumerated rather than pattern-matched from the last bug:
     attribute stores, SUBSCRIPT stores, stores through a borrowed object,
     MUTATING CALLS (.append/.remove/...), `del`, and setattr(obj, "name", ...).
     A `.append` is an ast.Call and is invisible to any assignment-only walker --
     that gap shipped a stateful function into a read-only module once already.

  2. THE STATE those writes name, and every reference to it across src/ AND
     tests/. Scanning one half of the repo produces a claim that is literally
     true and misleading: the qualifier ends up in the query's scope instead of
     in the claim.

  3. THE SWEEP SPLIT INTO **RED** vs **INERT**. After a field moves, a READ
     raises AttributeError and announces itself; an ASSIGNMENT succeeds, binds a
     name nothing reads, and the test keeps passing against empty state. The
     inert half is the entire risk and it is invisible to a green suite.

  4. FAKE CONSTRUCTIONS -- `SimpleNamespace(_jobs=[...])` and friends. Those are
     `ast.keyword`, not `ast.Attribute`, so an attribute walk skips them BY
     CONSTRUCTION, correctly and silently. The field moves on the real object and
     stays on every fake of it.

Every count prints its DENOMINATOR ("compared N of M"): a total without one reads
the same whether everything was examined or nothing was.

EXIT  0 nothing found · 1 sites found (there is a sweep to do) · 2 NO MEASUREMENT
      HAPPENED -- the query matched no member, the class was absent, a file
      failed to parse, OR argparse rejected the command line. Never a silent pass.

⚠️ `--roots` is nargs="*" and GREEDY: put the members FIRST or it swallows them
   and argparse exits 2. That is the same code as "nothing was examined", which
   is correct -- both mean no measurement happened -- but read the first line of
   output before treating a 2 as a finding about the code.
"""
from __future__ import annotations

import argparse
import ast
import pathlib
import subprocess
import sys
from collections import defaultdict

MUTATORS = {
    "append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse",
    "add", "discard", "update", "setdefault", "popitem", "write", "writelines",
}


def blob(ref: str, path: str) -> str | None:
    if ref == "WORKTREE":
        p = pathlib.Path(path)
        return p.read_text(encoding="utf-8") if p.exists() else None
    r = subprocess.run(["git", "show", f"{ref}:{path}"], capture_output=True)
    return None if r.returncode else r.stdout.decode("utf-8")


def tracked(ref: str, *roots: str) -> list[str]:
    if ref == "WORKTREE":
        out: list[str] = []
        for root in roots:
            out += [str(p).replace("\\", "/") for p in pathlib.Path(root).rglob("*.py")
                    if "__pycache__" not in p.parts]
        return sorted(out)
    r = subprocess.run(["git", "ls-tree", "-r", "--name-only", ref, *roots],
                       capture_output=True, check=True)
    return sorted(f for f in r.stdout.decode().split() if f.endswith(".py"))


def write_channels(fn: ast.AST, recv: str) -> list[tuple[int, str, str]]:
    """Every route by which `recv` can be CHANGED. Channels, not syntax."""
    out: list[tuple[int, str, str]] = []
    for n in ast.walk(fn):
        targets: list[ast.expr] = []
        if isinstance(n, ast.Assign):
            targets = list(n.targets)
        elif isinstance(n, (ast.AugAssign, ast.AnnAssign)):
            targets = [n.target]
        flat: list[ast.expr] = []
        for t in targets:
            flat += list(t.elts) if isinstance(t, ast.Tuple) else [t]
        for t in flat:
            if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) \
                    and t.value.id == recv:
                out.append((t.lineno, t.attr, "attribute store"))
            elif isinstance(t, ast.Subscript) and isinstance(t.value, ast.Attribute) \
                    and isinstance(t.value.value, ast.Name) and t.value.value.id == recv:
                out.append((t.lineno, t.value.attr, "SUBSCRIPT store"))
            elif isinstance(t, ast.Attribute) and isinstance(t.value, ast.Attribute) \
                    and isinstance(t.value.value, ast.Name) and t.value.value.id == recv:
                out.append((t.lineno, t.value.attr, "borrowed store"))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr in MUTATORS:
            base = n.func.value
            if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name) \
                    and base.value.id == recv:
                out.append((n.lineno, base.attr, f"MUTATING CALL .{n.func.attr}()"))
        if isinstance(n, ast.Delete):
            for t in n.targets:
                if isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name) \
                        and t.value.id == recv:
                    out.append((t.lineno, t.attr, "DELETE"))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                and n.func.id == "setattr" and len(n.args) > 1 \
                and isinstance(n.args[0], ast.Name) and n.args[0].id == recv \
                and isinstance(n.args[1], ast.Constant):
            out.append((n.lineno, n.args[1].value, "setattr(str)"))
    return sorted(set(out))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("members", nargs="+")
    ap.add_argument("--ref", default="WORKTREE",
                    help="git ref to read, or WORKTREE (default)")
    ap.add_argument("--cls", default="LiteTUI")
    ap.add_argument("--app-file", default="src/litetui/app.py")
    ap.add_argument("--roots", nargs="*", default=["src", "tests", "tools"])
    ap.add_argument("--receiver", default=None,
                    help="keep only sites whose receiver reads exactly this "
                         "(e.g. app). Read the BY RECEIVER grouping first.")
    args = ap.parse_args()

    src = blob(args.ref, args.app_file)
    if src is None:
        print(f"!! {args.app_file} unreadable at {args.ref}")
        return 2
    cls = next((n for n in ast.parse(src).body
                if isinstance(n, ast.ClassDef) and n.name == args.cls), None)
    if cls is None:
        print(f"!! class {args.cls} not found")
        return 2
    fns = {i.name: i for i in cls.body
           if isinstance(i, (ast.FunctionDef, ast.AsyncFunctionDef))}

    wanted = list(dict.fromkeys(args.members))
    found = [m for m in wanted if m in fns]
    missing = [m for m in wanted if m not in fns]

    print("=" * 78)
    print(f"1. WRITE CHANNELS   [{len(found)} of {len(wanted)} members found"
          + (f", MISSING {missing}" if missing else "") + "]")
    print("=" * 78)
    state: dict[str, list[str]] = defaultdict(list)
    for m in found:
        w = write_channels(fns[m], "self")
        print(f"  {m:<28} writes {len(w)}")
        for ln, attr, kind in w:
            print(f"       :{ln:<6} self.{attr:<26} {kind}")
            state[attr].append(m)
    if not found:
        print("  NOTHING WAS EXAMINED — the member query matched no method on the class.")
        return 2
    if not state:
        print("\n  No state is re-homed by this move: the sweep is the members only.")

    print("")
    print("=" * 78)
    print(f"2. THE STATE, AND EVERY REFERENCE TO IT   [{len(state)} field(s)]")
    print("=" * 78)
    files = tracked(args.ref, *args.roots)
    red: list[tuple[str, int, str, str]] = []
    inert: list[tuple[str, int, str, str]] = []
    fakes: list[tuple[str, int, str]] = []
    scanned = 0
    seen: set[str] = set()
    for f in files:
        text = blob(args.ref, f)
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            print(f"  !! PARSE FAILED {f} — counted as a problem, not skipped")
            return 2
        scanned += 1
        seen.add(f)
        for n in ast.walk(tree):
            if isinstance(n, ast.Attribute) and n.attr in state:
                row = (f, n.lineno, n.attr, ast.unparse(n.value))
                (inert if isinstance(n.ctx, ast.Store) else red).append(row)
            elif isinstance(n, ast.keyword) and n.arg in state:
                fakes.append((f, n.lineno, n.arg))

    # 🔴 A SCAN THAT DOES NOT PIN THE RECEIVER IS NOT A SWEEP. The first run of
    # this tool reported 67 sites where the hand count was 55: the extra 12 were
    # `self._jobs` inside an unrelated SCREEN class -- the same field NAME on a
    # different owner, which a move does not touch. The tool cannot know every
    # receiver, so it GROUPS BY RECEIVER and prints the grouping instead of
    # silently picking one. Filter with --receiver once you have looked.
    def split(rows):
        keep = [r for r in rows if args.receiver is None or r[3] == args.receiver]
        drop = [r for r in rows if args.receiver is not None and r[3] != args.receiver]
        return keep, drop

    def dump(label: str, rows, sep: str) -> None:
        by_recv: dict[str, int] = defaultdict(int)
        by_file: dict[str, int] = defaultdict(int)
        for f, _ln, _a, recv in rows:
            by_file[f] += 1
            by_recv[recv] += 1
        print(f"\n  {label}: {len(rows)} site(s) in {len(by_file)} file(s)")
        print(f"     BY RECEIVER: {dict(sorted(by_recv.items(), key=lambda kv: -kv[1]))}")
        for f, c in sorted(by_file.items(), key=lambda kv: -kv[1]):
            print(f"     {f:<48} {c}")
        for f, ln, attr, recv in rows[:12]:
            print(f"        {f}:{ln}  {recv}.{attr}{sep}")

    if args.receiver is not None:
        inert, dropped_i = split(inert)
        red, dropped_r = split(red)
        print(f"\n  [--receiver {args.receiver!r}: dropped "
              f"{len(dropped_i) + len(dropped_r)} site(s) on other receivers]")
    dump("🔴 INERT after the move (assignments — succeed, bind nothing, stay GREEN)",
         inert, " = ...")
    dump("RED after the move (reads — AttributeError, self-announcing)", red, "")
    print(f"\n  FAKE CONSTRUCTIONS (ast.keyword — an attribute walk skips these): "
          f"{len(fakes)} site(s)")
    for f, ln, arg in fakes[:12]:
        print(f"        {f}:{ln}  ...({arg}=...)")

    # ---- member-shaped sites -------------------------------------------
    # SilverBolt's find: a STUB CLASS that DEFINES the private name is a site,
    # and tracing state alone reaches the FILE without ever naming the DEF LINE.
    # After a rename the caller uses the public name and a stub class defining
    # only the private one raises -- RED, so it will not hide, but a pre-cost
    # exists to size the commit and 5 vs 7 sites is a different commit.
    member_sites: list[tuple[str, int, str, str]] = []
    for f in files:
        text = blob(args.ref, f)
        if text is None:
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            continue
        for n in ast.walk(tree):
            if isinstance(n, ast.ClassDef):
                for item in n.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                            and item.name in found and f != args.app_file:
                        member_sites.append((f, item.lineno, item.name,
                                             f"STUB-CLASS DEF in {n.name}"))
            elif isinstance(n, ast.Attribute) and n.attr in found:
                member_sites.append((f, n.lineno, n.attr,
                                     f"{ast.unparse(n.value)}.{n.attr}"))
            elif isinstance(n, ast.keyword) and n.arg in found:
                member_sites.append((f, n.lineno, n.arg, "keyword/fake"))
            elif isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                    and n.func.id == "setattr" and len(n.args) > 1 \
                    and isinstance(n.args[1], ast.Constant) and n.args[1].value in found:
                member_sites.append((f, n.lineno, n.args[1].value, "setattr(str)"))

    print("")
    print("=" * 78)
    print(f"2b. MEMBER-SHAPED SITES   [{len(member_sites)} site(s), "
          f"{len({m[0] for m in member_sites})} file(s)]")
    print("=" * 78)
    stubdefs = [m for m in member_sites if m[3].startswith("STUB-CLASS DEF")]
    print(f"  of which STUB-CLASS DEFS: {len(stubdefs)}  "
          "<- a state-only trace reaches the FILE and never the DEF LINE")
    for f, ln, name, how in stubdefs:
        print(f"        {f}:{ln}  def {name}(...)   {how}")
    for f, ln, name, how in [m for m in member_sites if m not in stubdefs][:12]:
        print(f"        {f}:{ln}  {how}")

    print("")
    print("=" * 78)
    print("3. THE PRE-COST")
    print("=" * 78)
    total = len(red) + len(inert) + len(fakes) + len(member_sites)
    allfiles = {r[0] for r in red} | {r[0] for r in inert} | {f for f, _, _ in fakes} | {m[0] for m in member_sites}
    print(f"  members moving        {len(found)}")
    print(f"  state re-homed        {len(state)}  {sorted(state)}")
    same = [r for r in red + inert if r[0] == args.app_file]
    print(f"  sweep                 {total} site(s) in {len(allfiles)} file(s)")
    print(f"     of which SAME-FILE {len(same)}   <- SUBSET of RED+INERT, in {args.app_file}: YOURS, same commit")
    print(f"     of which INERT     {len(inert)}   <- invisible to a green suite")
    print(f"     of which RED       {len(red)}")
    print(f"     of which FAKES     {len(fakes)}")
    print(f"     of which MEMBERS   {len(member_sites)}   <- incl. {len(stubdefs)} stub-class def(s)")
    skipped = [f for f in files if f not in seen]
    print(f"  [scanned {scanned} of {len(files)} tracked .py under {args.roots} at ref {args.ref}"
          + (f" | SKIPPED {skipped}]" if skipped else " | skipped none]"))
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
