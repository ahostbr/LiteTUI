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
# name. Each step is
# (name, key writes, reset): "full" closes everything and clears the composer, "search" erases
# the typed search term and keeps the screen open.
PY_TABS = ["Model", "NInfer", "Strata", "Xolotl", "Voice", "Generation", "Agent loop", "Compaction",
           "Capabilities", "Hooks", "Themes", "Interface"]
PROFILES = {
    "screens": {frontend: [(name, [keys], "full") for name, keys in STEPS] for frontend in ("textual", "rust")},
    # Search focuses the first match, so tabs are stepped instead: Tab, Tab moves focus from the
    # search box past Clear to the tab bar, then each Right arrow is the next tab ("←/→ or click
    # to change tab"). NInfer appears only on a machine with NInfer installed. Rust's /settings
    # draws Python's own dialog (native_settings_form), so both frontends get the same keys.
    "settings": {frontend: [("settings-open", ["/settings\r"], "none"), ("tab-01", ["\t", "\t"], "none")]
                 + [(f"tab-{n:02d}", ["\x1b[C"], "none") for n in range(2, len(PY_TABS) + 1)]
                 for frontend in ("textual", "rust")},
    # MCP at start from the saved setting (Ryan 2026-11-13, "From saved setting"): with --mcp
    # the fixture saves mcp_enabled on; the MCP servers section of /settings shows the switch.
    "mcpstart": {frontend: [("settings-open", ["/settings\r"], "none"),
                            ("tab", ["\t", "\t"] + ["\x1b[C"] * 7, "none"),
                            ("mcp-section", ["click:MCP servers"], "none")]
                 for frontend in ("textual", "rust")},
    # Ryan 2026-11-12: "the /backend screen can't be navigated with the arrow keys, and Enter
    # doesn't confirm or close it". Arrow keys are ESC [ B (down) and ESC [ A (up).
    "backend": {frontend: [("backend-open", ["/backend\r"], "none"), ("down-1", ["\x1b[B"], "none"),
                           ("down-2", ["\x1b[B"], "none"), ("enter", ["\r"], "none")]
                for frontend in ("textual", "rust")},
    # Parity item 4: an unknown slash command. Python says "Unknown: /nope — try /help" in the
    # chat log and opens nothing.
    "unknown": {frontend: [("unknown", ["/nope\r"], "none")] for frontend in ("textual", "rust")},
    # /agent-new relaunches the window as the new agent; /resume then lists both agents.
    "agentswitch": {frontend: [("agent-new", ["/agent-new Nova\r"], "none"), ("resume", ["/resume\r"], "none")]
                    for frontend in ("textual", "rust")},
    # The theme creator's colour picker opens on a CLICK on a token field (settings_screen
    # on_click). Themes is reached from the tab bar by Left twice (the tabs wrap); the rest click
    # by on-screen text: "click:<text>" presses the left button on the first cell of the first
    # row showing <text>; "click:<text>@<dy>" clicks <dy> rows below it (an input's text line is
    # two rows under its label).
    # /tools: a tick applies at once. Tab twice focuses the first tool, powershell (the Switch
    # and then the scroll region are focus stops first), and Space ticks it; a click on bash's
    # box ticks that one, and one on the Switch's top edge turns tools on (the fixture starts
    # with them off). The data root's settings then say what was saved.
    "tooltick": {frontend: [("tools-open", ["/tools\r"], "none"), ("space", ["\t", "\t", " "], "none"),
                            ("click", ["click:▐X▌ bash"], "none"),
                            ("switch", ["click:▊▔▔▔▔▔▔▔▔▎"], "none")]
                 for frontend in ("textual", "rust")},
    # Ctrl+T (\x14) and Shift+Tab (ESC [ Z) toggle at once, with no page (Ryan 2026-11-12,
    # via Sentinel: "Ctrl+T, no page"). Each shot is the chat after one key.
    "quickkeys": {frontend: [("ctrl-t", ["\x14"], "none"), ("shift-tab", ["\x1b[Z"], "none")]
                  for frontend in ("textual", "rust")},
    # Plan mode changes at once too: Ctrl+P (\x10) toggles, /plan toggles, /plan on sets (a
    # second /plan on changes and says nothing), and a bad argument gets _cmd_plan's line.
    "plankeys": {frontend: [("ctrl-p", ["\x10"], "none"), ("plan", ["/plan\r"], "none"),
                            ("plan-on", ["/plan on\r"], "none"), ("plan-on-again", ["/plan on\r"], "none"),
                            ("plan-bad", ["/plan maybe\r"], "none")]
                 for frontend in ("textual", "rust")},
    # /skills with skills on (--skills): Python's "Skills (N)" picker, Down onto the first
    # skill, Up and Enter on the Full report row (the report goes to the chat), then /skills,
    # Down and Enter loads the first skill and sends it.
    "skills": {frontend: [("skills-open", ["/skills\r"], "none"), ("down", ["\x1b[B"], "none"),
                          ("report", ["\x1b[A", "\r"], "none"),
                          ("enter", ["/skills\r", "\x1b[B", "\r"], "none")]
               for frontend in ("textual", "rust")},
    # The transcript cards: the fixture provider streams its reply to a prompt starting with PERF
    # and fails any other ("Something went wrong talking to the model server.").
    "turns": {frontend: [("normal", ["PERF hello\r"], "none"), ("failed", ["not a fixture prompt\r"], "none")]
              for frontend in ("textual", "rust")},
    # /hooks is Python's HooksEditor: New adds new-hook (unsaved), the wheel scrolls the editor,
    # Save hooks writes the project hooks.json. Each shot is after one step.
    "hooksedit": {frontend: [("hooks-open", ["/hooks\r"], "none"), ("new", ["click:New"], "none"),
                             ("wheel", ["wheel:Close@-5"] * 25, "none"),
                             ("save", ["click:Save hooks"], "none")]
                  for frontend in ("textual", "rust")},
    # /mcp is Python's MCPListBody: type a name and a loopback URL on a closed port (the
    # connect fails at once, nothing is spawned), Tab to Add (it sits off-screen at x165 at
    # 140 columns) and press it, then Close. The fixture runs with MCP off, so the new server is
    # "excluded" in both (no row buttons); .mcp.json lands in the data root. --mcp turns MCP on,
    # but the Rust launch (--mcp-lifecycle-version 1) starts every session with MCP off.
    "mcpedit": {frontend: [("mcp-open", ["/mcp\r"], "none"),
                           ("typed", ["click:name", "fixmcp", "click:https://host/mcp", "http://127.0.0.1:9/mcp"],
                            "none"),
                           ("added", ["\t", "\r"], "none"),
                           ("closed", ["click:Close"], "none")]
                for frontend in ("textual", "rust")},
    # /tasks is Python's own command: it answers in the chat log and opens nothing. Every verb
    # (bare, list, tail and kill of an unknown id, a bad verb) runs Python's handler.
    "tasksverbs": {frontend: [("bare", ["/tasks\r"], "none"), ("list", ["/tasks list\r"], "none"),
                              ("tail", ["/tasks tail nope\r"], "none"), ("kill", ["/tasks kill nope\r"], "none"),
                              ("bad", ["/tasks bogus\r"], "none")]
                   for frontend in ("textual", "rust")},
    # /cal is Python's CalendarScreen: click today (marked with a dot), the day popup's
    # n opens the new-job editor, Escape twice back to the month, Right to next month, q.
    "calday": {frontend: [("cal-open", ["/cal\r"], "none"), ("day", ["click:•"], "none"),
                          ("job", ["n"], "none"), ("job-back", ["\x1b"], "none"), ("day-back", ["\x1b"], "none"),
                          ("next-month", ["\x1b[C"], "none"), ("closed", ["q"], "none")]
               for frontend in ("textual", "rust")},
    # A job made through /cal: today's popup, n, type the prompt, Enter saves (JobBody's
    # Input.Submitted), the day lists it, Escape shows it in the month and the JOBS pane.
    # The job store in the data root then says what was saved.
    "calsave": {frontend: [("cal-open", ["/cal\r"], "none"), ("day", ["click:•"], "none"),
                           ("job", ["n"], "none"), ("typed", ["calendar parity job"], "none"),
                           ("saved", ["\r"], "none"), ("month", ["\x1b"], "none"),
                           ("closed", ["q"], "none")]
                for frontend in ("textual", "rust")},
    # /modelcfg is Python's ModelConfigScreen: click the Load tab, wheel its pane, click
    # Inference, then Escape. NEVER Ctrl+S here: applying reloads a loaded model.
    "modelcfgtabs": {frontend: [("modelcfg-open", ["/modelcfg\r"], "none"), ("load", ["click: Load "], "none"),
                                ("wheel", ["wheel:Context Length"] * 3, "none"),
                                ("inference", ["click: Inference "], "none"), ("closed", ["\x1b"], "none")]
                     for frontend in ("textual", "rust")},
    # /glassbox is Python's own command, answered in the chat: status, a bad verb (its usage
    # line), bare (start: the local feed and the recorder), stop.
    "glassboxverbs": {frontend: [("status", ["/glassbox status\r"], "none"),
                                 ("bad", ["/glassbox bogus\r"], "none"),
                                 ("start", ["/glassbox\r"], "none"), ("stop", ["/glassbox stop\r"], "none")]
                      for frontend in ("textual", "rust")},
    # The palette's Theme row is Textual's own theme search (App.search_themes): the header's
    # palette icon, "Theme", Enter, then Down and Enter picks a theme (applied, closed).
    "themepick": {frontend: [("palette", ["click:⭘"], "none"), ("typed", ["Theme"], "none"),
                             ("themes", ["\r"], "none"), ("down", ["\x1b[B"], "none"),
                             ("picked", ["\r"], "none")]
                  for frontend in ("textual", "rust")},
    "picker": {frontend: [("settings-open", ["/settings\r"], "none"), ("themes", ["\t", "\t", "\x1b[D", "\x1b[D"], "none"),
                          ("tokens", ["click:Palette tokens"], "none"),
                          ("scrolled", ["wheel:New theme name"] * 4, "none"),
                          ("picker-open", ["click: primary@2"], "none"), ("nudge", ["\x1b[A", "\x1b[C"], "none"),
                          ("accept", ["\r"], "none")]
               for frontend in ("textual", "rust")},
}


def click(screen_text, spec):
    """The SGR mouse bytes for a "click:<text>[@dy]" step (left press and release) or a
    "wheel:<text>[@dy]" step (one wheel-down notch), or None if <text> is not shown."""
    verb, _, rest = spec.partition(":")
    text, _, dy = rest.partition("@")
    for row, line in enumerate(screen_text().splitlines()):
        col = line.find(text)
        if col >= 0:
            x, y = col + 1 + (1 if text.startswith(" ") else 0), row + 1 + int(dy or 0)
            if verb == "wheel":
                return f"\x1b[<65;{x};{y}M"
            return f"\x1b[<0;{x};{y}M\x1b[<0;{x};{y}m"
    return None


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
    if args.skills:
        env["LITETUI_PERF_SKILLS"] = "1"  # skills on, two fixture skills in the private data root
    if args.mcp:
        env["LITETUI_PERF_MCP"] = "1"  # MCP on, no servers declared at start
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
    stream = []  # the raw terminal output, for --raw (colours are replayed offline)

    def reader():
        try:
            while True:
                text = pty.read(65536)
                with lock:
                    screen.feed(text)
                    if args.raw:
                        stream.append(text)
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
        if args.raw:
            with lock:
                path.with_suffix(".ansi").write_text("".join(stream), encoding="utf-8", newline="")
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
                if keys.startswith(("click:", "wheel:")):
                    settle(text, quiet=1.5, limit=30)
                    found = click(text, keys)
                    if found is None:
                        errors.append(f"step {name}: {keys[6:]!r} is not on screen")
                        continue
                    keys = found
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
    parser.add_argument("--skills", action="store_true",
                        help="turn skills on with two fixture skills (the fixture must support it)")
    parser.add_argument("--mcp", action="store_true",
                        help="turn MCP on with no servers declared (the fixture must support it)")
    parser.add_argument("--raw", action="store_true",
                        help="also write each shot's whole terminal stream as <NN>-<name>.ansi")
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
