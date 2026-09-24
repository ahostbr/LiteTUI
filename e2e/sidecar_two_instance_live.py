"""LIVE proof: two LiteTUI instances, two sidecars, edits in each, readback in all four.

Ryan 2026-09-24: "Yes, make it editable (full parity) — EXTREMELY IMPORTANT TO
MAKE TO LITETUI SEPRATE INSTANCE COMPATIABLE".

Two real LiteTUI processes (e2e/sidecar_instance.py) share one LITETUI_DATA_ROOT,
as two seats on one machine do, each with its own conversation and its own
real litetui-sidecar.exe window. The edits are made on each sidecar PAGE with
its own controls and Save button, reached over WebView2's DevTools port (a test
harness attachment, not a product backdoor). Checked:

  1. A conversation-scoped edit in A's sidecar changes A only; B's TUI and
     B's sidecar never see it.
  2. A device-scoped (global) edit in B's sidecar is applied in B, lands on
     disk, and reaches A exactly as the TUI propagates it (disk, not A's
     in-memory settings); A's sidecar, holding the old revision, gets an
     explicit CONFLICT on its next save, reloads, and then saves.
  3. On Claude, an effort change in A's sidecar on a live warm session shows
     the SAME cache warning; Cancel saves nothing; Send anyway saves and
     pre-approves the next send.
Screenshots of both sidecar windows are saved under artifacts/.

    set LITETUI_CLAUDE_LIVE=1 && python e2e/sidecar_two_instance_live.py <path-to-litetui-sidecar.exe>
"""
import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets

HERE = Path(__file__).resolve().parent
ART = Path("artifacts")
FIELD_CONVO, FIELD_GLOBAL = "autocompact_at_percent", "show_stop_time"


class Page:
    """One sidecar page over DevTools."""

    def __init__(self, name, port):
        self.name, self.port, self.ws, self.n = name, port, None, 0

    async def connect(self, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/json/list", timeout=2) as r:
                    pages = json.load(r)
                # WebView2 serves the custom protocol as http://sidecar.localhost/.
                page = next((p for p in pages if p.get("type") == "page" and (
                    p.get("url", "").startswith("sidecar://") or "sidecar.localhost" in p.get("url", ""))), None)
                if page:
                    self.ws = await websockets.connect(page["webSocketDebuggerUrl"], max_size=2**26)
                    return
            except OSError:
                pass
            await asyncio.sleep(0.5)
        raise AssertionError(f"{self.name}: sidecar page never appeared on port {self.port}")

    async def call(self, method, **params):
        self.n += 1
        await self.ws.send(json.dumps({"id": self.n, "method": method, "params": params}))
        while True:
            msg = json.loads(await self.ws.recv())
            if msg.get("id") == self.n:
                if "error" in msg:
                    raise AssertionError(f"{self.name}: {msg['error']}")
                return msg.get("result", {})

    async def js(self, expression):
        r = await self.call("Runtime.evaluate", expression=expression, returnByValue=True, awaitPromise=True)
        if "exceptionDetails" in r:
            raise AssertionError(f"{self.name}: {r['exceptionDetails']}")
        return r["result"].get("value")

    async def wait(self, expression, what, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            if await self.js(expression):
                return
            await asyncio.sleep(0.25)
        raise AssertionError(f"{self.name}: timed out waiting for {what}; status={await self.status()!r}")

    async def status(self):
        return await self.js("SidecarShell.state().status")

    async def field(self, key):
        return await self.js(f"SidecarShell.state().snapshot.fields[{json.dumps(key)}].saved")

    async def edit(self, category, key, value):
        """Change one control the way a person does, then press Save."""
        await self.goto(category)
        await self.js(f"""(() => {{
            const el = document.querySelector('[data-edit={json.dumps(key)}]');
            if (!el) throw new Error('no editor for {key}');
            if (el.type === 'checkbox') el.checked = {json.dumps(value)} === true;
            else el.value = {json.dumps(value)};
            el.dispatchEvent(new Event('change'));
            document.getElementById('save').click();
            return true; }})()""")

    async def goto(self, category):
        """Click the category in the rail, as a person does."""
        await self.js(f"document.querySelector('[data-category=' + {json.dumps(json.dumps(category))} + ']').click(); true")

    async def shot(self, name):
        r = await self.call("Page.captureScreenshot", format="png")
        ART.mkdir(exist_ok=True)
        (ART / name).write_bytes(base64.b64decode(r["data"]))


class Instance:
    def __init__(self, name, root, exe, port):
        self.name = name
        self.work = Path(tempfile.mkdtemp(prefix=f"sidecar-{name}-"))
        env = {**os.environ, "LITETUI_DATA_ROOT": str(root), "LITETUI_SIDECAR_EXE": str(exe),
               "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": f"--remote-debugging-port={port}",
               # Two sidecars otherwise share one WebView2 browser process (one
               # user-data folder beside the exe), and only the first launch's
               # debug port takes effect. Harmless in use; fatal to telling
               # the two pages apart here.
               "WEBVIEW2_USER_DATA_FOLDER": str(self.work / "webview2"),
               "PYTHONPATH": str(HERE.parent / "src")}
        self.log = open(self.work / "instance.log", "w", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, str(HERE / "sidecar_instance.py"), str(self.work)],
                                     env=env, stdout=self.log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, cwd=str(HERE.parent))
        self.n = 0
        self.page = Page(name, port)

    async def ask(self, cmd="state", timeout=180, **extra):
        self.n += 1
        (self.work / "cmd.json").write_text(json.dumps({"n": self.n, "cmd": cmd, **extra}), encoding="utf-8")
        end = time.time() + timeout
        while time.time() < end:
            try:
                s = json.loads((self.work / "state.json").read_text(encoding="utf-8"))
                if s["n"] == self.n:
                    return s
            except (OSError, ValueError, KeyError):
                pass
            if self.proc.poll() is not None:
                raise AssertionError(f"{self.name} exited; see {self.work / 'instance.log'}")
            await asyncio.sleep(0.25)
        raise AssertionError(f"{self.name}: no state for {cmd}")

    async def quit(self):
        (self.work / "cmd.json").write_text(json.dumps({"n": 10**6, "cmd": "quit"}), encoding="utf-8")
        try:
            self.proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def check(label, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + label + (f"  [{detail}]" if detail else ""))
    if not ok:
        raise AssertionError(label)


async def main(exe: Path):
    root = Path(tempfile.mkdtemp(prefix="litetui-two-instance-"))
    (root / "settings.json").write_text(json.dumps({
        "backend": "claude", "backend_chosen": True, "default_model": "sonnet",
        "sidecar_enabled": True, "tools_enabled": False, "skills_enabled": False, "mcp_enabled": False,
        "autocompact_enabled": False, "show_stop_time": False,
    }), encoding="utf-8")
    a, b = Instance("A", root, exe, 9331), Instance("B", root, exe, 9332)
    record = {"root": str(root)}
    try:
        await a.page.connect(); await b.page.connect()
        for inst in (a, b):
            await inst.page.wait("window.SidecarShell && SidecarShell.state().snapshot && SidecarShell.state().canWrite", "an editable snapshot")
        sa, sb = await a.ask(), await b.ask()
        record["start"] = {"A": sa, "B": sb}
        check("two separate processes and conversations", sa["pid"] != sb["pid"] and sa["convo"] != sb["convo"],
              f"A={sa['pid']}/{sa['convo'][:8]} B={sb['pid']}/{sb['convo'][:8]}")
        check("both sidecars show the Claude engine", await a.page.js("SidecarShell.state().snapshot.backend") == "claude"
              and await b.page.js("SidecarShell.state().snapshot.backend") == "claude")
        await a.page.goto("Generation")
        check("temperature is unsupported on Claude: shown, dimmed, no editor",
              await a.page.js("!document.querySelector('[data-edit=\"temperature\"]') && "
                              "SidecarShell.state().snapshot.fields.temperature.control.owner === 'unsupported'"))
        check("thinking_level is Claude's native effort, editable with its levels",
              await a.page.js("!!document.querySelector('select[data-edit=\"thinking_level\"]') && "
                              "SidecarShell.state().snapshot.fields.thinking_level.control.owner === 'native'"))
        await a.page.goto("Model"); await a.page.shot("sidecar-A-claude-model.png")
        await a.page.goto("Generation"); await a.page.shot("sidecar-A-claude-generation.png")
        b_temp_before = sb["disk_saved"][FIELD_CONVO]

        # 1. conversation-scoped edit in A only
        await a.page.edit("Compaction", FIELD_CONVO, "55")
        await a.page.wait("SidecarShell.state().status.startsWith('Saved')", "A's save")
        sa, sb = await a.ask(), await b.ask()
        check("A's TUI holds A's conversation edit", sa["in_memory"][FIELD_CONVO] == 55 and sa["disk_saved"][FIELD_CONVO] == 55,
              json.dumps({"mem": sa["in_memory"][FIELD_CONVO], "disk": sa["disk_saved"][FIELD_CONVO]}))
        check("B's TUI is untouched by A's conversation edit", sb["in_memory"][FIELD_CONVO] == b_temp_before
              and sb["disk_saved"][FIELD_CONVO] == b_temp_before, f"B={sb['disk_saved'][FIELD_CONVO]}")
        check("A's sidecar shows it", await a.page.field(FIELD_CONVO) == 55)
        check("B's sidecar does not", await b.page.field(FIELD_CONVO) == b_temp_before)
        record["after_A_convo_edit"] = {"A": sa, "B": sb}

        # 2. global edit in B, propagation exactly as the TUI's
        await b.page.edit("Interface", FIELD_GLOBAL, True)
        await b.page.wait("SidecarShell.state().status.startsWith('Saved')", "B's save")
        sa, sb = await a.ask(), await b.ask()
        check("B's TUI applied B's global edit", sb["in_memory"][FIELD_GLOBAL] is True and sb["disk_saved"][FIELD_GLOBAL] is True)
        check("the global edit is on disk for A (shared device settings)", sa["disk_saved"][FIELD_GLOBAL] is True)
        check("A's in-memory value is unchanged, as the TUI propagates today (no live push)",
              sa["in_memory"][FIELD_GLOBAL] is False, f"A mem={sa['in_memory'][FIELD_GLOBAL]}")
        check("B's sidecar shows it", await b.page.field(FIELD_GLOBAL) is True)
        await a.page.edit("Compaction", FIELD_CONVO, "65")
        await a.page.wait("SidecarShell.state().status.startsWith('Not saved: settings changed elsewhere')", "A's conflict")
        check("A's sidecar, holding the old global revision, got a CONFLICT (no overwrite)", True, await a.page.status())
        check("A's sidecar reloaded and now shows B's global edit", await a.page.field(FIELD_GLOBAL) is True)
        await a.page.js("document.getElementById('save').click(); true")
        await a.page.wait("SidecarShell.state().status.startsWith('Saved')", "A's re-save")
        sa, sb = await a.ask(), await b.ask()
        check("A's re-save landed on A only", sa["disk_saved"][FIELD_CONVO] == 65 and sb["disk_saved"][FIELD_CONVO] == b_temp_before)
        check("B's global edit survived A's save", sa["disk_saved"][FIELD_GLOBAL] is True and sb["disk_saved"][FIELD_GLOBAL] is True)
        record["after_B_global_edit"] = {"A": sa, "B": sb}
        await b.page.goto("Interface"); await b.page.shot("sidecar-B-interface.png")

        # 3. the cache warning, on A's live Claude session
        sa = await a.ask("turn", timeout=300, text="Reply only OK.")
        check("A ran a real Claude turn (live, warm session)", sa["backend"] == "claude")
        await a.page.edit("Generation", "thinking_level", "max")
        await a.page.wait("!!document.getElementById('cache-send')", "the cache warning dialog")
        warning = await a.page.js("document.querySelector('.dialog').innerText")
        await a.page.shot("sidecar-A-cache-warning.png")
        check("the sidecar shows the TUI's cache warning", "Changing effort" in warning and "full input price" in warning,
              warning.splitlines()[1] if len(warning.splitlines()) > 1 else warning)
        await a.page.js("document.getElementById('cache-cancel').click(); true")
        sa = await a.ask()
        check("Cancel saved nothing", sa["disk_saved"]["thinking_level"] != "max" and sa["in_memory"]["_thinking_level"] != "max",
              str(sa["disk_saved"]["thinking_level"]))
        await a.page.js("document.getElementById('save').click(); true")
        await a.page.wait("!!document.getElementById('cache-send')", "the cache warning again")
        await a.page.js("document.getElementById('cache-send').click(); true")
        await a.page.wait("SidecarShell.state().status.startsWith('Saved')", "the confirmed save")
        sa, sb = await a.ask(), await b.ask()
        check("Send anyway saved the effort in A and pre-approved the next send",
              sa["disk_saved"]["thinking_level"] == "max" and sa["preapproved"] == ["effort", "max"], str(sa["preapproved"]))
        check("B's effort is untouched", sb["disk_saved"]["thinking_level"] != "max")
        record["after_effort"] = {"A": sa, "B": sb}
        print("PASS live: two instances, two sidecars, isolated edits, TUI-exact propagation, cache warning")
    finally:
        ART.mkdir(exist_ok=True)
        (ART / "sidecar-two-instance.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        for inst in (a, b):
            await inst.quit()


if __name__ == "__main__":
    if os.environ.get("LITETUI_CLAUDE_LIVE") != "1":
        raise SystemExit("Set LITETUI_CLAUDE_LIVE=1 to run this explicit live probe")
    asyncio.run(main(Path(sys.argv[1]).resolve()))
