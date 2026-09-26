"""The deny floor: shell deletes that no profile, standing rule or flag can run.

ONE RULE SET, TWO RUNTIMES. Canonical: liteharness-oss `liteharness/deny_floor.py`
(the Claude Code PreToolUse hook). LiteTUI vendors a byte-identical copy at
`src/litetui/deny_floor.py` (its tool-policy deny gate), because neither runtime
can import the other: LiteTUI's venv has no liteharness and the hook's Python
has no litetui. Edit the canonical file, then run LiteTUI's
`scripts/sync_deny_floor.py`; a LiteTUI test fails while the two differ.

Why it exists (2026-09-26 16:45): an AUTONOMOUS seat ran
`$home='...'; Remove-Item $home -Recurse -Force -ErrorAction SilentlyContinue`.
$HOME is read-only in PowerShell, the assignment failed without stopping the
script, and the user's profile folder was deleted. The command WAS classified
as destructive; the profile allowed it anyway. This floor sits below every
profile.

Two rules, both about what the command TARGETS, never about how it is spelled:

  home-variable-delete    any delete whose target is a home-directory variable
                          ($home, $HOME, ~, $env:USERPROFILE, %USERPROFILE%,
                          $env:HOME, ...). A string classifier cannot know what
                          such a variable holds at run time, so the literal is
                          refused whatever the script claims to assign it.
  protected-root-delete   a RECURSIVE delete whose resolved target is, or
                          contains, a drive root, a user profile root, the
                          profile's .claude/.codex/.liteharness/.litesuite, a
                          git repository root (a `.git` DIRECTORY; a worktree's
                          `.git` file is not one), or a folder containing the
                          workspace (the workspace itself only when it is a repo).

Relative targets resolve against the folder the command is IN at that point:
`cd ~; Remove-Item * -Recurse` is judged as a delete of the profile, because a
cd / chdir / pushd / Set-Location / sl / Push-Location earlier in the same
command moves the base the later targets resolve against.

Ceiling (deliberate): a target, or a cd, held in an arbitrary variable or
expression (`$x`, `$tmp`, `([Environment]::GetFolderPath('UserProfile'))`) is
not resolvable here and is not refused; a delete run from inside a script file
is out of reach. This is a floor under the danger table, not a sandbox.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

HOME_VARIABLES = frozenset({
    "~", "$home", "${home}", "$env:home", "${env:home}",
    "$env:userprofile", "${env:userprofile}", "$userprofile", "${userprofile}",
    "%userprofile%", "%home%", "%homedrive%%homepath%",
})
HARNESS_DIRS = (".claude", ".codex", ".liteharness", ".litesuite")

#: A delete verb at a word boundary. `find` counts only with -delete / -exec rm.
_VERB = re.compile(
    r"(?i)(?<![\w.$-])(remove-item|rimraf|rmdir|rm|rd|del|erase|ri|find)"
    r"(?:\.exe)?(?![\w.:\\-])")
#: A shell word: quoted runs may hold spaces, `"$HOME"/*` stays one word, and an
#: unbalanced quote (the end of a `-c "..."` string) is a word of its own.
_TOKEN = re.compile(r"""(?:"[^"]*"|'[^']*'|[^\s"'])+|["']""")
_CMD_VERBS = frozenset({"rd", "rmdir", "del", "erase"})  # take cmd.exe /s /q flags
#: A find predicate that narrows what is deleted. Without one, `find X -delete`
#: (even with `-type f`) empties X, so it is judged as a recursive delete of X.
_FIND_FILTERS = frozenset({
    "-name", "-iname", "-path", "-ipath", "-wholename", "-iwholename", "-regex",
    "-iregex", "-newer", "-mtime", "-mmin", "-atime", "-amin", "-ctime", "-cmin",
    "-size", "-empty", "-user", "-group", "-perm", "-links", "-inum", "-samefile",
})
#: ...except a name/path/regex test whose pattern is only wildcards: `-name '*'`
#: matches everything, so it narrows nothing (review a93de8a9 R2).
_PATTERN_TESTS = frozenset({"-name", "-iname", "-path", "-ipath", "-wholename",
                            "-iwholename", "-regex", "-iregex"})


def _find_filtered(words: list[str]) -> bool:
    """Does this find expression (lower-case, unquoted words) narrow its matches?"""
    for i, word in enumerate(words):
        if word in _PATTERN_TESTS:
            pattern = words[i + 1] if i + 1 < len(words) else ""
            if not re.fullmatch(r"[*?.+]*", pattern):
                return True
        elif word in _FIND_FILTERS:
            return True
    return False
#: A change of directory; the words after it name the new base.
_CD = re.compile(
    r"(?i)(?<![\w.$-])(?:cd|chdir|pushd|set-location|sl|push-location)(?![\w.:\\-])")
#: `git` STARTING a command, right before the verb, on the same line. `\s+`
#: here crossed a newline, so a line ending in "git" excused the delete on the
#: next line (review 2be2a62c N1: "ls .git\nrm -rf ~" was allowed).
_GIT_VERB = re.compile(r"(?i)(?:^|[;&|\n(])[ \t]*git[ \t]+\Z")  # \Z: `$` matches before a final \n
_GLOB = re.compile(r"[*?]")
_MSYS = re.compile(r"^/([a-z])(/.*)?$", re.IGNORECASE)


def refusal(command, workspace, home=None) -> str | None:
    """The sentence that refuses `command`, or None when the floor allows it."""
    if not isinstance(command, str):
        command = " ".join(map(str, command or ()))
    workspace = Path(workspace)
    home = Path(home) if home is not None else Path.home()
    base: Path | None = workspace   # where relative targets resolve; None = unknown
    steps = sorted([*((m.start(), m) for m in _CD.finditer(command)),
                    *((m.start(), m) for m in _VERB.finditer(command))],
                   key=lambda step: step[0])
    for _, match in steps:
        if match.re is _CD:
            base = _cd_target(command[match.end():], base, home)
            continue
        if _GIT_VERB.search(command[:match.start()]):
            continue  # `git rm` works on the index and tracked files, not a tree
        verb = match.group(1).lower()
        recursive, targets, deletes = _arguments(verb, command[match.end():])
        if not deletes:
            continue
        if verb != "find":
            targets += _pipeline_source(command[:match.start()])
        for raw in targets:
            if _normal(raw) in HOME_VARIABLES:
                return _say("home-variable-delete",
                            f"it deletes {raw!r}, a home-directory variable whose value "
                            "the host cannot see")
            if not recursive:
                continue
            resolved = _resolve(raw, base, home)
            why = resolved and _protected(resolved, workspace, home)
            if why:
                return _say("protected-root-delete",
                            f"it recursively deletes {resolved}, {why}")
    return None


def _say(rule: str, what: str) -> str:
    return (f"DENY FLOOR [{rule}]: {what}. No tool profile, standing rule or "
            "rewording can run this; do not retry it in another form. Delete a "
            "specific folder by its literal path instead.")


def _unquote(word: str) -> str:
    return word.replace('"', "").replace("'", "").strip()


def _separator(word: str) -> int:
    """Index of the first `;`, `|` or `&` outside quotes, or -1."""
    quote = ""
    for i, ch in enumerate(word):
        if quote:
            quote = "" if ch == quote else quote
        elif ch in "\"'":
            quote = ch
        elif ch in ";|&":
            return i
    return -1


def _arguments(verb: str, rest: str):
    """(recursive, targets, deletes) for the words after one delete verb."""
    recursive = verb == "rimraf"
    deletes = verb != "find"
    find_words, in_paths = [], True
    targets: list[str] = []
    for token in _TOKEN.findall(rest.split("\n", 1)[0]):
        if token.startswith("\\;"):
            break  # the end of a find -exec
        cut = _separator(token)
        stop = cut >= 0
        if stop:
            token = token[:cut]
        if token[:1] in (")", "}"):
            break
        token = token.rstrip(")")
        if token.count("}") > token.count("{"):
            token = token.rstrip("}")
        low = _unquote(token).lower()
        if not low or low in ("--", "{}"):
            pass
        elif verb == "find":
            in_paths = in_paths and not (low.startswith("-") or low in ("(", "!"))
            if in_paths:
                targets.append(token)
            else:
                find_words.append(low)
                if low in ("-delete", "rm", "rmdir", "remove-item", "rimraf"):
                    deletes = True
        elif low.startswith("-"):
            name, _, value = low.partition(":")
            if (name == "--recursive" or ("recurse".startswith(name[1:]) and len(name) > 1)
                    or (re.fullmatch(r"-[a-z]*r[a-z]*", name) and len(name) <= 4)):
                recursive = True
            if value and value not in ("$true", "$false"):
                targets.append(token.partition(":")[2])
        elif verb in _CMD_VERBS and re.fullmatch(r"/[a-z]", low):
            recursive = recursive or low == "/s"
        else:
            targets.append(token)
        if stop:
            break
    if verb == "find":
        recursive = not _find_filtered(find_words)
    return recursive, targets, deletes


def _pipeline_source(before: str) -> list[str]:
    """`gci $home | Remove-Item -Recurse`: the pipe's head names the target."""
    tail = before.rstrip()
    xargs = re.search(r"\|\s*xargs(?:\s+-\S+)*$", tail)
    if xargs:
        tail = tail[:xargs.start()]
    elif tail.endswith("|"):
        tail = tail[:-1]
    else:
        return []
    head = re.split(r"[;\n{(]|&&|\|\|", tail)[-1].split("|")[0]
    words = [_unquote(w) for w in _TOKEN.findall(head)]
    if words[:1] == ["find"] and _find_filtered([w.lower() for w in words]):
        # A filtered listing names only what matched, not its root -- except a
        # home-variable root, refused whatever narrows it (review a93de8a9 R1:
        # `find $HOME -type d -name .claude | xargs rm -rf` deletes ~/.claude).
        return [w for w in words[1:] if not w.startswith("-") and _normal(w) in HOME_VARIABLES]
    paths = [w for w in words[1:] if not w.startswith("-")]
    # A listing with no path lists the current folder (review 2be2a62c N2:
    # `cd ~; ls | xargs rm -rf`); "." resolves against the tracked base.
    return paths or ["."]


def _normal(raw: str) -> str:
    """Lower-case, unquoted, with trailing separators and globs removed."""
    text = _unquote(raw).lower()
    while True:
        text = text.rstrip("/\\")
        head, sep, last = text.replace("\\", "/").rpartition("/")
        if sep and _GLOB.search(last):
            text = text[:len(head)]
            continue
        return text


def _cd_target(rest: str, base: Path | None, home: Path) -> Path | None:
    """The base after `cd <rest>`: the first word that is not a flag. A bare
    `cd` goes home (bash); `-` or a variable leaves the base unknown."""
    for token in _TOKEN.findall(rest.split("\n", 1)[0]):
        cut = _separator(token)
        word = _unquote(token[:cut] if cut >= 0 else token)
        low = word.lower()
        if low.startswith("-") and ":" in word:
            word = low = word.partition(":")[2]   # -Path:C:\x names the folder (N3)
        if word and not (low.startswith("-") or re.fullmatch(r"/[a-z]", low)):
            return _resolve(word, base, home)
        if word == "-":
            return None
        if cut >= 0:
            break
    return home.resolve()


def _resolve(raw: str, base: Path | None, home: Path) -> Path | None:
    """The folder a target names, or None when a variable (or an unknown base
    under a relative target) hides it."""
    parts = re.split(r"[\\/]", _unquote(raw))
    while len(parts) > 1 and parts[-1] == "":
        parts.pop()  # trailing separators
    while parts and _GLOB.search(parts[-1]):
        parts.pop()  # `dir/*` empties dir: judge it as dir
    if not parts:
        return base.resolve() if base is not None else None
    text = "/".join(parts) if parts != [""] else "/"
    low = text.lower()
    for name in sorted(HOME_VARIABLES, key=len, reverse=True):
        if low == name or low.startswith(name + "/"):
            text = str(home) + text[len(name):]
            break
    if re.search(r"\$|%\w+%", text):
        return None  # an arbitrary variable: not resolvable from the string
    msys = _MSYS.match(text)
    if os.name == "nt" and msys:
        text = f"{msys.group(1)}:/{(msys.group(2) or '/').lstrip('/')}"
    if os.name == "nt" and re.fullmatch(r"[a-z]:", text, re.IGNORECASE):
        text += "/"
    path = Path(text)
    if not path.is_absolute():
        if base is None:
            return None
        path = base / path
    try:
        return path.resolve()
    except OSError:
        return Path(os.path.abspath(path))


def _contains(outer: Path, inner: Path) -> bool:
    """`inner` is `outer` or lies under it (case-insensitive on Windows)."""
    a, b = os.path.normcase(str(outer)), os.path.normcase(str(inner))
    return b == a or b.startswith(a.rstrip("\\/") + os.sep)


def _protected(target: Path, workspace: Path, home: Path) -> str | None:
    home = home.resolve()
    if target.parent == target:
        return "a drive root"
    if _contains(target, home):
        return "the user profile root (or a folder containing it)"
    if os.path.normcase(str(target.parent)) == os.path.normcase(str(home.parent)):
        return "a user profile root"
    for name in HARNESS_DIRS:
        if _contains(target, home / name):
            return f"~/{name} (or a folder containing it)"
    workspace = workspace.resolve()
    if _contains(target, workspace) and not _contains(workspace, target):
        return "a folder containing the workspace"
    if (target / ".git").is_dir():
        return "a git repository root"
    return None
