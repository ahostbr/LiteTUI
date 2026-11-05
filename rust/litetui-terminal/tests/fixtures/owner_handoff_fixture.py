"""A private Git repository and fixture handoff executable. Never use the host harness."""
import os
import shutil
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


def selected():
    return Path(os.environ["LITETUI_DATA_ROOT"]).resolve().parent / "workspace"


@contextmanager
def configure(history):
    from litetui import owner_mode, ttyguard
    directory = selected()
    assert directory.parent == history.root.resolve().parent and directory.name == "workspace"
    assert directory.is_dir() and not directory.is_symlink()
    home = directory.parent / "private-git-home"
    home.mkdir(exist_ok=True)
    for key in list(os.environ):
        if key.upper().startswith("GIT_"):
            os.environ.pop(key)
    os.environ.update(HOME=str(home), USERPROFILE=str(home), XDG_CONFIG_HOME=str(home),
                      GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull)
    git = shutil.which("git")
    assert git
    if not (directory / ".git").exists():
        assert not list(directory.iterdir())
        def setup(*args):
            result = subprocess.run([git, *args], cwd=directory, capture_output=True, check=False)
            assert result.returncode == 0, (args, result.stderr)
        setup("init", "--template=", "--initial-branch=private-owner")
        setup("config", "--local", "user.name", "Private fixture")
        setup("config", "--local", "user.email", "private@example.invalid")
        setup("config", "--local", "core.autocrlf", "false")
        setup("config", "--local", "core.longpaths", "true")
        for name in ("SOUL.md", "NOW.md", "DOMAIN.md", "unrelated.txt"):
            (directory / name).write_text("Private starting " + name + "\n", encoding="utf-8")
        setup("add", "--", "SOUL.md", "NOW.md", "DOMAIN.md", "unrelated.txt")
        setup("commit", "-m", "Private starting point")
        (directory / "unrelated.txt").write_text("Keep this unrelated staged edit.\n", encoding="utf-8")
        setup("add", "--", "unrelated.txt")

    history.boundary.real_git_read = ttyguard.run
    command = [sys.executable, "-I", str(Path(__file__).resolve())]
    popen = subprocess.Popen
    def spawn(argv, *args, **kwargs):
        if (argv[:2] == [git, "--no-optional-locks"] and Path(kwargs["cwd"]).samefile(directory)
                and argv[2] in {"rev-parse", "symbolic-ref", "check-attr", "rev-list", "diff-tree", "cat-file"}):
            return popen(argv, *args, **kwargs)
        if list(argv[:-1]) == [*command, "owner-handoff", "--memory"]:
            destination = Path(argv[-1]).resolve()
            if destination.samefile(directory) or (destination.parent == Path(tempfile.gettempdir()).resolve()
                    and destination.name.startswith("litetui-owner-probe-") and not list(destination.iterdir())):
                return popen(argv, *args, **kwargs)
        return history.boundary.forbidden()
    with patch.object(owner_mode, "handoff_command", lambda: command), patch.object(
            history.boundary, "scoped_popen", spawn, create=True):
        yield


if __name__ == "__main__":
    assert sys.argv[1:3] == ["owner-handoff", "--memory"] and len(sys.argv) == 4
    folder = Path(sys.argv[3]).resolve()
    if not folder.samefile(selected()):
        assert folder.name.startswith("litetui-owner-probe-") and not list(folder.iterdir())
        print("HANDOFF REFUSED: missing private fixture owner documents")
        sys.exit(2)
    assert folder.samefile(selected()) and folder.joinpath(".git").is_dir()
    assert os.environ.get("LITEHARNESS_AGENT_ID")
    assert (folder / "NOW.md").read_text(encoding="utf-8").startswith("Fresh private owner handoff.")
    subprocess.run(["git", "commit", "-m", "Private owner handoff", "--", "NOW.md", "DOMAIN.md"],
                   cwd=folder, check=True, capture_output=True)
