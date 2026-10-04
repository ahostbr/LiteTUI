"""Is a shell command CONFINED to the seat's own git worktree? (T0246)

`tool_policy` asks this one question before it prompts an interactive seat about a
DANGER_TABLE row: a command that runs entirely inside the tree the seat owns
should not need a human (Owner: "cmds inside its worktree that arent removal of the
tree... it should never need approval on interactive").

This module only READS: it looks at `.git` files and directory listings and
resolves paths. It starts no process and writes nothing. Its answer is a REFUSAL
REASON or None, and every doubt is a refusal: a command we cannot parse, a path
we cannot place, a shell construct we cannot follow all return a reason, which
leaves today's behaviour (the prompt) in force.

What "own worktree" means:
  * the seat's workspace sits INSIDE a linked worktree (`.git` is a FILE whose
    `gitdir:` points under `.git/worktrees/`): that worktree's root is the one root;
  * the workspace sits in a main checkout (`.git` is a DIRECTORY): the roots are
    `<checkout>/.worktrees/<SeatName>-*` (each itself a linked worktree).
  A plain directory, or a main checkout with no such worktree, has no root, so
  nothing is exempt.

What "confined" means (a command is confined to ONE root, all of it):
  * every `cd` / `Set-Location` / `pushd` target resolves inside the root;
  * every absolute path, `~` path and `..` path resolves inside the root;
  * when the seat did NOT start inside the root (it sits in the main checkout) the
    command must open with that `cd` and contain no `||`; a `;` or newline after it
    is accepted only when every cd target exists now, because `cd X; rm y` runs
    `rm y` where the seat started if the cd fails;
  * the worktree itself is never an operand of a deletion: not its root, not its
    `.git` link, and nothing above it (an operand above the root is outside).

CEILING (deliberate): an interpreter fed a heredoc or `-c` string is judged by the
path LITERALS it contains, exactly as `python script.py` is already unjudged by the
danger table. Code that computes a path at run time is not seen.

ACCEPTED, same class: a package runner (`npm exec`, `npx`, `bunx`, `pnpm dlx`, `yarn dlx`)
runs an arbitrary package exactly as `npm run <script>` and `python script.py` already run
arbitrary code without a prompt. The exemption proves a command's PATHS and VERBS (see
scope_verbs); it does not read the code of a program it is allowed to run. Pinned in
tests/test_worktree_scope.py so it stays a decision. What IS refused: git subcommands and
text-tool programs whose whole purpose is to run a command (`git rebase -x`, `bisect run`,
`submodule foreach`, `difftool`, awk `system`/pipes, sed `e`).
"""
from __future__ import annotations

import json
import os
import re
import warnings
from pathlib import Path

from litetui import scope_verbs

_CD_VERBS = frozenset({"cd", "chdir", "pushd", "set-location", "sl"})
#: A word after one of these is a command line, not an argument: `bash -c "..."`,
#: `powershell -Command "..."`, `cmd /c "..."`, `node -e "..."`.
_PAYLOAD_FLAGS = frozenset({"-c", "-command", "-e", "-ec", "-encodedcommand", "/c", "/k",
                            "--command", "--eval"})
_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "pwsh", "powershell", "cmd", "xargs", "env"})


def _require_verb(words: list[str]) -> None:
    """scope_verbs' allowlist, as this module's refusal."""
    try:
        scope_verbs.require_in_tree_verb(words)
    except scope_verbs.VerbRefused as why:
        raise _Refuse(str(why)) from None


def _effective(words: list[str]) -> str:
    try:
        return scope_verbs.effective_verb(words)[0]
    except scope_verbs.VerbRefused as why:
        raise _Refuse(str(why)) from None


_ABS = re.compile(r"^(?:[A-Za-z]:)?[\\/]")
_DRIVE_ABS = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/]")
_SWITCH = re.compile(r"^/[A-Za-z]$")            # `rd /s`, `cmd /c`: a switch, not a path
_DOTDOT = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")
#: A PowerShell PROVIDER path (`HKLM:\SOFTWARE\x`, `Env:\PATH`, `Cert:\...`, `Function:\x`):
#: `Remove-Item` on it deletes a registry key, an environment variable, a certificate,
#: not a file in the tree. Placed under the cwd it would look inside, so any word whose
#: `name:` prefix is longer than a drive letter is refused. In data text (a heredoc body,
#: where `https://x` is common) only the PowerShell provider names are.
_PROVIDER = re.compile(r"^[A-Za-z][A-Za-z0-9_]+:")
_PS_PROVIDER = re.compile(
    r"(?i)^(?:hk[a-z]{1,3}|hkey_[a-z_]+|env|cert|function|variable|alias|wsman|registry|"
    r"certificate|temp):")
_PIECE = re.compile(r"[^\s'\"`,;()<>|&]+")
_HEREDOC = re.compile(r"(?<!<)<<(?!<)(-?)[ \t]*(?:'([A-Za-z_]\w*)'|\"([A-Za-z_]\w*)\"|([A-Za-z_]\w*))")
_MARK = "\x00H\x00"
#: A segment that contains any of these words is a DELETION for the root-protection
#: check, whatever the danger table made of the command: the table does not see a
#: command that starts a new line, so "is this a deletion" is decided here per segment.
_DELETE_WORDS = frozenset({"rm", "del", "erase", "rmdir", "rd", "ri", "unlink", "shred",
                           "remove-item", "rimraf", "truncate", "find", "clean", "worktree"})


# ── roots ────────────────────────────────────────────────────────────────────


def _real(path: str | Path) -> str:
    try:
        return os.path.normcase(os.path.realpath(str(path)))
    except (OSError, ValueError):
        return os.path.normcase(os.path.normpath(str(path)))


def _inside(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("\\/") + os.sep)


def _linked_worktree_root(directory: Path) -> Path | None:
    """The linked-worktree root at or above `directory`, else None. The FIRST `.git`
    found walking up decides: a directory means a main checkout (not a worktree)."""
    for candidate in (directory, *directory.parents):
        git = candidate / ".git"
        try:
            if git.is_dir():
                return None
            if git.is_file():
                head = git.read_text(encoding="utf-8", errors="replace").splitlines()[:1]
                target = head[0].partition(":")[2].strip().replace("\\", "/") if head else ""
                return candidate if "/worktrees/" in target else None
        except OSError:
            return None
    return None


def _main_checkout(directory: Path) -> Path | None:
    for candidate in (directory, *directory.parents):
        try:
            if (candidate / ".git").is_dir():
                return candidate
        except OSError:
            return None
    return None


def own_roots(workspace: Path, seat_name: str | None) -> list[Path]:
    """The worktree root(s) this seat owns, resolved. Empty when it owns none."""
    try:
        ws = Path(os.path.realpath(str(workspace)))
    except (OSError, ValueError):
        return []
    if not ws.is_dir():
        return []
    linked = _linked_worktree_root(ws)
    if linked is not None:
        return [linked]
    name = (seat_name or "").strip().lower()
    main = _main_checkout(ws)
    if main is None or not name:
        return []
    base = main / ".worktrees"
    roots: list[Path] = []
    try:
        for entry in sorted(base.iterdir()):
            if (entry.name.lower().startswith(name + "-") and entry.is_dir()
                    and (entry / ".git").is_file()
                    and _real(entry.parent) == _real(base)):
                roots.append(Path(os.path.realpath(str(entry))))
    except OSError:
        return []
    return roots


def _output_scratch_bases() -> set[str]:
    """Machine-local opt-in only; empty env explicitly disables file defaults.

    The home file is shared by spawned seats even when a pane transport filters
    environment variables. Invalid config grants no card-parent expansion.
    """
    name = "LITETUI_OUTPUT_SCRATCH_ROOTS"
    try:
        if name in os.environ:
            values = [value for value in os.environ[name].split(os.pathsep) if value]
        else:
            config = Path.home() / ".litetui" / "output-roots.json"
            try:
                values = json.loads(config.read_text(encoding="utf-8"))["scratch_roots"]
            except FileNotFoundError:
                return set()
        if not isinstance(values, list) or not all(
            isinstance(value, str) and Path(value).expanduser().is_absolute()
            for value in values
        ):
            raise ValueError("scratch_roots must be a list of absolute paths")
        return {_real(Path(value).expanduser()) for value in values}
    except (OSError, ValueError, TypeError, KeyError) as error:
        warnings.warn(f"Output scratch roots disabled: {error}", stacklevel=2)
        return set()


def output_context(workspace: Path, seat_name: str | None) -> tuple[list[Path], dict[str, str]]:
    """T0331 output roots: own tree/card scratch and host TEMP, not foreign cards.

    This is path context only. The approval policy must still prove each target
    and every other effect; these roots grant no executable/destructive authority.
    """
    roots = own_roots(workspace, seat_name)
    scratch_bases = _output_scratch_bases()
    for root in list(roots):
        parent = root.parent
        if (re.fullmatch(r"T\d+(?:-[A-Za-z0-9]+)*", parent.name, re.IGNORECASE)
                and _real(parent.parent) in scratch_bases):
            roots.append(parent.resolve())
    variables = {}
    for name in ("TEMP", "TMP"):
        raw = os.environ.get(name, "")
        variables["$env:" + name.lower()] = raw
        if raw and Path(raw).is_absolute():
            roots.append(Path(raw).resolve())
    return roots, variables


# ── heredocs ─────────────────────────────────────────────────────────────────


def _split_heredocs(command: str) -> tuple[str, list[tuple[str, str]]] | None:
    """(skeleton, [(consumer_verb, body)]) with each heredoc body lifted out of the
    command text, or None when one is never closed. The skeleton keeps a marker where
    the operator was so the consuming command can be named."""
    lines = command.splitlines()
    out: list[str] = []
    bodies: list[list[str]] = []
    pending: list[tuple[str, bool, int]] = []
    for line in lines:
        if pending:
            delim, dash, k = pending[0]
            probe = (line.lstrip("\t") if dash else line).rstrip("\r")
            if probe == delim:
                pending.pop(0)
            else:
                bodies[k].append(line)
            continue
        quote = ""
        rebuilt: list[str] = []
        i = 0
        while i < len(line):
            ch = line[i]
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in "'\"":
                quote = ch
            elif ch == "<":
                m = _HEREDOC.match(line, i)
                if m:
                    delim = m.group(2) or m.group(3) or m.group(4)
                    bodies.append([])
                    pending.append((delim, bool(m.group(1)), len(bodies) - 1))
                    rebuilt.append(f" {_MARK} ")
                    i = m.end()
                    continue
            rebuilt.append(ch)
            i += 1
        out.append("".join(rebuilt))
    if pending:
        return None
    return "\n".join(out), [("", "\n".join(b)) for b in bodies]


def command_lines(command: str) -> list[str] | None:
    """The command's own lines with heredoc BODIES lifted out (a body is data, not a
    command line), or None when a heredoc is never closed. tool_policy runs the
    danger table on each line separately: its command-position anchor has no
    multiline flag, so a command that starts a new line is invisible to it when the
    whole text is scanned at once."""
    split = _split_heredocs(command)
    if split is None:
        return None
    return [line.replace(_MARK, " ") for line in split[0].splitlines()]


# ── tokenizer ────────────────────────────────────────────────────────────────


class _Refuse(Exception):
    pass


def _tokenize(text: str):
    """[(tokens, separator_after)] where a token is (text, quoted). Raises _Refuse for
    anything this finite reader does not follow."""
    segments: list[tuple[list[tuple[str, bool]], str]] = []
    tokens: list[tuple[str, bool]] = []
    buf: list[str] = []
    has = False
    quoted = False
    quote = ""
    i = 0

    def flush():
        nonlocal buf, has, quoted
        if has:
            tokens.append(("".join(buf), quoted))
        buf, has, quoted = [], False, False

    def end(sep):
        nonlocal tokens
        flush()
        segments.append((tokens, sep))
        tokens = []

    n = len(text)
    while i < n:
        ch = text[i]
        if quote:
            if ch == quote:
                quote = ""
            elif quote == '"' and ch in "$`":
                raise _Refuse("a variable or command substitution")
            else:
                buf.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote, has, quoted = ch, True, True
        elif ch in "$`()%":
            raise _Refuse("a variable, substitution or grouping")
        elif ch == "<":
            raise _Refuse("an input redirect")
        elif ch == "\\":
            if i + 1 < n and text[i + 1] in " '\"\t":
                raise _Refuse("a backslash escape")
            buf.append(ch)
            has = True
        elif ch in " \t\r":
            flush()
        elif ch == "\n":
            end("\n")
        elif ch == ";":
            end(";")
        elif ch in "&|":
            two = text[i:i + 2]
            if two in ("&&", "||"):
                end(two)
                i += 1
            else:
                end(ch)
        elif ch == ">":
            flush()
            if text[i + 1:i + 2] == ">":
                i += 1
            if text[i + 1:i + 2] == "&":      # `2>&1`
                i += 1
                while i + 1 < n and text[i + 1].isdigit():
                    i += 1
        else:
            buf.append(ch)
            has = True
        i += 1
    if quote:
        raise _Refuse("an unterminated quote")
    end("")
    return segments


# ── the check ────────────────────────────────────────────────────────────────


def _option_tails(option: str) -> list[str]:
    """The values an option token can carry INSIDE the same word, each to be judged as a
    path: `--git-dir=X`, PowerShell's `-Path:X` / `-LiteralPath:X`, and a short flag with
    its value attached (`-C../x`, `-I/usr/include`). Doubt is cheap: a tail that is not a
    path is placed under the cwd and passes."""
    tails: list[str] = []
    for sep in ("=", ":"):
        if sep in option:
            tails.append(option.split(sep, 1)[1])
    # `-C../x`, `-I/usr/include`: a flag letter then a PATH-shaped value. A flag letter then
    # more letters (`-Force`, `-Path:x`) is a long name, not an attached value.
    short = re.match(r"^-[A-Za-z]([^A-Za-z].*)$", option)
    if short:
        tails.append(short.group(1))
    return [t for t in tails if t]


class _Scope:
    def __init__(self, root: Path, start: Path, deleting: bool):
        self.root = _real(root)
        self.git_link = _real(Path(root) / ".git")
        self.cwd = os.path.normpath(str(start))
        self.deleting = deleting
        self.cds_exist = True

    def _place(self, value: str) -> str:
        if _ABS.match(value):
            return os.path.normpath(value)
        return os.path.normpath(os.path.join(self.cwd, value))

    def outside(self, value: str) -> bool:
        return not _inside(_real(self._place(value)), self.root)

    def check_value(self, value: str) -> None:
        """One path-ish word: refuse unless it is inside the root."""
        if not value:
            return
        if value.startswith("~"):
            raise _Refuse("a home-relative path")
        if value.startswith(("\\\\", "//")):
            raise _Refuse("a network path")
        if re.match(r"^[A-Za-z]:(?![\\/])", value):
            raise _Refuse("a drive-relative path")
        if _PROVIDER.match(value):
            raise _Refuse(f"{value!r} is a provider or URL path, not a file in the tree")
        # EVERY word is placed and resolved, not only absolute / `..` ones: a plain
        # relative word can cross a junction or symlink inside the tree that points
        # out of it (`link/x`), and only the resolved path shows that.
        placed = _real(self._place(value))
        if not _inside(placed, self.root):
            raise _Refuse(f"{value!r} resolves outside the worktree")
        if self.deleting and placed in (self.root, self.git_link):
            raise _Refuse(f"{value!r} is the worktree itself")

    def check_nested(self, text: str, *, strict: bool, commands: bool = False) -> None:
        """Text that is not tokens of THIS command: a quoted string with spaces, or a
        heredoc body. `strict` = it may be COMMANDS (a `-c` payload, a heredoc fed to a
        shell), so a nested cd or any `..` is refused outright; otherwise it is
        data/code judged by its path literals."""
        if commands:
            self.check_commands(text)
        for piece in _PIECE.findall(text):
            low = piece.lower()
            if strict and (low in _CD_VERBS or low == "popd"):
                raise _Refuse("a cd inside a quoted command")
            if strict and _DOTDOT.search(piece):
                raise _Refuse("a `..` path inside a quoted command")
            if _PS_PROVIDER.match(piece):
                raise _Refuse(f"{piece!r} is a PowerShell provider path")
            m = _DRIVE_ABS.search(piece)
            if m:
                self.check_value(piece[m.start():])
            elif piece.startswith("~/") or piece == "~":
                raise _Refuse("a home-relative path")
            elif piece.startswith("/") and len(piece) > 1 and re.match(r"/[A-Za-z0-9_.~-]", piece):
                self.check_value(piece)
            elif _DOTDOT.search(piece):
                self.check_value(piece)

    def check_commands(self, text: str) -> None:
        """Text that RUNS as commands (a `-c` payload, a heredoc fed to a shell): every
        segment is read as a command line of its own, and a system verb refuses. The
        text may not be tokenisable here (`$x`, subshells): that refuses too."""
        for tokens, _sep in _tokenize(text):
            _require_verb([t[0] for t in tokens])

    def check_token(self, text: str, quoted: bool, payload: bool = False) -> None:
        """`payload`: this quoted word follows -c / -Command / /c, so it RUNS as a command
        line. Any other quoted word with spaces (a commit message) is an argument: its
        path literals are judged, but its words are not read as commands."""
        if _SWITCH.match(text):
            return
        if quoted and re.search(r"[\s;&|<>()]", text):
            self.check_nested(text, strict=True, commands=payload)
            return
        if text.startswith("-"):
            for tail in _option_tails(text):
                self.check_value(tail)
            return
        self.check_value(text)

    def cd(self, args: list[tuple[str, bool]]) -> None:
        target = None
        it = iter(args)
        for text, _ in it:
            low = text.lower()
            if low in ("-path", "-literalpath"):
                target = next(it, (None, 0))[0]
                break
            if text.startswith("-") or low == "/d":
                if text == "-":
                    raise _Refuse("cd -")
                continue
            target = text
            break
        if not target or target.startswith("~") or target == "-":
            raise _Refuse("a cd with no explicit target")
        if _DRIVE_ABS.match(target) is None and re.match(r"^[A-Za-z]:", target):
            raise _Refuse("a drive-relative cd")
        placed = self._place(target)
        if not _inside(_real(placed), self.root):
            raise _Refuse(f"cd {target!r} leaves the worktree")
        if not os.path.isdir(placed):
            self.cds_exist = False
        self.cwd = placed


def violation(command: str, root: Path, start_cwd: Path, *, deleting: bool) -> str | None:
    """Why `command` is NOT confined to `root`, or None when it is."""
    try:
        split = _split_heredocs(command)
        if split is None:
            return "an unterminated heredoc"
        skeleton, heredocs = split
        segments = _tokenize(skeleton)
        scope = _Scope(root, start_cwd, deleting)
        deleting_overall = deleting
        started_outside = not _inside(_real(start_cwd), scope.root)
        if started_outside:
            first = segments[0][0] if segments else []
            if not first or first[0][0].lower() not in _CD_VERBS:
                return "it does not start with a cd into the worktree"
            if any(sep == "||" for _, sep in segments):
                return "a `||` after the cd"
        bodies = iter(heredocs)
        for tokens, _sep in segments:
            if not tokens:
                continue
            verb = tokens[0][0].lower()
            args = [t for t in tokens[1:]]
            if verb in _CD_VERBS:
                scope.cd(args)
                continue
            if verb == "popd":
                return "a popd"
            words = [t[0] for t in tokens if t[0] != _MARK]
            effective = _effective(words)
            if effective in _CD_VERBS or effective == "popd":
                return "a cd under a wrapper or condition cannot be followed"
            _require_verb(words)
            consumes = any(t[0] == _MARK for t in tokens)
            # judged per SEGMENT: a deletion on any line protects the root, whether or
            # not the danger table recognised it
            scope.deleting = deleting_overall or any(t[0].lower() in _DELETE_WORDS for t in tokens)
            previous = ""
            for text, quoted in tokens:
                if text != _MARK:
                    scope.check_token(text, quoted, payload=previous in _PAYLOAD_FLAGS)
                previous = text.lower()
            if consumes:
                _, body = next(bodies)
                runs_commands = effective in _SHELLS or verb in _SHELLS
                scope.check_nested(body, strict=runs_commands, commands=runs_commands)
        # `cd X; rm y` / a newline: if the cd FAILS the rest still runs where the seat
        # started (the main checkout). Fine only when every cd target exists right now.
        if started_outside and not scope.cds_exist and any(
                sep in (";", "\n", "&") for _, sep in segments):
            return "a `;`/newline after a cd into a directory that does not exist"
    except _Refuse as why:
        return str(why)
    return None


def confined(command: str, roots: list[Path], start_cwd: Path, *, deleting: bool) -> bool:
    return any(violation(command, root, start_cwd, deleting=deleting) is None for root in roots)
