"""Target config: parsing, validation, and the Prometheus label shape.

These are the ground rules for what the monitor is pointed at. A bad URL
caught here (at load) is better than a bad URL discovered mid-sweep, and the
label shape is asserted because it is the contract the metrics renderer and the
Grafana dashboard both depend on.
"""

from __future__ import annotations

import json

import pytest

from litetui.monitor.targets import Target, default_config, load_targets


def test_target_requires_http_url():
    with pytest.raises(ValueError):
        Target(name="x", url="not-a-url")
    with pytest.raises(ValueError):
        Target(name="x", url="ftp://example.com")


def test_target_requires_name():
    with pytest.raises(ValueError):
        Target(name="", url="https://example.com")


def test_target_defaults():
    t = Target(name="a", url="https://a.test")
    assert t.screenshot is True
    assert t.expect_text is None
    assert t.timeout_ms == 30000
    assert t.as_labels() == {"name": "a", "url": "https://a.test"}


def test_load_targets_json(tmp_path):
    cfg = tmp_path / "t.json"
    cfg.write_text(
        json.dumps(
            {
                "targets": [
                    {"name": "example", "url": "https://example.com",
                     "expect_text": "Example Domain"},
                    {"name": "bare", "url": "https://bare.test"},
                ]
            }
        ),
        encoding="utf-8",
    )
    ts = load_targets(cfg)
    assert [t.name for t in ts] == ["example", "bare"]
    assert ts[0].expect_text == "Example Domain"
    assert ts[0].screenshot is True
    assert ts[1].screenshot is True


def test_load_targets_toml(tmp_path):
    cfg = tmp_path / "t.toml"
    cfg.write_text(
        'targets = [\n'
        '  { name = "example", url = "https://example.com", '
        '    expect_text = "Example Domain" }\n'
        "]\n",
        encoding="utf-8",
    )
    ts = load_targets(cfg)
    assert ts[0].name == "example"
    assert ts[0].expect_text == "Example Domain"


def test_load_targets_empty_file_is_valid(tmp_path):
    cfg = tmp_path / "empty.json"
    cfg.write_text("{}", encoding="utf-8")
    assert load_targets(cfg) == []


def test_load_targets_rejects_bad_url_in_file(tmp_path):
    cfg = tmp_path / "t.json"
    cfg.write_text(
        json.dumps({"targets": [{"name": "bad", "url": "notaurl"}]}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
        load_targets(cfg)


def test_default_config_is_loadable(tmp_path):
    cfg = tmp_path / "d.json"
    cfg.write_text(json.dumps(default_config()), encoding="utf-8")
    ts = load_targets(cfg)
    assert len(ts) >= 1
    assert all(t.screenshot for t in ts)
