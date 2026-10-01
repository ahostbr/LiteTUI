"""Which command words the own-worktree exemption trusts (T0246).

`worktree_scope` proves a command's PATHS stay inside the seat's tree. A path proof says
nothing about what a verb does outside paths: `kill`, `net stop`, `Stop-Service`,
`wmic process call create` act on the system with no path at all. So the exemption is
an ALLOWLIST of in-tree verbs, not a denylist of system ones: a verb that is not named
here is NOT exempt and prompts exactly as it did before the exemption existed, so the
next system verb nobody listed is a prompt, not a hole. (GuardTuring: the danger table's
own detection of such verbs is negative and has gaps, card T0251.)

Pure text work: no filesystem, no process.

An allowlisted verb still carries per-verb refusals (`git -c`, `git push`,
`find -exec <system verb>`, `tar --to-command`, a launcher's own command line), and a
wrapper (`env`, `timeout`, ...) is peeled only with options this module can classify.
"""
from __future__ import annotations

import re


class VerbRefused(Exception):
    """The command is not exempt; the message says why."""


#: Verbs whose business is files, text, builds and tests in the tree. A path-shaped verb
#: (`./node_modules/.bin/vitest`) is allowed too: worktree_scope has already placed it
#: inside the tree. Anything else prompts.
IN_TREE_VERBS = frozenset({
    # delete / move / copy / make / inspect files
    "rm", "del", "erase", "rd", "rmdir", "ri", "remove-item", "unlink", "shred", "rimraf",
    "truncate", "mkdir", "md", "new-item", "cp", "copy", "copy-item", "mv", "move", "move-item",
    "rename-item", "ren", "touch", "ln", "ls", "dir", "get-childitem", "gci", "cat", "type",
    "get-content", "gc", "set-content", "add-content", "out-file", "echo", "printf", "write-output",
    "write-host", "pwd", "get-location", "stat", "file", "du", "tree", "basename", "dirname",
    "realpath", "readlink", "test", "[", "true", "false", ":", "sleep", "start-sleep", "date",
    "chmod", "chown", "attrib", "get-item", "test-path", "resolve-path", "join-path",
    # archives (the extract rows)
    "tar", "bsdtar", "unzip", "zip", "7z", "7za", "gunzip", "gzip", "bunzip2", "bzip2", "xz",
    "unxz", "zstd", "unzstd", "unrar", "unar", "expand-archive", "compress-archive", "expand",
    # text and search
    "grep", "egrep", "fgrep", "rg", "sed", "awk", "find", "fd", "head", "tail", "wc", "sort",
    "uniq", "cut", "tr", "diff", "cmp", "tee", "xxd", "select-string", "sls", "measure-object",
    "sort-object", "select-object", "where-object", "foreach-object", "format-table",
    "format-list", "out-string", "convertto-json", "convertfrom-json",
    # git (per-verb refusals below)
    "git",
    # languages, package managers, builds, tests
    "node", "npm", "npx", "bun", "bunx", "pnpm", "yarn", "deno", "tsc", "vitest", "jest",
    "eslint", "prettier", "vite", "esbuild", "electron-vite", "python", "python3", "py", "pytest",
    "uv", "uvx", "pip", "pip3", "ruff", "mypy", "black", "cargo", "rustc", "rustfmt", "make",
    "cmake", "ninja", "go", "dotnet", "msbuild", "gradle", "mvn", "java", "javac", "pwsh-script",
    "liteharness", "lst", "litetui",
    # launchers (their own command line is checked below)
    "cmd", "bash", "sh", "zsh", "dash", "pwsh", "powershell", "start", "start-process", "saps",
    "invoke-item", "ii", "env",
    # shell control words with no effect of their own
    "fi", "done", "esac", "}", "for", "select", "case", "function", "return", "export",
    "set", "unset", "local", "source", ".", "break", "continue", "wait", "exit", "cd",
})

#: Refused outright even if a table or a typo ever listed them as in-tree.
SYSTEM_VERBS = frozenset({
    "kill", "pkill", "killall", "taskkill", "tskill", "stop-process", "reg", "regedit", "net",
    "net1", "sc", "schtasks", "register-scheduledtask", "unregister-scheduledtask",
    "shutdown", "restart-computer", "stop-computer", "netsh", "diskpart", "bcdedit", "format",
    "mkfs", "dd", "vssadmin", "cipher", "wevtutil", "clear-eventlog", "set-executionpolicy",
    "icacls", "takeown", "iex", "invoke-expression", "set-mppreference", "add-mppreference",
    "msiexec", "regsvr32", "rundll32", "runas", "psexec", "psexec64", "wscript", "cscript",
    "mshta", "set-itemproperty", "new-itemproperty", "remove-itemproperty", "wmic", "powercfg",
    "sudo", "doas", "su", "systemctl", "service", "launchctl", "crontab", "at",
})
_SYSTEM_FAMILIES = re.compile(
    r"^(?:stop|start|restart|set|new|remove|suspend|resume)-service$"
    r"|^[a-z]+-netfirewall|^(?:disable|enable)-[a-z]"
    r"|^(?:set|new|remove)-net[a-z]|^(?:set|new|remove)-scheduledtask$|^restart-")

#: Words that run what follows them (peeled before the verb is read). Control keywords
#: take nothing; the rest take only the options listed in _skip_wrapper_args.
_KEYWORDS = frozenset({"then", "do", "else", "elif", "if", "while", "until", "!", "{"})
_WRAPPERS = _KEYWORDS | {"env", "nohup", "time", "command", "exec", "xargs", "nice", "ionice",
                         "timeout", "stdbuf", "builtin", "busybox"}

_LAUNCHER_SHELLS = frozenset({"bash", "sh", "zsh", "dash", "pwsh", "powershell"})
_PAYLOAD_FLAGS = frozenset({"-c", "-command", "-encodedcommand", "-ec", "-e"})
_LAUNCHER_PROGRAMS = frozenset({"start", "start-process", "saps", "invoke-item", "ii"})
_DURATION = re.compile(r"\d+(?:\.\d+)?[smhd]?")


def _skip_wrapper_args(wrapper: str, words: list[str], i: int) -> int:
    """Index of the first word after `wrapper`'s own arguments. An option this does not
    recognise is refused: guessing whether `-u root` takes a value is how `sudo -u root
    kill` read `root` as the verb."""
    n = len(words)
    if wrapper in _KEYWORDS:
        return i
    if wrapper == "env":
        while i < n and "=" in words[i] and not words[i].startswith("-"):
            i += 1
        if i < n and words[i].startswith("-"):
            raise VerbRefused("an `env` option")
        return i
    if wrapper == "timeout":
        while i < n and words[i] in ("-s", "-k") and i + 1 < n:
            i += 2
        if i < n and _DURATION.fullmatch(words[i]):
            return i + 1
        if i < n and words[i].startswith("-"):
            raise VerbRefused("a `timeout` option")
        return i
    if wrapper == "nice":
        if i < n and words[i] == "-n" and i + 1 < n:
            return i + 2
        if i < n and re.fullmatch(r"-\d+", words[i]):
            return i + 1
    if wrapper == "xargs":
        while i < n and words[i] in ("-0", "-r", "--no-run-if-empty", "--null"):
            i += 1
    if wrapper == "time" and i < n and words[i] == "-p":
        return i + 1
    if i < n and words[i].startswith("-"):
        raise VerbRefused(f"a `{wrapper}` option")
    return i


def effective_verb(words: list[str]) -> tuple[str, list[str], str]:
    """(verb, its arguments, the verb word as written) once leading VAR=x assignments
    and wrapper words are peeled. The verb is a lower-case basename without `.exe`."""
    i = 0
    while i < len(words):
        word = words[i]
        if word.lower() in _WRAPPERS:
            i = _skip_wrapper_args(word.lower(), words, i + 1)
            continue
        if re.fullmatch(r"[A-Za-z_]\w*=.*", word):
            i += 1
            continue
        break
    if i >= len(words):
        return "", [], ""
    written = words[i]
    verb = re.split(r"[\\/]", written)[-1].lower()
    if verb.endswith(".exe"):
        verb = verb[:-4]
    return verb, words[i + 1:], written


def is_system_verb(verb: str) -> bool:
    return verb in SYSTEM_VERBS or bool(_SYSTEM_FAMILIES.match(verb))


def _refuse_git_config(args: list[str]) -> None:
    """`git -c alias.p=push p`, `--config-env`, `--exec-path`: configuration given on the
    command line can make any later word run any program. Only git's GLOBAL options (before
    the subcommand) are read; `-C <path>` and friends take a value and are skipped."""
    i = 0
    while i < len(args):
        w = args[i]
        low = w.lower()
        attached = w.startswith("-c") and "=" in w and not w.startswith("--")   # -cname=value
        if w == "-c" or attached or low.startswith(("--config-env", "--exec-path")):
            raise VerbRefused("`git -c`/`--config-env` can define an alias that runs anything")
        if w in ("-C", "--git-dir", "--work-tree", "--namespace"):
            i += 2
        elif w.startswith("-"):
            i += 1
        else:
            return


def require_in_tree_verb(words: list[str]) -> None:
    """Raise VerbRefused unless this command line's effective verb is a trusted in-tree
    verb (and passes that verb's own refusals). No words, or only assignments: fine."""
    verb, rest, written = effective_verb(words)
    if not verb:
        return
    if is_system_verb(verb):
        raise VerbRefused(f"a system action ({verb}) is not scoped by a path")
    path_shaped = bool(re.search(r"[\\/]", written)) or written.startswith(".")
    if verb not in IN_TREE_VERBS and not path_shaped:
        raise VerbRefused(f"`{verb}` is not on the in-tree verb list")
    low = [w.lower() for w in rest]
    if verb == "git":
        if "push" in low or "config" in low:
            raise VerbRefused("a `git push`/`git config` acts beyond the tree")
        _refuse_git_config(rest)
    if verb in ("find", "xargs", "fd"):
        for flag in ("-exec", "-execdir", "-ok", "-okdir", "-x", "--exec"):
            if flag in low:
                tail = rest[low.index(flag) + 1:]
                cmd = []
                for w in tail:
                    if w in (";", "\\;", "+"):
                        break
                    cmd.append(w)
                require_in_tree_verb(cmd)
    if verb in ("tar", "bsdtar") and any(
            re.search(r"to-command|checkpoint-action|use-compress-program|^-i$", w) for w in low):
        raise VerbRefused("a tar option that runs a program")
    if verb in _LAUNCHER_SHELLS:
        for index, w in enumerate(low):
            if w in _PAYLOAD_FLAGS:
                require_in_tree_verb(rest[index + 1:])
                return
        return                                        # `bash script.sh`: the script is a path
    if verb == "cmd":
        tail = list(rest)
        while tail and re.fullmatch(r"/[A-Za-z]+", tail[0]):
            tail = tail[1:]
        if tail:
            require_in_tree_verb(tail)
        return
    if verb in _LAUNCHER_PROGRAMS:
        tail = list(rest)
        while tail and (tail[0].startswith("-") or re.fullmatch(r"/[A-Za-z]+", tail[0])):
            tail = tail[1:]
        if tail:
            require_in_tree_verb(tail)
