"""T536 — resolve_agent: name, prefix, and ambiguity."""
import json
from pathlib import Path

from litetui.harness import resolve_agent, AGENTS_DIR


def _write_agent(tmp_path, agent_id, name):
    d = tmp_path / "agents"
    d.mkdir(exist_ok=True)
    (d / f"{agent_id}.json").write_text(
        json.dumps({"agent_id": agent_id, "name": name}), encoding="utf-8"
    )
    return d


class TestResolve:
    def test_exact_id(self, tmp_path, monkeypatch):
        d = _write_agent(tmp_path, "aaaa-bbbb-cccc", "Alice")
        monkeypatch.setattr("litetui.harness.AGENTS_DIR", d)
        aid, err = resolve_agent("aaaa-bbbb-cccc")
        assert aid == "aaaa-bbbb-cccc"
        assert err == ""

    def test_exact_name(self, tmp_path, monkeypatch):
        d = _write_agent(tmp_path, "aaaa-bbbb-cccc", "Alice")
        monkeypatch.setattr("litetui.harness.AGENTS_DIR", d)
        aid, err = resolve_agent("Alice")
        assert aid == "aaaa-bbbb-cccc"

    def test_name_case_insensitive(self, tmp_path, monkeypatch):
        d = _write_agent(tmp_path, "aaaa-bbbb-cccc", "Alice")
        monkeypatch.setattr("litetui.harness.AGENTS_DIR", d)
        aid, err = resolve_agent("alice")
        assert aid == "aaaa-bbbb-cccc"

    def test_unique_prefix(self, tmp_path, monkeypatch):
        d = _write_agent(tmp_path, "aaaa-bbbb-cccc", "Alice")
        _write_agent(tmp_path, "xxxx-yyyy-zzzz", "Bob")
        monkeypatch.setattr("litetui.harness.AGENTS_DIR", d)
        aid, err = resolve_agent("aaaa")
        assert aid == "aaaa-bbbb-cccc"

    def test_ambiguous_prefix(self, tmp_path, monkeypatch):
        d = _write_agent(tmp_path, "aaaa-1111", "Alice")
        _write_agent(tmp_path, "aaaa-2222", "Bob")
        monkeypatch.setattr("litetui.harness.AGENTS_DIR", d)
        aid, err = resolve_agent("aaaa")
        assert aid is None
        assert "ambiguous" in err

    def test_unknown(self, tmp_path, monkeypatch):
        d = _write_agent(tmp_path, "aaaa-bbbb-cccc", "Alice")
        monkeypatch.setattr("litetui.harness.AGENTS_DIR", d)
        aid, err = resolve_agent("zzzz")
        assert aid is None
        assert "no agent matches" in err

    def test_empty_registry(self, tmp_path, monkeypatch):
        d = tmp_path / "agents"
        d.mkdir()
        monkeypatch.setattr("litetui.harness.AGENTS_DIR", d)
        aid, err = resolve_agent("anything")
        assert aid is None
        assert "no agents" in err
