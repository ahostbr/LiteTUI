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

Ceiling (deliberate): a target held in an arbitrary variable (`$x`, `$tmp`) is
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
_GLOB = re.compile(r"[*?]")
_MSYS = re.compile(r"^/([a-z])(/.*)?$", re.IGNORECASE)


def refusal(command, workspace, home=None) -> str | None:
    """The sentence that refuses `command`, or None when the floor allows it."""
    if not isinstance(command, str):
        command = " ".join(map(str, command or ()))
    workspace = Path(workspace)
    home = Path(home) if home is not None else Path.home()
    for match in _VERB.finditer(command):
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
            resolved = _resolve(raw, workspace, home)
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
    filtered, in_paths = False, True
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
            elif low in ("-delete", "rm", "rmdir", "remove-item", "rimraf"):
                deletes = True
            elif low in _FIND_FILTERS:
                filtered = True
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
        recursive = not filtered
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
    words = _TOKEN.findall(head)[1:]
    return [w for w in words if not _unquote(w).startswith("-")]


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


def _resolve(raw: str, workspace: Path, home: Path) -> Path | None:
    """The folder a target names, or None when a variable hides it."""
    parts = re.split(r"[\\/]", _unquote(raw))
    while len(parts) > 1 and parts[-1] == "":
        parts.pop()  # trailing separators
    while parts and _GLOB.search(parts[-1]):
        parts.pop()  # `dir/*` empties dir: judge it as dir
    if not parts:
        return workspace.resolve()
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
        path = workspace / path
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
