"""The update check (src/litetui/update_check.py).

Covers the parts that can lie: the version ordering (a lexicographic compare
gets 0.10.0 < 0.9.0 wrong and ships a "newer available" notice on every minor
bump past 9), the TTL cache (a CLI launches constantly — the fetch must not
run on every one), the opt-outs (ported from the desktop updater's
getAutoUpdateDisabledReason), and the one thing a user actually sees: the
notice line in the chat log.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import pytest

from litetui import update_check

CUR = "0.24.0"


# ── Version ordering ─────────────────────────────────────────────────────────

def test_version_key_is_numeric_not_lexicographic():
    # The bug this file exists to keep dead: "0.9.0" > "0.10.0" as strings.
    assert update_check.version_key("0.10.0") > update_check.version_key("0.9.0")


def test_release_sorts_above_its_own_prerelease():
    assert update_check.version_key("0.24.0") > update_check.version_key("0.24.0.dev1")


def test_is_newer():
    assert update_check.is_newer("0.25.0", CUR)
    assert not update_check.is_newer(CUR, CUR)
    assert not update_check.is_newer("0.23.9", CUR)


def test_unparseable_sorts_lowest():
    assert update_check.version_key("not-a-version") < update_check.version_key("0.0.1")


# ── Opt-outs ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def clean_env(monkeypatch):
    """No opt-outs, no dev tree: every test below starts from "enabled"."""
    monkeypatch.delenv("LITETUI_DISABLE_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("LITETUI_FORCE_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(update_check, "_is_dev_tree", lambda: False)


def test_disabled_by_env(clean_env, monkeypatch):
    monkeypatch.setenv("LITETUI_DISABLE_UPDATE_CHECK", "1")
    r = update_check.maybe_check(CUR)
    assert r["status"] == "disabled"
    assert "LITETUI_DISABLE_UPDATE_CHECK" in r["reason"]


def test_disabled_in_dev_tree(monkeypatch):
    monkeypatch.delenv("LITETUI_DISABLE_UPDATE_CHECK", raising=False)
    monkeypatch.delenv("LITETUI_FORCE_UPDATE_CHECK", raising=False)
    monkeypatch.setattr(update_check, "_is_dev_tree", lambda: True)
    r = update_check.maybe_check(CUR)
    assert r["status"] == "disabled"
    assert "source checkout" in r["reason"]


def test_force_overrides_dev_tree_but_never_the_user(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(update_check, "_is_dev_tree", lambda: True)
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda: "99.0.0")
    # The dev-tree gate yields to FORCE...
    monkeypatch.setenv("LITETUI_FORCE_UPDATE_CHECK", "1")
    assert update_check.maybe_check(CUR)["status"] == "available"
    # ...and the user's own opt-out outranks FORCE.
    monkeypatch.setenv("LITETUI_DISABLE_UPDATE_CHECK", "1")
    assert update_check.maybe_check(CUR)["status"] == "disabled"


# ── The check and its cache ──────────────────────────────────────────────────

def _write_cache(root: Path, *, checked_at: float, current: str = CUR, latest: str = CUR, outcome: str = "up-to-date"):
    (root / update_check.CACHE_FILENAME).write_text(
        json.dumps({"latest": latest, "outcome": outcome, "current": current, "checked_at": checked_at}),
        encoding="utf-8",
    )


def test_fresh_cache_suppresses_the_fetch(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    _write_cache(tmp_path, checked_at=time.time())

    def _boom(*a, **k):  # pragma: no cover — the point is it must not run
        raise AssertionError("a fresh cache must not fetch")

    monkeypatch.setattr(update_check, "fetch_latest_version", _boom)
    r = update_check.maybe_check(CUR)
    assert r["status"] == "up-to-date"
    assert r["cached"] is True


def test_stale_cache_refetches(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    _write_cache(tmp_path, checked_at=time.time() - update_check.CHECK_TTL_SECONDS - 1)
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda: "0.25.0")
    r = update_check.maybe_check(CUR)
    assert r == {"status": "available", "latest": "0.25.0"}
    # and the fresh cache now suppresses the next launch's fetch
    assert update_check.maybe_check(CUR)["cached"] is True


def test_a_version_change_invalidates_the_cache(clean_env, monkeypatch, tmp_path):
    """A user who just upgraded must not be told an update is available that
    they are now ON: the cache is keyed on the running version."""
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    _write_cache(tmp_path, checked_at=time.time(), current="0.23.0", latest="0.24.0", outcome="available")
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda: "0.24.0")
    r = update_check.maybe_check(CUR)  # now running 0.24.0
    assert r["status"] == "up-to-date"
    assert "cached" not in r, "a version change must force a live fetch"


def test_fetch_failure_is_an_error_status_and_caches_nothing(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))

    def _boom(*a, **k):
        raise OSError("connection reset")

    monkeypatch.setattr(update_check, "fetch_latest_version", _boom)
    r = update_check.maybe_check(CUR)
    assert r["status"] == "error"
    assert "connection reset" in r["error"]
    assert not (tmp_path / update_check.CACHE_FILENAME).exists(), (
        "a failed fetch must not cache a failure into the TTL window"
    )


def test_corrupt_cache_is_ignored_not_fatal(clean_env, monkeypatch, tmp_path):
    monkeypatch.setenv("LITETUI_DATA_ROOT", str(tmp_path))
    (tmp_path / update_check.CACHE_FILENAME).write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda: CUR)
    assert update_check.maybe_check(CUR)["status"] == "up-to-date"


# ── The live UI path ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_notice_reaches_the_chat_log(clean_env, monkeypatch):
    """The end-to-end proof: boot the real app, fake a newer PyPI version,
    and require the [update] line in the chat log — the port of the desktop
    app's UPDATE_STATUS broadcast, one system line instead of a broadcast
    channel. system_message() mounts ChatMessage(Text(...)), so assert on
    the mounted renderable, not on the widget tree's internals."""
    from litetui.app import LiteTUI
    from litetui.widgets import ChatMessage

    # Collapse the 15 s startup delay and point the fetch at a canned
    # "newer" version.
    monkeypatch.setenv("LITETUI_DATA_ROOT", "/tmp/litetui-update-check-test")
    monkeypatch.setattr(update_check, "STARTUP_DELAY_SECONDS", 0.2)
    monkeypatch.setattr(update_check, "fetch_latest_version", lambda: "99.99.99")
    monkeypatch.setattr(update_check, "_started", False)

    a = LiteTUI()
    found = False
    async with a.run_test(size=(100, 30)) as pilot:
        for _ in range(60):  # ~3 s worst case at the app's pause cadence
            await pilot.pause()
            for c in a.query_one("#chat-log").children:
                if isinstance(c, ChatMessage):
                    content = getattr(c, "content", None)
                    chunk = content.plain if content is not None and hasattr(content, "plain") else str(content)
                    if "[update]" in chunk and "99.99.99" in chunk:
                        found = True
                        break
            if found:
                break
    assert found, "the [update] notice never reached the chat log"
