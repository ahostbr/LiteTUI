"""Metrics: Prometheus text rendering and the two sinks.

The renderer is the load-bearing part: a malformed exposition line silently
breaks the Pushgateway scrape, and unescaped label values are a security footgun
(label injection). So the tests pin the exact bytes of a rendered series, the
escape rules, and the "collapse to latest per series" semantic that makes a
gauge correct across repeated sweeps.
"""

from __future__ import annotations

import json

from litetui.monitor.metrics import (
    FileSink,
    Metric,
    PushgatewaySink,
    render_prometheus,
)


def _m(name, value, labels=None, help=""):
    return Metric(name, value, labels or {}, help=help)


def test_single_metric_without_labels():
    out = render_prometheus([_m("up", 1.0, help="")])
    lines = out.strip().splitlines()
    # Empty help text: the HELP line is skipped, only TYPE precedes the sample.
    assert "# HELP" not in out
    assert lines[0] == "# TYPE up gauge"
    assert lines[1] == "up 1.0"


def test_help_line_rendered_when_present():
    out = render_prometheus([_m("up", 1.0, help="Is it up?")])
    lines = out.strip().splitlines()
    assert lines[0] == "# HELP up Is it up?"
    assert lines[1] == "# TYPE up gauge"
    assert lines[2] == "up 1.0"


def test_labeled_series_escapes_and_sorts_labels():
    m = _m("browser_monitor_up", 1.0,
           {"name": "example", "url": "https://example.com"})
    out = render_prometheus([m]).strip().splitlines()
    sample = [l for l in out if l.startswith("browser_monitor_up{")][0]
    # Labels are sorted by key: name before url.
    assert sample == (
        'browser_monitor_up{name="example",'
        'url="https://example.com"} 1.0'
    )


def test_label_value_escaping():
    m = _m("m", 0.0, {"note": 'a"b\\c\nd'})
    out = render_prometheus([m]).strip().splitlines()
    sample = [l for l in out if l.startswith("m{")][0]
    assert 'note="a\\"b\\\\c\\nd"' in sample


def test_repeated_series_collapses_to_latest():
    out = render_prometheus(
        [
            _m("load", 0.5, {"name": "a"}),
            _m("load", 0.9, {"name": "a"}),
            _m("load", 0.1, {"name": "b"}),
        ]
    )
    lines = [l for l in out.strip().splitlines() if l.startswith("load{")]
    # Exactly one line per (name, labels) even though "a" was seen twice.
    assert len(lines) == 2
    assert 'load{name="a"} 0.9' in lines      # latest wins
    assert 'load{name="b"} 0.1' in lines


def test_floats_render_canonically():
    assert "1.0" in render_prometheus([_m("m", 1.0)])
    assert "0.25" in render_prometheus([_m("m", 0.25)])


def test_empty_metrics_renders_empty():
    assert render_prometheus([]) == ""


def test_invalid_metric_name_rejected():
    import pytest
    with pytest.raises(ValueError):
        Metric("9bad", 1.0, {})


def test_pushgateway_url_shape():
    sink = PushgatewaySink(base_url="http://host:9091/", job="job",
                           group="grp", instance="inst")
    assert sink.url() == "http://host:9091/metrics/job/job/group/grp"


def test_pushgateway_uses_injected_post(tmp_path):
    calls = []
    sink = PushgatewaySink(base_url="http://host:9091")
    sink._post = lambda url, body: calls.append((url, body))
    body = render_prometheus([_m("up", 1.0)])
    sink.emit(body, [_m("up", 1.0)])
    assert len(calls) == 1
    assert calls[0][0].endswith("/metrics/job/browser-monitor/group/monitor")
    assert "up 1.0" in calls[0][1]


def test_file_sink_appends_jsonl(tmp_path):
    path = tmp_path / "log.jsonl"
    sink = FileSink(path)
    body = render_prometheus([_m("up", 1.0, {"name": "a"})])
    sink.emit(body, [_m("up", 1.0, {"name": "a"})])
    sink.emit(body, [_m("up", 0.0, {"name": "a"})])
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["metrics"][0] == {"name": "up", "value": 1.0, "labels": {"name": "a"}}
    assert 'up{name="a"} 1.0' in first["prometheus"]
    second = json.loads(lines[1])
    assert second["metrics"][0]["value"] == 0.0
