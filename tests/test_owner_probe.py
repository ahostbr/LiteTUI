"""The availability probe cannot commit a pre-existing owner folder."""
from pathlib import Path
import shutil
import subprocess

import pytest

from litetui import owner_mode


@pytest.mark.skipif(shutil.which("liteharness") is None, reason="liteharness is not installed")
def test_probe_does_not_mutate_a_valid_folder_at_the_old_probe_path(tmp_path, monkeypatch):
    folder = tmp_path / "litetui-owner-probe-does-not-exist"
    folder.mkdir()

    def git(*args):
        return subprocess.check_output(["git", "-C", str(folder), *args], text=True).strip()

    git("init")
    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    (folder / "SOUL.md").write_text("Widgets charter\n", encoding="utf-8")
    (folder / "DOMAIN.md").write_text("Widgets decisions\n", encoding="utf-8")
    now = "\n".join(f"## {heading}\n- widgets" for heading in (
        "Updated", "Doing now", "Next 3 steps", "Waiting on", "Crew alive", "Do NOT redo"))
    (folder / "NOW.md").write_text(now, encoding="utf-8")
    git("add", "SOUL.md", "DOMAIN.md", "NOW.md")
    git("commit", "-m", "test: seed owner files")
    (folder / "NOW.md").write_text(now + "\n- next widget\n", encoding="utf-8")
    before_head = git("rev-parse", "HEAD")
    before_status = git("status", "--porcelain")
    before_files = {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()}
    monkeypatch.setattr(owner_mode.tempfile, "gettempdir", lambda: str(tmp_path))
    try:
        owner_mode.require_handoff()
    except owner_mode.OwnerModeError:
        pass  # the old probe reports refusal only after it has committed
    assert git("rev-parse", "HEAD") == before_head, "capability probe created a commit"
    assert git("status", "--porcelain") == before_status
    assert {p.name: p.read_bytes() for p in folder.iterdir() if p.is_file()} == before_files


def test_probe_uses_and_removes_its_own_empty_directory(tmp_path, monkeypatch):
    seen = []

    def run(argv, **kwargs):
        folder = Path(argv[-1])
        assert folder.is_dir() and list(folder.iterdir()) == []
        seen.append(folder)
        return subprocess.CompletedProcess(argv, 2, "HANDOFF REFUSED: missing SOUL.md", "")

    monkeypatch.setattr(owner_mode.subprocess, "run", run)
    owner_mode.require_handoff()
    assert len(seen) == 1 and not seen[0].exists()
