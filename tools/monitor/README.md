# Browser monitor → Grafana

Watch web targets **from the outside** (no instrumentation on the target side)
using LiteTUI's real Chrome, and feed the results to Grafana.

```
 Chrome (real browser)
   ▲ driven by
   │
   └── litetui/monitor  (Python engine)
          │  one sweep = N targets, each measured
          ▼
   metrics:  browser_monitor_up / _load_seconds / _text_chars
             browser_monitor_expect_met / _changed
          │  pushed to
          ▼
   Pushgateway (:9091)  ← scrape →  Prometheus (:9090)  ← query →  Grafana (:3000)
```

Everything below the browser is dependency-light and fully unit-tested; the
browser is the only live, slow, non-deterministic piece, and it is isolated
behind a `BrowserProbe` interface so the rest runs in tests with a fake.

---

## 1. Run the POC with no browser at all

This proves the pipeline end-to-end (probe → metrics → file sink → report)
using a fake probe. Nothing to install, no Chrome, no network:

```
python -m litetui.monitor.cli demo --json
```

You get a per-target report and a machine-readable JSON summary, plus a
`monitor-log.jsonl` the sink wrote.

## 2. Run a real sweep (needs Chrome)

Prereqs: the LiteTUI chrome-bridge extension loaded in Chrome and the relay
running (LiteTUI does this itself via `chrome` action="start"). Then:

```
# write a default config
python -m litetui.monitor.cli init --config monitor-targets.json
# or copy the shipped one:
copy tools\monitor\monitor-targets.json monitor-targets.json

# file sink (default) — durable JSONL, no Grafana needed
python -m litetui.monitor.cli run --config monitor-targets.json --json

# pushgateway sink — for Grafana (needs the compose stack below)
python -m litetui.monitor.cli run --config monitor-targets.json --sink pushgateway
```

A sweep is safe to run on a schedule; a bad target never aborts the rest.

## 3. From inside LiteTUI

The monitor is registered as a `/monitor` command:

```
/monitor demo        # safe default: fake probe, no browser
/monitor targets     # list the configured targets
/monitor init        # write a default config
/monitor             # real sweep over the default config (off the UI thread)
/monitor --config C:\path\targets.json
```

The real sweep runs in a background thread and posts its report back as a
system message, so the TUI stays responsive.

## 4. Bring up Grafana + Prometheus

```
cd tools/monitor
docker compose up -d
```

| Service     | URL                     | Login     |
|-------------|-------------------------|-----------|
| Grafana     | http://localhost:3000   | admin/admin |
| Prometheus  | http://localhost:9090   | —         |
| Pushgateway | http://localhost:9091   | —         |

The Prometheus datasource and the **Browser monitor** dashboard are
provisioned automatically from `docker/grafana/provisioning` — no manual
Grafana setup. Alert rules (target down, slow load, missing content, visual
regression) are loaded from `docker/prometheus/rules/monitor.rules.yml` and
show under Prometheus → Alerts.

Then run a sweep against the pushgateway and watch the dashboard populate:

```
python -m litetui.monitor.cli run --config monitor-targets.json --sink pushgateway --pushgateway http://localhost:9091
```

---

## Metrics reference

All gauges, labelled with `job=browser-monitor`, `name=<target>`, `url=<target url>`.

| Metric                          | Meaning                                        |
|---------------------------------|------------------------------------------------|
| `browser_monitor_up`            | 1 = last visit succeeded, 0 = errored          |
| `browser_monitor_load_seconds`  | page load time (wall-clock around navigation)  |
| `browser_monitor_text_chars`    | characters of visible text captured            |
| `browser_monitor_expect_met`    | 1 = configured `expect_text` found on the page |
| `browser_monitor_changed`       | 1 = screenshot differs from the stored baseline|

## Config format

```json
{
  "targets": [
    { "name": "example", "url": "https://example.com",
      "expect_text": "Example Domain", "screenshot": true,
      "text_selector": null, "timeout_ms": 30000 }
  ]
}
```

- `expect_text` — optional; visit is flagged `expect_met=false` when the page
  loaded but the substring is absent (a silent content regression).
- `screenshot` — capture a viewport PNG and fingerprint it for visual
  regression (first capture sets the baseline, stored in `monitor-baselines.json`).

## Layout

```
src/litetui/monitor/        # the engine (pure, stdlib-only, unit-tested)
  targets.py                # Target dataclass + config loading (JSON/TOML)
  probe.py                  # BrowserProbe: ChromeProbe (real) + FakeProbe
  metrics.py                # Prometheus rendering + FileSink / PushgatewaySink
  regression.py             # SHA-256 fingerprint + baseline state machine
  engine.py                 # MonitorEngine: one sweep over N targets
  report.py                 # human report + JSON
  cli.py                    # `monitor init|targets|run|demo`
src/litetui/plugins/monitor_plugin.py   # /monitor TUI command
tools/monitor/              # Grafana/Prometheus/Pushgateway compose stack
```

## Notes & known limits

- Load time is wall-clock around navigation — a coarse proxy, not Performance
  API LCP. Good for trend + spike detection, not for millisecond claims.
- Visual regression is a whole-capture SHA-256: it flags "something changed",
  not "which pixels". Inspect the PNG to see what.
- The real sweep needs the Chrome extension; `demo` and every test do not.
- The monitor must run where Chrome exists (the host); the compose stack is
  just the visualization/sink side.
