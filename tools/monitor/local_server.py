"""Local, no-Docker monitor dashboard.

Runs the REAL MonitorEngine on a timer against a Chrome probe and serves a
self-refreshing HTML dashboard over localhost. This is the native stand-in for
the docker-compose Grafana/Prometheus/Pushgateway stack on a machine without
Docker: same five metrics, live, with the actual captured screenshots.

    python tools/monitor/local_server.py --port 8090 --interval 5

    then open  http://localhost:8090

It needs the LiteTUI chrome-bridge extension loaded and the relay running
(LiteTUI does that itself via `chrome` action="start").
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from litetui.monitor import ChromeProbe, FileSink, MonitorEngine, RegressionTracker, load_targets

HERE = Path(__file__).resolve().parent


class History:
    """Rolling in-memory store of sweep results, safe across threads."""

    def __init__(self, maxlen: int = 300) -> None:
        self._lock = threading.Lock()
        self._series: dict[str, deque] = {}
        self._latest: dict[str, dict] = {}
        self._last_run: float | None = None
        self._maxlen = maxlen

    def record(self, results) -> None:
        now = time.time()
        with self._lock:
            self._last_run = now
            for r in results:
                vals = {
                    "up": 1.0 if r.ok else 0.0,
                    "load_s": r.load_ms / 1000.0,
                    "text_chars": r.text_chars,
                    "expect_met": 1.0 if r.expect_met else 0.0,
                    "changed": 1.0 if r.regression == "changed" else 0.0,
                }
                self._latest[r.target] = vals
                self._series.setdefault(r.target, deque(maxlen=self._maxlen)).append(
                    [round(now, 1), vals]
                )

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "last_run": self._last_run,
                "latest": dict(self._latest),
                "series": {t: list(d) for t, d in self._series.items()},
            }


HIST = History()


def sweep_loop(interval: float, targets, probe, tracker, sink) -> None:
    engine = MonitorEngine(probe=probe, sink=sink, tracker=tracker)
    while True:
        try:
            HIST.record(engine.run_sweep(targets))
        except Exception as exc:  # noqa: BLE001 - a sweep error must not kill the server
            print(f"[sweep] error: {type(exc).__name__}: {exc}")
        time.sleep(interval)


class Handler(BaseHTTPRequestHandler):
    shot_dir: Path = Path(".")

    def log_message(self, *a) -> None:  # silence per-request logging
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/index.html"):
            self._send(200, DASHBOARD_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif self.path == "/metrics":
            self._send(200, json.dumps(HIST.snapshot()).encode(), "application/json")
        elif self.path.startswith("/shots/"):
            name = Path(self.path[len("/shots/"):]).name  # strip any path tricks
            f = self.shot_dir / name
            if f.exists():
                self._send(200, f.read_bytes(), "image/png")
            else:
                self._send(404, b"not found", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")


DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Browser monitor</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  body{font-family:system-ui,Segoe UI,Roboto,sans-serif;margin:0;background:#0b0e14;color:#e6e6e6}
  header{padding:16px 24px;border-bottom:1px solid #222;display:flex;justify-content:space-between;align-items:center}
  h1{font-size:18px;margin:0;font-weight:600}
  #status{font-size:12px;color:#8ab}
  .wrap{padding:16px 24px;display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}
  .card{background:#12161f;border:1px solid #232838;border-radius:8px;padding:12px}
  .card h3{margin:0 0 8px;font-size:13px;color:#aab;font-weight:600}
  .shots{display:flex;gap:10px;flex-wrap:wrap;padding:0 24px 24px}
  .shot{background:#12161f;border:1px solid #232838;border-radius:8px;padding:8px;width:280px}
  .shot img{width:100%;border-radius:4px;display:block}
  .shot .lbl{font-size:12px;margin-top:6px;display:flex;justify-content:space-between}
  .ok{color:#4caf50}.bad{color:#ef5350}.chg{color:#ffb74d}
  canvas{max-height:180px}
</style></head>
<body>
<header><h1>Browser monitor <span style="color:#5a6b8a;font-weight:400">/ local, no-Docker</span></h1>
  <div id="status">connecting…</div></header>
<div class="wrap" id="charts"></div>
<div class="shots" id="shots"></div>
<script>
const METRICS = [
  {key:'up',        title:'Targets up (1 = up)',      type:'line',   min:0, max:1},
  {key:'load_s',    title:'Page load time (s)',        type:'line'},
  {key:'expect_met',title:'Expectation met (1 = yes)', type:'line',   min:0, max:1},
  {key:'changed',   title:'Visual regression (1 = changed)', type:'bar', min:0, max:1},
  {key:'text_chars',title:'Visible text (chars)',      type:'line'},
];
const charts = {};
function buildCards(){
  const wrap = document.getElementById('charts');
  METRICS.forEach(m=>{
    const c = document.createElement('div'); c.className='card';
    c.innerHTML = `<h3>${m.title}</h3><canvas></canvas>`;
    wrap.appendChild(c);
    const ctx = c.querySelector('canvas');
    charts[m.key] = new Chart(ctx, {type:m.type, data:{labels:[],datasets:[]},
      options:{animation:false,responsive:true,
        scales:{y:(m.min!==undefined)?{min:m.min,max:m.max}:{},x:{display:false}},
        plugins:{legend:{labels:{boxWidth:10,font:{size:10}}}},
        elements:{point:{radius:1}}}});
  });
}
const COLORS=['#42a5f5','#ef5350','#66bb6a','#ffa726','#ab47bc','#26c6da'];
function ts(x){return new Date(x*1000).toLocaleTimeString();}
function refresh(){
  fetch('/metrics').then(r=>r.json()).then(d=>{
    document.getElementById('status').textContent =
      d.last_run ? `last sweep ${ts(d.last_run)}` : 'waiting for first sweep…';
    const targets = Object.keys(d.series);
    METRICS.forEach((m,mi)=>{
      const ch = charts[m.key];
      ch.data.labels = targets[0] ? d.series[targets[0]].map(p=>ts(p[0])) : [];
      ch.data.datasets = targets.map((t,ti)=>({
        label:t, type:m.type,
        data: d.series[t].map(p=>p[1][m.key]),
        borderColor:COLORS[ti%COLORS.length],
        backgroundColor:m.type==='bar' ? COLORS[ti%COLORS.length]+'88' : 'transparent',
        tension:.2, fill:false,
      }));
      ch.update('none');
    });
    // screenshots
    const shots = document.getElementById('shots');
    shots.innerHTML = '';
    targets.forEach(t=>{
      const l = d.latest[t]||{};
      const div = document.createElement('div'); div.className='shot';
      const cls = l.up===1?'ok':'bad';
      div.innerHTML = `<img src="/shots/${t}.png" alt="${t}">
        <div class="lbl"><span>${t}</span>
        <span class="${cls}">${l.up===1?'up':'DOWN'}</span>
        <span class="${l.changed===1?'chg':''}">${l.changed===1?'changed':'stable'}</span></div>`;
      shots.appendChild(div);
    });
  }).catch(()=>{ document.getElementById('status').textContent='reconnecting…'; });
}
buildCards(); refresh(); setInterval(refresh, 5000);
</script>
</body></html>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Local no-Docker monitor dashboard")
    ap.add_argument("--port", type=int, default=8090)
    ap.add_argument("--interval", type=float, default=5.0, help="seconds between sweeps")
    ap.add_argument("--config", default=str(HERE / "monitor-targets.json"))
    ap.add_argument("--shots", default="monitor-shots")
    ap.add_argument("--state", default="monitor-baselines.json")
    ap.add_argument("--log", default="monitor-log.jsonl")
    ap.add_argument("--open", action="store_true", help="open the dashboard in a browser")
    args = ap.parse_args(argv)

    targets = load_targets(args.config)
    shot_dir = Path(args.shots)
    shot_dir.mkdir(parents=True, exist_ok=True)
    probe = ChromeProbe(shot_dir=shot_dir)
    tracker = RegressionTracker(args.state)
    sink = FileSink(args.log)

    Handler.shot_dir = shot_dir
    threading.Thread(target=sweep_loop,
                     args=(args.interval, targets, probe, tracker, sink),
                     daemon=True).start()

    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://localhost:{args.port}"
    print(f"browser monitor live at {url}  (sweeping {len(targets)} target(s) every {args.interval:g}s)")
    print("Ctrl-C to stop.")
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
