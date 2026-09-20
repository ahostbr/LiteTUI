"""The CLI verbs that need no browser: init, targets, demo.

`run` is not tested here — it needs a live Chrome extension. What IS tested is
the config lifecycle (init writes a valid file, targets reads it back) and the
demo, which proves the whole metrics->sink->report pipeline produces output
without any browser. All writes go to a chdir'd tmp_path so nothing lands in
the checkout root.
"""

from __future__ import annotations

import json

from litetui.monitor import cli


def test_init_writes_a_loadable_config(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "t.json"
    rc = cli.main(["init", "--config", str(cfg)])
    assert rc == 0
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["targets"]
    # And it round-trips through the targets command.
    cli.main(["targets", "--config", str(cfg)])
    out = capsys.readouterr().out
    assert "example" in out


def test_init_refuses_to_overwrite_without_force(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "t.json"
    cli.main(["init", "--config", str(cfg)])
    try:
        cli.main(["init", "--config", str(cfg)])
        raised = False
    except SystemExit:
        raised = True
    assert raised, "second init without --force should refuse"
    # ...but --force allows it.
    assert cli.main(["init", "--config", str(cfg), "--force"]) == 0


def test_targets_missing_config_exits(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    try:
        cli.main(["targets", "--config", str(tmp_path / "nope.json")])
        raised = False
    except SystemExit:
        raised = True
    assert raised


def test_demo_produces_a_report_and_a_log(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["demo", "--log", str(tmp_path / "log.jsonl"), "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "demo" in out
    assert "up" in out
    # The demo writes the real file sink, proving the pipeline below the browser.
    log = tmp_path / "log.jsonl"
    assert log.exists()
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2  # one record per demo target
    json.loads(lines[0])  # each line is valid JSON
