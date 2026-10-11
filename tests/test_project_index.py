"""T0237 WS2: the cwd repo's AGENT_INDEX.md rides into the system prompt with the store.

Contract:
  * A conversation whose cwd is inside a repo holding AGENT_INDEX.md gets that
    text in system message 0, ONCE, persisted in convo.jsonl (a SNAPSHOT taken
    when the conversation starts; a resume keeps the snapshot it was given).
  * The Claude backend's segment prompt carries it too (same appsvc block).
  * Over 12k chars it is cut and the cut names the file. No index, no block.
  * The index is independent of the store latch: an empty store does not stop
    the index, and the index does not stop a later store write being picked up.
"""
import json
from pathlib import Path

import pytest

from litetui import appsvc
from litetui import app as app_mod
from litetui.project_index import INDEX_CAP, INDEX_NAME, find_index

INDEX_TEXT = "# Agent index\n\n- Dragon work: read `Docs/dragon.md` first (INDEX-MARKER-7741).\n"


def make_repo(root: Path, text: str | None = INDEX_TEXT) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")  # a worktree's .git is a file
    if text is not None:
        (root / INDEX_NAME).write_text(text, encoding="utf-8")
    return root


def make_app(monkeypatch, cwd: Path, with_store: bool = True):
    monkeypatch.chdir(cwd)
    app = app_mod.LiteTUI()
    app._materialise_convo()
    for name in ("memory.md", "soul.md", "handoff.md"):  # a fresh convo seeds templates: blank them
        (app.convo_dir / name).write_text("", encoding="utf-8")
    if with_store:
        (app.convo_dir / "soul.md").write_text("SOUL-MARKER-1188\n", encoding="utf-8")
    return app


# ── locating the index ─────────────────────────────────────────────────


def test_finds_the_index_from_a_subdirectory_of_the_repo(tmp_path):
    repo = make_repo(tmp_path / "repo")
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert find_index(sub) == repo / INDEX_NAME


def test_never_borrows_the_index_of_an_enclosing_repo(tmp_path):
    make_repo(tmp_path / "outer")
    inner = make_repo(tmp_path / "outer" / "inner", text=None)
    assert find_index(inner) is None


def test_no_git_root_means_only_the_cwd_itself_is_consulted(tmp_path):
    (tmp_path / INDEX_NAME).write_text("x", encoding="utf-8")
    sub = tmp_path / "sub"
    sub.mkdir()
    assert find_index(tmp_path) == tmp_path / INDEX_NAME
    assert find_index(sub) is None


# ── the plain (LiteTUI-native) path ────────────────────────────────────


def test_index_lands_in_message_zero_once_and_is_persisted(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    app = make_app(monkeypatch, repo)
    first = app._request_messages()[0]["content"]
    assert appsvc.INDEX_HEADER in first and "INDEX-MARKER-7741" in first
    assert str(repo / INDEX_NAME).replace("\\", "/") in first.replace("\\", "/"), "the path is named"
    again = app._request_messages()[0]["content"]
    assert again == first and again.count(appsvc.INDEX_HEADER) == 1
    # golden path: grep -c AGENT_INDEX convo.jsonl >= 1 (the edit record carries message 0)
    raw = (app.convo_dir / "convo.jsonl").read_text(encoding="utf-8")
    assert raw.count("AGENT_INDEX") >= 1 and "INDEX-MARKER-7741" in raw


def test_a_resumed_conversation_keeps_its_snapshot(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    app = make_app(monkeypatch, repo)
    app._request_messages()
    saved = app.conversation[0]["content"]
    (repo / INDEX_NAME).write_text("# CHANGED\nINDEX-MARKER-0000\n", encoding="utf-8")
    resumed = make_app(monkeypatch, repo)
    resumed.conversation[0] = {"role": "system", "content": saved}
    out = resumed._request_messages()[0]["content"]
    assert out == saved and "INDEX-MARKER-0000" not in out


def test_index_present_with_an_empty_store_still_injects_and_leaves_the_store_unlatched(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo")
    app = make_app(monkeypatch, repo, with_store=False)
    content = app._request_messages()[0]["content"]
    assert "INDEX-MARKER-7741" in content
    assert appsvc.STORE_HEADER not in content, "an empty store leaves no marker"
    (app.convo_dir / "soul.md").write_text("SOUL-MARKER-1188\n", encoding="utf-8")
    later = app._request_messages()[0]["content"]
    assert "SOUL-MARKER-1188" in later and later.count(appsvc.INDEX_HEADER) == 1


def test_no_index_means_no_block_and_no_behaviour_change(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo", text=None)
    app = make_app(monkeypatch, repo)
    content = app._request_messages()[0]["content"]
    assert appsvc.INDEX_HEADER not in content and "SOUL-MARKER-1188" in content


def test_oversize_index_is_cut_at_the_cap_and_the_cut_names_the_file(tmp_path, monkeypatch):
    repo = make_repo(tmp_path / "repo", text="HEAD-LINE\n" + "x" * (INDEX_CAP + 500))
    app = make_app(monkeypatch, repo)
    notices = []
    app._system = notices.append
    content = app._request_messages()[0]["content"]
    assert "HEAD-LINE" in content
    assert "x" * (INDEX_CAP - 20) in content and "x" * (INDEX_CAP + 100) not in content
    assert "truncated" in content and INDEX_NAME in content.split("truncated", 1)[1]
    assert any(INDEX_NAME in n and "truncated" in n for n in notices), "the user is warned too"


# ── the Claude backend path ────────────────────────────────────────────


def test_claude_segment_prompt_carries_the_index_and_stays_fixed(tmp_path, monkeypatch):
    from litetui.claude_turn import ledger_for, system_prompt_for

    repo = make_repo(tmp_path / "repo")
    app = make_app(monkeypatch, repo)
    ledger = ledger_for(app)
    segment = ledger.select_segment(str(repo))
    prompt = system_prompt_for(app, segment)
    assert appsvc.INDEX_HEADER in prompt and "INDEX-MARKER-7741" in prompt
    (repo / INDEX_NAME).write_text("CHANGED-LATER\n", encoding="utf-8")
    again = system_prompt_for(app, ledger.segment(segment["id"]))
    assert again == prompt, "a resume carries the segment's recorded prefix"

def test_cap_counts_raw_characters_so_a_crlf_file_is_over_the_cap_like_index_check(tmp_path, monkeypatch):
    # 4,500 CRLF lines = 13,500 raw chars (9,000 if each CRLF were folded to one char).
    repo = make_repo(tmp_path / "repo", text=None)
    (repo / INDEX_NAME).write_bytes((b"a" + bytes([13, 10])) * 4_500)
    app = make_app(monkeypatch, repo)
    notices = []
    app._system = notices.append
    content = app._request_messages()[0]["content"]
    assert "truncated" in content and any("13498" in n for n in notices)
