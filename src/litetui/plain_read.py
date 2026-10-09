"""Is this tool call a PLAIN READ inside the seat's OWN FOLDER? (T0408-K)

The user, 8 October 2026, on the permission judge: "Build it, and let it allow plain
reads inside a seat's own folder". Nothing wider: plain reads, own folder.

This module is the WALL in front of that judge. The judge is a small model, and a small
model can be talked into "allow". So the design has to stay safe with a judge that
answers "allow" to everything, which means everything that makes a wrong "allow"
harmless is decided HERE, by a finite reader, before any model is asked. `assess`
answers one question about one tool call and returns either

    eligible      with the facts a judge would be shown: the verbs, the operands
                  resolved and written relative to the seat's folder, the whole command
    not eligible  with exactly one reason code from the closed set REASONS

Every doubt is "not eligible". An unknown tool, shell, argument, character, verb,
option or path form is never eligible: the reader holds ALLOWLISTS, so the next thing
nobody thought of is a refusal, not a hole. "Not eligible" changes nothing: the call
keeps today's route (the human is asked).

WHERE IT MAY BE CONSULTED (binding on any later insertion; nothing calls it today).
Only inside the CONFIRM branch of the policy decision: after the jobs-file ownership
refusal, after the deny floor, after the unknown-profile rejection, after a standing
human deny rule and after a capability the profile does not grant. It can never turn a
DENY into an ALLOW because it is never asked about one. And ON THE INTERACTIVE PROFILE
ONLY: a human who chose strict supervision asked to be asked, so strict never consults
this function (the policy owner's invariant 6; binding, not the inserter's choice). It
takes no profile itself, so it cannot be told one and widen for it.

WHAT THE CALLER MUST ALREADY KNOW. `shell` is a fact about the interpreter that will
run the text, not a guess from the tool's name: "bash" means a POSIX shell reads it,
"powershell" means PowerShell does. The two grammars disagree about quotes, commas and
braces, so a command proved under the wrong one is not proved. (LiteTUI's own bash tool
falls back to cmd.exe on a machine with no bash; there this function must not be asked.)

OWN FOLDER is `worktree_scope.own_roots` and nothing else: not the human's opt-in
scratch roots, not TEMP, not the card folder. Every operand is placed in each directory
the command may run in and resolved with real paths, so a symlink or junction that
leaves the folder is outside it. Refused whatever they resolve to: home, network,
drive-relative, provider and stream paths, and any path part named for another seat's
store or for git's internals (PROTECTED_PARTS).

GIT IS NEVER ELIGIBLE (reason SHARED_STORE; leader's ruling 646f2c8c on the policy
owner's invariant 5). `own_roots` only ever returns LINKED worktrees, and a linked
worktree's `.git` is a file pointing into the main checkout's `.git/worktrees/`, so
every git command a seat runs reads the shared repository outside every root this
module can be given. Two further facts, from knowledge of git and NOT measured here:
`git status` and `git diff` rewrite the index by default, and a git read can run a
program named in shared config or `.gitattributes` (`core.fsmonitor`, `gpg.program`
under `log.showSignature`, textconv and clean filters). No option on the reading
command proves those unset. The widening that was considered and rejected, git for
names-and-metadata forms only, needs the policy owner to rule that invariant 5 does
not cover git's own reads, and the user's word, because it is wider than his ruling.

PLAIN READ is a command in which every pipeline stage is a verb on the short lists
below, every option is on that verb's own list, and every operand is a literal path.
Verbs that read CONTENT take existing regular files with one link, named literally, and
never a name on the secrets list. Verbs that LIST take one directory level: recursion
is refused because it crosses links the reader never resolved. The only redirect is
the word `2>&1`.

THE WORD READ HERE MUST BE THE WORD THE PROGRAM GETS. The reviews of the first two
candidates (39c010a, b92e652) kept finding one weak step: this process and the program
that runs resolving one word differently. Each is now a refusal, not a ceiling:
  * rg must carry `--no-config`, alone or after a pipe: the tools hand the child
    RIPGREP_CONFIG_PATH, and a config file can add an option that runs a program;
  * no PowerShell verb is a program: PowerShell rebuilds a program's command line and
    a quote inside a pattern can become extra arguments;
  * a pattern handed to a program is refused when it is empty, holds a double quote or
    ends in a backslash;
  * a path with a `..` part, a path rooted with no drive letter on Windows, a part
    ending in a dot or a space, and a Windows device name are refused;
  * an object filter may name only the properties in _PROPERTIES, and a JSON dump may
    not follow a listing: PowerShell computes other properties by opening the file;
  * ConvertTo-Json is eligible only after a ConvertFrom-Json stage in the same
    pipeline: the lines Get-Content emits carry the drive and provider members a
    file object does, so a dump of them prints machine metadata;
  * on Windows, an operand is refused when it, or any existing part of it below the
    root, carries the SYSTEM attribute, is a reparse point that real-path resolution
    left unresolved, or has a name ending in `.lnk` (see `_runtime_link`).

IT ONLY READS, AND IT ALWAYS ANSWERS. It resolves paths, tests that a content operand
is a regular file with a single link, and reads the `.git` markers `own_roots` reads.
It starts no process, opens no file's contents, asks no model and touches no network.
The one process-state read is the current directory, because that is where LiteTUI's
shell tools run. It never raises: any failure inside is "not eligible", reason FAULT.

CEILINGS (stated, not solved; a pure function cannot settle them):
  * the check is taken before the command runs: a path that is a plain file now can be
    a link by the time it is opened;
  * the secret-name list is a courtesy and not a boundary. It misses `config.json`,
    `settings.json`, `*.tfstate`, `*.kdbx`, `.pgpass` and database files, and a secret
    in a file with an innocent name is read like any other file. On the interactive
    profile today's policy already allows `cat .env` with no question, so the list
    refuses more than today, never less;
  * which program a verb's NAME starts is the machine's business (PATH, a shell alias):
    this proves what the text asks for, not what is installed;
  * an option channel through the ENVIRONMENT is closed for rg (`--no-config`) and
    cannot be closed in the command's own words for GNU grep before 3.6, which reads
    GREP_OPTIONS (it can add `-f FILE`, a second file read as patterns; it cannot run
    a program), or for bash itself, which sources the file BASH_ENV names before any
    command. The same channels exist for every bash command that runs today without
    a prompt, so the wall adds nothing there;
  * NOTHING here was ever run on Linux or macOS. The rules for `/` paths, `..` and
    program arguments are reasoned for those systems from their documented behaviour.
"""
from __future__ import annotations

import fnmatch
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

# The tokenizer and the scope check are worktree_scope's, private names included, so
# there is ONE shell reader and ONE definition of "inside the tree" in this codebase.
# tests/test_plain_read.py pins the exact behaviour relied on, so a change there fails
# here in the open.
from litetui import worktree_scope

BASH = "bash"
POWERSHELL = "powershell"

#: The relay shows a human the first 2,048 characters of a command; a judge must never
#: be shown less than the whole.
MAX_COMMAND_CHARS = 2000

# ── the closed set of reasons ────────────────────────────────────────────────
TOOL = "tool"                        # not one of the two shell tools
SHELL = "shell"                      # shell kind unknown, or not the tool's own
ARGUMENTS = "arguments"              # no command text, or an argument this reader does not know
EMPTY = "empty"                      # nothing to run
TOO_LONG = "too-long"                # over MAX_COMMAND_CHARS
CHARSET = "charset"                  # a character outside printable ASCII
URL = "url"                          # a literal containing ://
SUBSTITUTION = "substitution"        # $, backtick, ( ), %
REDIRECT = "redirect"                # any < or > that is not the word 2>&1
GLOB = "glob"                        # * ? [ ]
SYNTAX = "syntax"                    # any other construct this reader does not follow
VERB = "verb"                        # a stage whose verb is not on the list
SHARED_STORE = "shared-store"        # git: reads the shared repository, not the folder
OPTION = "option"                    # an option not on that verb's list
OPERAND = "operand"                  # operands this verb's form does not allow
PIPELINE = "pipeline"                # a stage that would read paths from its input
PATH_FORM = "path-form"              # home, network, drive-relative, provider, stream, odd characters
NO_OWN_FOLDER = "no-own-folder"      # the seat owns no worktree
OUTSIDE = "outside-own-folder"       # resolves outside the folder, links included
PROTECTED = "protected-path"         # a part named for another seat's store or git internals
SECRET = "secret-name"               # a content read of a name on the secrets list
NOT_PLAIN_FILE = "not-a-plain-file"  # a content operand that is missing, a directory or multiply linked
CWD = "cwd"                          # the directories it may run in disagree about an operand
FAULT = "fault"                      # the reader itself failed: nothing was proved

REASONS = frozenset({
    TOOL, SHELL, ARGUMENTS, EMPTY, TOO_LONG, CHARSET, URL, SUBSTITUTION, REDIRECT, GLOB,
    SYNTAX, VERB, SHARED_STORE, OPTION, OPERAND, PIPELINE, PATH_FORM, NO_OWN_FOLDER,
    OUTSIDE, PROTECTED, SECRET, NOT_PLAIN_FILE, CWD, FAULT,
})

#: Tool name (lower case) to the shell that reads its command. `Bash` is the Claude
#: backend's native tool; it reaches the same policy door as LiteTUI's own `bash`.
SHELL_TOOLS = {"bash": BASH, "powershell": POWERSHELL}
#: Arguments the two shell tools define. Anything else (an MCP shell's `cwd`, an `env`)
#: could change where or how the text runs, so it is not eligible.
ARGUMENT_KEYS = frozenset({"command", "timeout", "background", "run_in_background", "description"})

#: Path parts that are never inside "own folder", wherever they sit: other seats'
#: stores, the harness's own state, and git's internals (in a linked worktree `.git` is
#: a pointer to the shared repository).
PROTECTED_PARTS = frozenset({".agents", ".convos", ".liteharness", ".litesuite", ".claude",
                             ".codex", ".git"})
#: Names a CONTENT verb never reads, matched on every part below the folder's root, as
#: written and as resolved. A name is all this can see: see the ceilings above.
SECRET_NAMES = (".env*", "*.pem", "*.key", "*.pfx", "*.p12", "*.jks", "*.keystore", "id_rsa*",
                "id_ed25519*", "id_ecdsa*", "id_dsa*", ".npmrc", ".pypirc", ".netrc", "_netrc",
                ".git-credentials", ".ssh", ".aws", ".gnupg", "*secret*", "*token*",
                "*credential*", "*password*")
#: `git` is refused by name so the refusal says why (see the module docstring).
SHARED_STORE_VERBS = frozenset({"git"})


@dataclass(frozen=True)
class PlainRead:
    """The answer. When `eligible`, the other fields are what a judge is shown."""

    eligible: bool
    reason: str = ""                   # one of REASONS when not eligible, else ""
    verbs: tuple[str, ...] = ()        # one per pipeline stage, in order
    operands: tuple[str, ...] = ()     # resolved, relative to the folder's root, `/` separated
    command: str = ""                  # the whole command, never cut


class _No(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


# ── the verb lists ───────────────────────────────────────────────────────────
#
# A verb is described by what it MAY be given. An option that is not written down here
# refuses, which is how every flag that writes, runs, recurses or follows is refused:
# `rg --pre`, `file -C`, `tail -f`, `sha256sum -c`, `grep -r`, `ls -R`,
# `Get-Content -Stream`, `Get-ChildItem -Recurse`.

CONTENT, LISTING, FILTER = "content", "listing", "filter"
#: Value kinds. Besides these three: a frozenset is one word from a closed set, a
#: `_Names` is a comma list of property names from a closed set, a range is a bounded
#: whole number.
INT, TEXT, PATH = "int", "text", "path"


class _Names(frozenset):
    """Property names an object filter may be given."""


def _w(words: str) -> frozenset[str]:
    return frozenset(words.split())


@dataclass(frozen=True)
class _Spec:
    name: str                                    # the verb as a judge is shown it
    kind: str                                    # CONTENT, LISTING or FILTER
    flags: frozenset[str] = frozenset()          # options that take no value
    cluster: str = ""                            # native: letters that may share one dash
    valued: Mapping[str, object] = field(default_factory=dict)   # option -> value kind
    positional: tuple[str, ...] = (PATH,)        # what the bare words are, in order
    files: tuple[int, int | None] = (1, None)    # how many path operands, least and most
    stdin: bool = False                          # reads its input when it names no file
    numeric: bool = False                        # `-5` means five lines
    cmdlet: bool = False                         # PowerShell parameter grammar
    needs: str = ""                              # an option that must be written out


_COLOR = _w("never always auto")
_CONTEXT = {"-A": INT, "-B": INT, "-C": INT, "-m": INT, "--after-context": INT,
            "--before-context": INT, "--context": INT, "--max-count": INT}
_LINES = {"-n": INT, "-c": INT, "--lines": INT, "--bytes": INT}

_CAT = _Spec("cat", CONTENT, cluster="AbeEnstTv", stdin=True, flags=_w(
    "--number --number-nonblank --squeeze-blank --show-all --show-ends --show-tabs"
    " --show-nonprinting"))
_HEAD = _Spec("head", CONTENT, cluster="qv", valued=_LINES, stdin=True, numeric=True,
              flags=_w("--quiet --silent --verbose"))
_TAIL = _Spec("tail", CONTENT, cluster="qv", valued=_LINES, stdin=True, numeric=True,
              flags=_w("--quiet --silent --verbose"))
_LS = _Spec("ls", LISTING, cluster="1AaBbCcdFGghiklmnopQqrSstUuvXx", files=(0, None),
            valued={"--color": _COLOR}, flags=_w(
    "--all --almost-all --human-readable --si --size --inode --reverse --directory --classify"
    " --numeric-uid-gid --no-group --group-directories-first --full-time --color"))
_WC = _Spec("wc", CONTENT, cluster="cLlmw", stdin=True, flags=_w(
    "--bytes --chars --lines --words --max-line-length"))
_GREP = dict(kind=CONTENT, cluster="abcEFGHhIiLlnoPqsvwx", positional=(TEXT, PATH), stdin=True,
             valued={**_CONTEXT, "-e": TEXT, "--regexp": TEXT, "--color": _COLOR,
                     "--colour": _COLOR},
             flags=_w("--extended-regexp --fixed-strings --basic-regexp --perl-regexp"
                      " --ignore-case --no-ignore-case --word-regexp --line-regexp --count"
                      " --line-number --with-filename --no-filename --only-matching --quiet"
                      " --silent --invert-match --files-with-matches --files-without-match"
                      " --text --no-messages --byte-offset --color --colour"))
#: rg reads a config file named by RIPGREP_CONFIG_PATH, which the shell tools hand the
#: child unchanged, and a config file can add `--pre` (RUNS a program on every file) or
#: name other files. So every eligible rg stage, alone or after a pipe, must carry
#: `--no-config` in its own words (policy owner's R1 on 39c010a): a bare rg is not eligible.
_RG = _Spec("rg", CONTENT, cluster="acFHIilNnoSsUvwx", positional=(TEXT, PATH), stdin=True,
            needs="--no-config",
            valued={**_CONTEXT, "-e": TEXT, "--regexp": TEXT,
                    "--color": _COLOR | {"ansi"}},
            flags=_w("--fixed-strings --ignore-case --smart-case --case-sensitive --line-number"
                     " --no-line-number --count --files-with-matches --only-matching"
                     " --invert-match --word-regexp --line-regexp --with-filename"
                     " --no-filename --heading --no-heading --text --multiline --no-config"))
_STAT = _Spec("stat", LISTING, cluster="t", valued={"-c": TEXT, "--format": TEXT},
              flags=_w("--terse"))
_FILE = _Spec("file", CONTENT, cluster="bi", flags=_w(
    "--brief --mime --mime-type --mime-encoding"))
_SHA256 = _Spec("sha256sum", CONTENT, cluster="bt", stdin=True, flags=_w("--binary --text --tag"))
_CMP = _Spec("cmp", CONTENT, cluster="bls", files=(2, 2), flags=_w(
    "--print-bytes --verbose --silent --quiet"))
_DIFF = _Spec("diff", CONTENT, cluster="aBbiqsuwy", files=(2, 2),
              valued={"-U": INT, "--unified": INT, "--color": _COLOR},
              flags=_w("--text --ignore-blank-lines --ignore-space-change --ignore-case --brief"
                       " --report-identical-files --unified --ignore-all-space --side-by-side"
                       " --color"))

_BASH_VERBS: dict[str, _Spec] = {
    "cat": _CAT, "head": _HEAD, "tail": _TAIL, "ls": _LS, "wc": _WC,
    "grep": _Spec("grep", **_GREP), "egrep": _Spec("egrep", **_GREP),
    "fgrep": _Spec("fgrep", **_GREP), "rg": _RG, "stat": _STAT, "file": _FILE,
    "sha256sum": _SHA256, "cmp": _CMP, "diff": _DIFF,
}

_ERRORS = _w("silentlycontinue stop continue ignore")
_ENCODINGS = _w("utf8 utf8bom utf8nobom ascii unicode utf32 bigendianunicode oem default")
_PS_PATH = {"-path": PATH, "-literalpath": PATH, "-erroraction": _ERRORS}


def _cmdlet(name: str, kind: str, flags: str = "", valued: Mapping[str, object] | None = None,
            positional: tuple[str, ...] = (PATH,), files: tuple[int, int | None] = (1, 1),
            stdin: bool = False) -> _Spec:
    return _Spec(name, kind, flags=_w(flags), valued=dict(valued or {}), positional=positional,
                 files=files, stdin=stdin, cmdlet=True)


_GET_CONTENT = _cmdlet("get-content", CONTENT, "-raw", {
    **_PS_PATH, "-totalcount": INT, "-head": INT, "-first": INT, "-tail": INT, "-last": INT,
    "-encoding": _ENCODINGS})
_GET_CHILDITEM = _cmdlet("get-childitem", LISTING, "-force -name -file -directory -hidden",
                         _PS_PATH, files=(0, 1))
_GET_ITEM = _cmdlet("get-item", LISTING, "-force", _PS_PATH)
_SELECT_STRING = _cmdlet(
    "select-string", CONTENT,
    "-simplematch -casesensitive -quiet -list -notmatch -allmatches -raw -noemphasis",
    {**_PS_PATH, "-pattern": TEXT, "-context": INT, "-encoding": _ENCODINGS},
    positional=(TEXT, PATH), stdin=True)
#: The ONLY properties an object filter may name (policy owner's review of 39c010a).
#: A filter after a listing is handed file objects, and PowerShell computes some of a
#: file's properties by opening it or by following a link this module never resolved.
#: Each name here is the entry's own stored data; the basis is knowledge of .NET's
#: FileSystemInfo, not a run:
#:   name, extension             text held in the object: no disk access at all
#:   length, lastwritetime,      the directory entry's own record (for a link: the link's,
#:   creationtime                not its target's); read without opening the file
#: Deliberately NOT here, so they refuse: `mode` (some PowerShell versions open the
#: file to count its hard links), `versioninfo` (reads the file's resources), `target`
#: and `linktarget` (read the link), `directory` and `fullname` (step out to the parent
#: folders), and every name nobody has shown to be inert.
_PROPERTIES = _Names({"name", "length", "extension", "lastwritetime", "creationtime"})
#: `count` is the engine's own member on any object (1 for a single item); no disk.
_MEASURED = _Names(_PROPERTIES | {"count"})

_SELECT_OBJECT = _cmdlet("select-object", FILTER, "-unique", {
    "-first": INT, "-last": INT, "-skip": INT, "-property": _PROPERTIES},
    positional=(_PROPERTIES,), files=(0, 0))
_SORT_OBJECT = _cmdlet("sort-object", FILTER, "-descending -unique -casesensitive",
                       {"-property": _PROPERTIES}, positional=(_PROPERTIES,), files=(0, 0))
_MEASURE_OBJECT = _cmdlet("measure-object", FILTER,
                          "-line -word -character -sum -average -maximum -minimum",
                          {"-property": _MEASURED}, positional=(_MEASURED,), files=(0, 0))

#: Only the aliases that mean the cmdlet on EVERY platform. `cat`, `type`, `ls`, `dir`
#: and `sort` are aliases on Windows and the GNU programs under PowerShell for Unix,
#: where `sort Name` reads a FILE called Name: one spelling, two commands, so neither.
#:
#: EVERY verb here is a cmdlet (review W1 on 39c010a). PowerShell builds a native
#: program's command line without escaping a double quote inside an argument and drops
#: an empty one, so `rg 'x" "--pre=calc' a.txt` reaches rg as three words, one of which
#: runs a program. A cmdlet's arguments never cross a command line. rg is bash's only.
_POWERSHELL_VERBS: dict[str, _Spec] = {
    "get-content": _GET_CONTENT, "gc": _GET_CONTENT,
    "get-childitem": _GET_CHILDITEM, "gci": _GET_CHILDITEM,
    "get-item": _GET_ITEM, "gi": _GET_ITEM,
    "test-path": _cmdlet("test-path", LISTING, "", {
        **_PS_PATH, "-pathtype": _w("leaf container any")}),
    "get-filehash": _cmdlet("get-filehash", CONTENT, "", {
        **_PS_PATH, "-algorithm": _w("sha1 sha256 sha384 sha512 md5")}),
    "select-string": _SELECT_STRING, "sls": _SELECT_STRING,
    "select-object": _SELECT_OBJECT, "select": _SELECT_OBJECT,
    "sort-object": _SORT_OBJECT,
    "measure-object": _MEASURE_OBJECT, "measure": _MEASURE_OBJECT,
    # depth 0 to 4: deeper walks out of the object into whatever it points at
    "convertto-json": _cmdlet("convertto-json", FILTER, "-compress", {"-depth": range(5)},
                              positional=(), files=(0, 0)),
    "convertfrom-json": _cmdlet("convertfrom-json", FILTER, positional=(), files=(0, 0)),
}

VERBS: dict[str, dict[str, _Spec]] = {BASH: _BASH_VERBS, POWERSHELL: _POWERSHELL_VERBS}
#: Cmdlets whose output is FILE OBJECTS, and the two filters that may not follow them in
#: a pipeline: `Select-String` given a file object searches that file's contents, and
#: `ConvertTo-Json` reads EVERY property of it, the ones _PROPERTIES leaves out included.
_EMITS_FILES = frozenset({"get-childitem", "get-item"})
_READS_FILE_OBJECTS = frozenset({"select-string", "convertto-json"})
#: Names Windows gives to devices, with or without an extension, in any folder.
_DEVICES = frozenset({"CON", "PRN", "AUX", "NUL"} | {f"{port}{n}" for port in ("COM", "LPT")
                                                    for n in range(10)})

# ── reading the text ─────────────────────────────────────────────────────────

_PRINTABLE = frozenset(map(chr, range(0x20, 0x7F)))
_WORD_END = " \t\r\n;|&<>"
_JOINS = ("|", "&&", "||")
_PATH_TEXT = re.compile(r"[A-Za-z0-9_.+@~ /\\:-]+")
_DRIVE = re.compile(r"[A-Za-z]:[\\/]")


def _guard(text: str, shell: str) -> str:
    """Refuse what `worktree_scope._tokenize` cannot be trusted with, and hand back the
    text with each `2>&1` word blanked.

    The tokenizer was written to judge PATHS: it drops a redirect silently (`cat a > b`
    reads as three words), and it knows nothing of braces, wildcards, comments or the
    places where PowerShell and bash split words differently. This pass uses the
    tokenizer's own quote rule (a quote runs to the next same quote; no escapes), so
    both readers agree on what is inside a quote, and refuses everything in between.
    """
    powershell = shell == POWERSHELL
    out = list(text)
    quote = ""
    fresh = True                      # no character of the current word seen yet
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
                # PowerShell: `'a'b` is TWO arguments and `'a''b'` is one with a quote
                # in it; the tokenizer reads both as the single word `ab`.
                if powershell and i + 1 < n and text[i + 1] not in _WORD_END:
                    raise _No(SYNTAX)
            elif quote == '"' and ch in "$`":
                raise _No(SUBSTITUTION)
            elif quote == '"' and ch == "\\" and not powershell:
                raise _No(SYNTAX)     # bash: \" keeps the quote open; the tokenizer closes it
            i += 1
            continue
        if ch in " \t\r\n;|&":
            fresh = True
            i += 1
            continue
        if ch in "'\"":
            if powershell and not fresh:
                raise _No(SYNTAX)
            quote = ch
        elif ch in "$`()%":
            raise _No(SUBSTITUTION)
        elif (ch == "2" and fresh and text.startswith("2>&1", i)
              and (i + 4 == n or text[i + 4] in _WORD_END)):
            out[i:i + 4] = "    "     # the one redirect a plain read may carry
            i += 4
            continue
        elif ch in "<>":
            raise _No(REDIRECT)
        elif ch in "*?[]":
            raise _No(GLOB)
        elif ch in "{}" or (ch == "\\" and not powershell):
            raise _No(SYNTAX)         # brace expansion, a script block, a bash escape
        elif fresh and (ch == "#" or (ch == "@" and powershell)):
            raise _No(SYNTAX)         # a comment; a splat, array or hash literal
        fresh = False
        i += 1
    if quote:
        raise _No(SYNTAX)
    return "".join(out)


def _value(kind: object, value: str, quoted: bool, cmdlet: bool) -> None:
    """One option value or bare word that is not a path."""
    if kind == INT:
        ok = re.fullmatch(r"[+-]?\d+", value) is not None
    elif kind == TEXT:
        # A pattern or a format is data, whatever it SAYS. But a program's words are
        # rebuilt into one Windows command line, and three shapes do not survive that
        # everywhere: an empty word can be dropped, a double quote can split the word,
        # a final backslash can swallow the quote after it (review W1; from knowledge
        # of command-line building, not from a run). A cmdlet's values never cross one.
        ok = cmdlet or not (value == "" or '"' in value or value.endswith("\\"))
    elif isinstance(kind, range):
        ok = value.isdigit() and int(value) in kind
    elif isinstance(kind, _Names):
        ok = not (quoted and "," in value) and all(
            name in kind for name in value.lower().split(","))
    else:
        ok = (value.lower() if cmdlet else value) in kind
    if not ok:
        raise _No(OPTION)


def _native_words(spec: _Spec, tokens: list[tuple[str, bool]]) -> list[str]:
    """The path operands of a program's argument list (GNU option grammar). Programs
    are bash's only: no PowerShell verb is one."""
    files: list[str] = []
    options = True
    begun = False                     # a pattern or file has been read
    needed = bool(spec.needs)         # the option that must be written has not been seen
    pattern_due = spec.positional[0] == TEXT
    i = 0
    while i < len(tokens):
        word, quoted = tokens[i]
        i += 1
        if options and word == "--":
            options = False
        elif options and word.startswith("-") and word != "-":
            if begun:
                # GNU parsers move a late option forward; parsers that keep POSIX order
                # (BSD, POSIXLY_CORRECT) read it and every word after it as FILES.
                raise _No(OPTION)
            name, equals, value = word.partition("=")
            if not equals and (word in spec.flags
                               or (spec.cluster and re.fullmatch(f"-[{spec.cluster}]+", word))
                               or (spec.numeric and re.fullmatch(r"-\d+", word))):
                needed = needed and word != spec.needs
                continue
            kind = spec.valued.get(name)
            if kind is None or (equals and not name.startswith("--")):
                raise _No(OPTION)
            if not equals:
                if i == len(tokens):
                    raise _No(OPTION)
                value, quoted = tokens[i]
                i += 1
            _value(kind, value, quoted, cmdlet=False)
            if name in ("-e", "--regexp"):
                pattern_due = False
        elif word == "-":
            raise _No(OPERAND)        # standard input by name
        elif pattern_due:
            _value(TEXT, word, quoted, cmdlet=False)
            pattern_due, begun = False, True
        else:
            files.append(word)
            begun = True
    if pattern_due:
        raise _No(OPERAND)
    if needed:
        raise _No(OPTION)             # rg without `--no-config` written out
    return files


def _cmdlet_words(spec: _Spec, tokens: list[tuple[str, bool]]) -> list[str]:
    """The path operands of a cmdlet's argument list. Parameter names are matched
    whole: PowerShell accepts any unambiguous prefix (`-Str` is `-Stream`), and a
    reader that guessed at prefixes would be guessing at what binds."""
    files: list[str] = []
    slots = list(spec.positional)
    i = 0
    while i < len(tokens):
        word, quoted = tokens[i]
        i += 1
        if word.startswith("-"):
            if quoted:
                raise _No(SYNTAX)     # a quoted `-x` is a value, in a position we would misread
            name = word.lower()
            if name in spec.flags:
                continue
            kind = spec.valued.get(name)
            if kind is None or i == len(tokens):
                raise _No(OPTION)
            word, quoted = tokens[i]
            i += 1
            if word.startswith("-") and not quoted and kind != INT:
                raise _No(OPTION)
        elif slots:
            kind = slots[0]
        else:
            raise _No(OPERAND)
        if kind in slots:
            slots.remove(kind)        # bound, by name or by position
        if kind == PATH:
            files.append(word)
        else:
            _value(kind, word, quoted, cmdlet=True)
    if TEXT in slots:
        raise _No(OPERAND)            # a search with no pattern
    return files


def _path_form(written: str, shell: str) -> None:
    """Refuse a path operand whose TEXT is not a plain literal path, before any disk is
    consulted."""
    if not written:
        raise _No(OPERAND)
    if re.search(r"[*?\[\]]", written):
        raise _No(GLOB)               # PowerShell expands these even inside quotes
    windows = os.name == "nt"         # a drive letter and `\` mean a path only there
    rest = written[2:] if windows and _DRIVE.match(written) else written
    if (not _PATH_TEXT.fullmatch(written) or written.startswith(("~", "\\\\", "//"))
            or ":" in rest or ("\\" in written and (shell == BASH or not windows))):
        raise _No(PATH_FORM)
    # The three rules below are the reviews' W2, W3 and W5 on 39c010a. Each refuses a
    # word that this process and the program that runs would resolve DIFFERENTLY.
    if windows and written[0] in "/\\":
        # rooted with no drive letter: Python places it on this process's drive, Git
        # Bash under its own install folder. The form Git Bash uses (`/c/...`) too.
        raise _No(PATH_FORM)
    for part in re.split(r"[\\/]+", written):
        if part in ("", "."):
            continue
        # `..`: this reader removes `link/..` as text; a kernel walks the link first, so
        # `..` is the parent of the link's TARGET. A final dot or space: Windows drops it,
        # so the name a judge is shown is not the name written. A device name (`nul`,
        # `con`, `aux.txt`) is not a file in any folder.
        if (part == ".." or part[-1] in " ."
                or part.split(".")[0].upper() in _DEVICES):
            raise _No(PATH_FORM)


def _plan(text: str, shell: str) -> tuple[list[str], list[tuple[str, bool]]]:
    """(verbs, [(path operand as written, is a content read)]) for a command whose TEXT
    is a plain read, else _No. No disk is consulted here."""
    allowed = _PRINTABLE | set("\n\t") | ({"\r"} if shell == POWERSHELL else set())
    if any(ch not in allowed for ch in text):
        raise _No(CHARSET)            # PowerShell reads typographic quotes and dashes as ASCII ones
    if "://" in text:
        raise _No(URL)
    try:
        segments = worktree_scope._tokenize(_guard(text, shell))
    except worktree_scope._Refuse:
        raise _No(SYNTAX) from None
    verbs: list[str] = []
    operands: list[tuple[str, bool]] = []
    before = ""
    stage = 0
    lead = ""
    earlier: list[str] = []
    for tokens, separator in segments:
        if separator == "&":
            raise _No(SYNTAX)         # a background job, or PowerShell's call operator
        if not tokens:
            if separator in _JOINS or before in _JOINS:
                raise _No(SYNTAX)
            before = separator
            continue
        stage = stage + 1 if before == "|" else 0
        before = separator
        word, quoted = tokens[0]
        key = word.lower() if shell == POWERSHELL else word
        if key in SHARED_STORE_VERBS and not quoted:
            raise _No(SHARED_STORE)
        spec = None if quoted else VERBS[shell].get(key)
        if spec is None:
            raise _No(VERB)
        files = (_cmdlet_words if spec.cmdlet else _native_words)(spec, tokens[1:])
        if stage == 0:
            lead = spec.name
            earlier = []              # the verbs before this one in the same pipeline
        least, most = spec.files
        counted = least <= len(files) and (most is None or len(files) <= most)
        if shell == POWERSHELL:
            # A cmdlet that takes paths also takes them FROM THE PIPELINE:
            # `Get-ChildItem | Get-Content` reads every file in the directory.
            if stage == 0:
                if spec.kind == FILTER:
                    raise _No(PIPELINE)
            elif (files or not (spec.kind == FILTER or spec.stdin)
                  or (spec.name in _READS_FILE_OBJECTS and lead in _EMITS_FILES)
                  or (spec.name == "convertto-json" and "convertfrom-json" not in earlier)):
                raise _No(PIPELINE)
        earlier.append(spec.name)
        if not counted and not (stage and spec.stdin and not files):
            raise _No(OPERAND)
        if spec.kind == LISTING and not files:
            files = ["."]             # it lists the directory it runs in
        for written in files:
            _path_form(written, shell)
            operands.append((written, spec.kind == CONTENT))
        verbs.append(spec.name)
    if not verbs:
        raise _No(EMPTY)
    return verbs, operands


# ── placing the operands ─────────────────────────────────────────────────────


def _parts(path: str) -> set[str]:
    """Path parts as Windows compares them: case-blind, trailing dots and spaces dropped
    (`.claude.` and `.CLAUDE` name `.claude`)."""
    return {part.rstrip(" .").lower() for part in re.split(r"[\\/]+", path)
            if part not in ("", ".", "..")}


_SYSTEM_OR_REPARSE = 0x4 | 0x400     # FILE_ATTRIBUTE_SYSTEM, FILE_ATTRIBUTE_REPARSE_POINT


def _runtime_link(real: str, real_root: str) -> bool:
    """Could Git Bash's runtime open this name as a LINK that Python sees as a plain file?
    (review round 2, S1, on b92e652; Windows only.)

    Git Bash's programs run on a Cygwin-derived runtime. The reviewer knows this from
    the runtime's documented link forms and has NOT seen Git for Windows do it; nobody
    has tried it in a shell, and whether anyone looks is the user's decision. By those
    documented forms the runtime follows, as a symbolic link, a file that carries the
    SYSTEM attribute and begins with its link cookie, a `.lnk` shortcut of its own
    making (it hides the suffix, so the name `gone` opens `gone.lnk`), and a link kind
    Windows itself will not follow, which real-path resolution leaves in place as an
    ordinary name. So the wall would prove the file and `cat` would open the link's
    target, wherever that is. Each form is refused for the operand and for every
    existing part of it below the root. Only stat calls: nothing is opened."""
    path = real_root
    for part in os.path.relpath(real, real_root).split(os.sep):
        if part == os.curdir:
            continue
        path = os.path.join(path, part)
        if part.lower().endswith(".lnk"):
            return True
        try:
            attributes = os.lstat(path).st_file_attributes
        except FileNotFoundError:
            if os.path.lexists(path + ".lnk"):
                return True
            continue
        except OSError:
            return True               # a name that cannot be examined is not proved
        if attributes & _SYSTEM_OR_REPARSE:
            return True
    return False


def _locate(operands: list[tuple[str, bool]], root: Path, bases: list[Path]) -> list[str]:
    """Each operand resolved and written relative to `root`, or _No. An operand is
    proved in EVERY directory the command may run in, and must be the same file in all."""
    real_root = os.path.realpath(str(root))
    shown: list[str] = []
    for written, content in operands:
        found: set[str] = set()
        relative = ""
        for base in bases:
            scope = worktree_scope._Scope(root, base, deleting=False)
            try:
                scope.check_value(written)
            except worktree_scope._Refuse:
                raise _No(OUTSIDE) from None
            placed = scope._place(written)
            real = os.path.realpath(placed)
            relative = os.path.relpath(real, real_root)
            parts = _parts(relative)
            lexical = os.path.normcase(os.path.normpath(placed))
            if worktree_scope._inside(lexical, scope.root):
                parts |= _parts(lexical[len(scope.root):])
            if parts & PROTECTED_PARTS:
                raise _No(PROTECTED)
            if os.name == "nt" and _runtime_link(real, real_root):
                raise _No(NOT_PLAIN_FILE)
            if content:
                if any(fnmatch.fnmatchcase(part, name) for part in parts for name in SECRET_NAMES):
                    raise _No(SECRET)
                try:
                    plain = os.path.isfile(real) and os.stat(real).st_nlink == 1
                except OSError:
                    plain = False
                if not plain:
                    raise _No(NOT_PLAIN_FILE)
            found.add(os.path.normcase(real))
        if len(found) != 1:
            raise _No(CWD)
        relative = relative.replace("\\", "/")
        if relative not in shown:
            shown.append(relative)
    return shown


def assess(tool_name: str, args: Mapping[str, object] | None, shell: str | None,
           workspace: Path, seat_name: str | None, *, cwd: Path | None = None) -> PlainRead:
    """Is this call a plain read inside the seat's own folder?

    `shell` is the interpreter that will read the command ("bash" or "powershell");
    `workspace` and `seat_name` are what `worktree_scope.own_roots` takes. `cwd` is the
    directory the tool process runs in when the caller knows it; left out, it is this
    process's current directory, which is where LiteTUI's shell tools run.

    It ANSWERS; it never raises (review W4 on 39c010a). Whatever goes wrong inside,
    a device name the path library cannot relate, a workspace that is not a path, a
    seat name that is not text, is "not eligible" with reason FAULT. A wall that raised
    would leave the outcome to whatever its caller does with an error.
    """
    text = ""
    try:
        command = args.get("command") if isinstance(args, Mapping) else None
        text = command if isinstance(command, str) else ""
        return _judge(tool_name, args, command, text, shell, workspace, seat_name, cwd)
    except Exception:                 # noqa: BLE001 - the one place every failure must become "no"
        return PlainRead(False, FAULT, command=text)


def _judge(tool_name, args, command, text: str, shell, workspace, seat_name, cwd) -> PlainRead:
    def no(reason: str) -> PlainRead:
        return PlainRead(False, reason, command=text)

    name = tool_name.lower() if isinstance(tool_name, str) else ""
    if name not in SHELL_TOOLS:
        return no(TOOL)
    if shell != SHELL_TOOLS[name]:
        return no(SHELL)
    if not isinstance(command, str) or set(args) - ARGUMENT_KEYS:
        return no(ARGUMENTS)
    if not text.strip():
        return no(EMPTY)
    if len(text) > MAX_COMMAND_CHARS:
        return no(TOO_LONG)
    try:
        verbs, operands = _plan(text, shell)
    except _No as why:
        return no(why.reason)
    roots = worktree_scope.own_roots(Path(workspace), seat_name)
    if not roots:
        return no(NO_OWN_FOLDER)
    bases = [Path(os.path.abspath(str(base))) for base in (workspace, cwd or Path.cwd())]
    first = ""
    for root in roots:
        try:
            shown = _locate(operands, root, bases)
        except _No as why:
            first = first or why.reason
            continue
        return PlainRead(True, "", tuple(verbs), tuple(shown), text)
    return no(first)
