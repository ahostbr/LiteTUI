"""Capture the same screens from Python LiteTUI (Textual) and Rust LiteTUI in hidden ConPTY.

Ryan, 2026-11-11: the Rust GUI must be a 1:1 copy of the Python LiteTUI. This drives both
frontends through the same keystrokes (slash commands, Esc) against the controlled provider of
target/final-performance-prep/performance_runtime.py (no real provider, network, model load,
desktop focus or screen capture) and writes each screen's text cells to
<out>/<frontend>/<NN>-<name>.txt plus index.json for a side-by-side page.

    python -X utf8 scripts/capture_parity_screens.py --binary <litetui-rs.exe> --out <dir>

Needs psutil and pywinpty in the parent interpreter (the global Python311 has both).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import shutil
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREP = ROOT / "target/final-performance-prep"
_spec = importlib.util.spec_from_file_location("compare_frontends_v4", PREP / "compare_frontends_v4.py")
cf = importlib.util.module_from_spec(_spec)
sys.modules["compare_frontends_v4"] = cf
_spec.loader.exec_module(cf)

# Same keystrokes for both frontends. Python LiteTUI binds no F-keys; every screen opens from a
# slash command, so slash commands are the common ground. Read-only commands only.
STEPS = [
    ("slash-menu", "/"),
    ("model", "/model\r"),
    ("backend", "/backend\r"),
    ("think", "/think\r"),
    ("settings", "/settings\r"),
    ("resume", "/resume\r"),
    ("help", "/help\r"),
    ("keys", "/keys\r"),
    ("tools", "/tools\r"),
    ("skills", "/skills\r"),
    ("hooks", "/hooks\r"),
    ("mcp", "/mcp\r"),
    ("tasks", "/tasks\r"),
    ("calendar", "/cal\r"),
    ("modelcfg", "/modelcfg\r"),
    ("glassbox", "/glassbox\r"),
]

# --profile settings (Ryan 2026-11-12: "let me see both"): Python's /settings has 12 tabs and a
# search box that activates the tab of the first match, so each tab is reached by typing its
# name. Rust's /settings is a hub whose letter keys open separate editors. Each step is
# (name, key writes, reset): "full" closes everything and clears the composer, "search" erases
# the typed search term and keeps the screen open.
PY_TABS = ["Model", "NInfer", "Strata", "Xolotl", "Voice", "Generation", "Agent loop", "Compaction",
           "Capabilities", "Hooks", "Themes", "Interface"]
RUST_EDITORS = [("model-prefs", "p"), ("agent", "a"), ("voice", "v"), ("interface", "u"), ("connection", "n"),
                ("compaction", "c"), ("mcp", "m"), ("sidecar", "s"), ("glassbox", "g"), ("lifecycle", "l"),
                ("status", "i")]
PROFILES = {
    "screens": {frontend: [(name, [keys], "full") for name, keys in STEPS] for frontend in ("textual", "rust")},
    "settings": {
        # Search focuses the first match, so tabs are stepped instead: Tab, Tab moves focus from the
        # search box past Clear to the tab bar, then each Right arrow is the next tab ("←/→ or
        # click to change tab"). NInfer appears only on a machine with NInfer installed.
        "textual": [("settings-open", ["/settings\r"], "none"), ("tab-01", ["\t", "\t"], "none")]
        + [(f"tab-{n:02d}", ["\x1b[C"], "none") for n in range(2, len(PY_TABS) + 1)],
        # Rust today needs a second Enter: "/settings" + Enter (completion popup open) leaves the
        # palette on the Settings row (parity bug, Python opens Settings at once).
        "rust": [("settings-hub", ["/settings\r", "\r"], "full")]
        + [(f"editor-{name}", ["/settings\r", key], "full") for name, key in RUST_EDITORS],
    },
}


def settle(screen_text, *, quiet=2.0, minimum=1.0, limit=60.0):
    """Wait until the decoded screen stops changing for `quiet` seconds."""
    start = time.monotonic()
    last, since = screen_text(), time.monotonic()
    while time.monotonic() - start < limit:
        time.sleep(0.25)
        now = screen_text()
        if now != last:
            last, since = now, time.monotonic()
        elif time.monotonic() - since >= quiet and time.monotonic() - start >= minimum:
            return now
    return last


def run(frontend, args):
    from winpty import Backend, PtyProcess
    base = args.out / frontend
    base.mkdir(parents=True)
    env = cf.child_environment(base, args.source, frontend)
    workspace = base / "workspace"
    workspace.mkdir()
    env.update(LITETUI_PERF_CHUNKS="8", LITETUI_PERF_DELAY="0.001")  # the runtime accepts .001-.1 s
    if args.all_plugins:
        env["LITETUI_PERF_ALL_PLUGINS"] = "1"  # child_environment drops inherited LITETUI_* keys
    common = ["--backend", "custom", "--model", "perf-fixture", "--tool-profile", "strict", "--thinking-level",
              "default", "--server-mode", "connect", "--base-url", "http://127.0.0.1:45120/v1"]
    if frontend == "rust":
        module = base / "overlay/src/litetui"
        module.mkdir(parents=True)
        (module / "__init__.py").write_text("__path__.append(" + repr(str(args.source / "src/litetui")) + ")\n",
                                            encoding="utf-8")
        (module / "cli.py").write_text("import runpy\nrunpy.run_path(" + repr(str(PREP / "performance_runtime.py"))
                                       + ",run_name='__main__')\n", encoding="utf-8")
        command = [str(args.binary), "--interactive", "--python", str(args.python), "--source", str(base / "overlay"),
                   "--data-root", str(base / "data"), "--workspace", str(workspace), "--tools", "off",
                   "--timeout", "120", *common]
    else:
        command = [str(args.python), "-B", str(PREP / "performance_runtime.py"), "--cwd", str(workspace), *common]
    pty = PtyProcess.spawn(command, cwd=str(workspace), env=env, dimensions=(args.rows, args.cols),
                           backend=Backend.ConPTY)
    screen = cf.TerminalCells(args.cols, args.rows)
    lock = threading.Lock()
    errors = []

    def reader():
        try:
            while True:
                text = pty.read(65536)
                with lock:
                    screen.feed(text)
        except EOFError:
            pass
        except BaseException as error:  # noqa: BLE001 - recorded, the capture is then marked failed
            errors.append(repr(error))

    thread = threading.Thread(target=reader, name=f"parity-{frontend}")
    thread.start()

    def text():
        with lock:
            return screen.text()

    shots = []

    def shoot(name):
        grid = settle(text)
        path = base / f"{len(shots):02d}-{name}.txt"
        path.write_text(grid, encoding="utf-8")
        shots.append({"name": name, "file": path.name})

    try:
        settle(text, quiet=4.0, minimum=8.0, limit=args.startup)
        shoot("start")
        for name, writes, reset in PROFILES[args.profile][frontend]:
            if not pty.isalive():
                errors.append(f"frontend exited before step {name}")
                break
            for index, keys in enumerate(writes):
                if index:
                    settle(text, quiet=1.5, limit=30)
                pty.write(keys)
            shoot(name)
            if reset == "full":
                for _ in range(3):  # close the screen(s), then clear the composer (draft never sent)
                    pty.write("\x1b")
                    time.sleep(0.6)
                pty.write("\x08" * 16)
            elif reset == "search":
                pty.write("\x08" * (len(writes[-1]) + 2))
            settle(text, quiet=1.0, limit=15)
        pty.write("\x11")  # Ctrl+Q
        deadline = time.monotonic() + 30
        while pty.isalive() and time.monotonic() < deadline:
            time.sleep(0.25)
    finally:
        if pty.isalive():
            errors.append("frontend did not exit on Ctrl+Q; terminated")
            pty.terminate(force=True)
        cleanup = cf.finish_reader(pty, thread)
    (base / "index.json").write_text(json.dumps({"frontend": frontend, "shots": shots, "errors": errors,
                                                 "cleanup": cleanup}, indent=1), encoding="utf-8")
    return shots, errors


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--python", type=Path,
                        default=Path(os.environ.get("LITETUI_PYTHON") or shutil.which("python") or "python"))
    parser.add_argument("--source", type=Path, default=ROOT)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cols", type=int, default=140)
    parser.add_argument("--rows", type=int, default=45)
    parser.add_argument("--startup", type=float, default=240.0)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="screens")
    parser.add_argument("--frontends", default="textual,rust", help="comma list of textual, rust")
    parser.add_argument("--all-plugins", action="store_true",
                        help="load every plugin in the controlled runtime (needed for /settings)")
    args = parser.parse_args()
    # The children run with their own working directory, so every path they get is absolute.
    args.out, args.binary, args.source = args.out.resolve(), args.binary.resolve(), args.source.resolve()
    assert not args.out.exists(), args.out
    args.out.mkdir(parents=True)
    result = {}
    for frontend in args.frontends.split(","):
        shots, errors = run(frontend, args)
        result[frontend] = {"shots": len(shots), "errors": errors}
        print(json.dumps({frontend: result[frontend]}), flush=True)
    return 0 if not any(r["errors"] for r in result.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
