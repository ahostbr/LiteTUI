"""LIVE proof: ClinePass in the editable sidecar, beside a second instance.

Ryan 2026-09-24: "the sidecar is a gui representation of the settings menu",
and "EXTREMELY IMPORTANT TO MAKE TO LITETUI SEPRATE INSTANCE COMPATIABLE".

Instance A runs on ClinePass (invocation-only --backend cline, exactly as a
second seat launched that way), instance B on the shared default (Claude). Both
share one LITETUI_DATA_ROOT and each opens its own real litetui-sidecar window.
Checked on A's sidecar, against what the TUI shows for ClinePass:
  - engine choices carry ClinePass with its /backend readiness mark;
  - default-model choices are the 14 cline-pass ids;
  - local-server rows are shown as unsupported with no editor;
  - thinking choices are ClinePass's per-model levels (off, never the wire 'none');
  - the launch-set engine shows the value in effect and is EDITABLE (full parity).
Then an edit in A's sidecar (thinking level, then compaction threshold) lands
in A only, B's TUI and sidecar never see it, and a live ClinePass turn runs in
A after the edit. Last, A changes its launch-set engine to codex in the sidecar;
A's next /reconnect adopts it and B does not move.

    python e2e/sidecar_cline_live.py <path-to-litetui-sidecar.exe>

Needs a ClinePass login (`cline auth`, read from ~/.cline), the Claude CLI login
for B and the Codex login for A's final engine, and a sidecar built from
litetui-sidecar 2b27b6e or later.
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from sidecar_two_instance_live import ART, HERE, Instance, Page, check


class ClineAwareInstance(Instance):
    def __init__(self, name, root, exe, port, backend=None, model=None):
        self.name = name
        self.work = Path(tempfile.mkdtemp(prefix=f"sidecar-{name}-"))
        env = {**os.environ, "LITETUI_DATA_ROOT": str(root), "LITETUI_SIDECAR_EXE": str(exe),
               "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS": f"--remote-debugging-port={port}",
               "WEBVIEW2_USER_DATA_FOLDER": str(self.work / "webview2"),
               "PYTHONPATH": str(HERE.parent / "src"),
               "E2E_BACKEND": backend or "", "E2E_MODEL": model or ""}
        self.log = open(self.work / "instance.log", "w", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, str(HERE / "cline_instance.py"), str(self.work)],
                                     env=env, stdout=self.log, stderr=subprocess.STDOUT,
                                     stdin=subprocess.DEVNULL, cwd=str(HERE.parent))
        self.n = 0
        self.page = Page(name, port)


async def main(exe: Path):
    root = Path(tempfile.mkdtemp(prefix="litetui-cline-two-instance-"))
    (root / "settings.json").write_text(json.dumps({
        "backend": "claude", "backend_chosen": True, "default_model": "sonnet",
        "sidecar_enabled": True, "tools_enabled": False, "skills_enabled": False, "mcp_enabled": False,
        "autocompact_enabled": False,
    }), encoding="utf-8")
    a = ClineAwareInstance("A", root, exe, 9341, backend="cline", model="cline-pass/glm-5.3-flash")
    b = ClineAwareInstance("B", root, exe, 9342)
    record = {"root": str(root)}
    try:
        await a.page.connect(); await b.page.connect()
        for inst in (a, b):
            await inst.page.wait("window.SidecarShell && SidecarShell.state().snapshot && SidecarShell.state().canWrite",
                                 "an editable snapshot")
        sa, sb = await a.ask(), await b.ask()
        record["start"] = {"A": sa, "B": sb}
        check("two separate processes and conversations", sa["pid"] != sb["pid"] and sa["convo"] != sb["convo"],
              f"A={sa['pid']}/{sa['convo'][:8]} B={sb['pid']}/{sb['convo'][:8]}")
        check("A's TUI runs ClinePass, B's runs Claude", sa["backend"] == "cline" and sb["backend"] == "claude",
              f"A={sa['backend']}/{sa['model']} B={sb['backend']}")
        snap = "SidecarShell.state().snapshot"
        check("A's sidecar shows the ClinePass engine", await a.page.js(f"{snap}.backend") == "cline")
        engines = await a.page.js(f"{snap}.choices.backend")
        cline_row = next((e for e in engines if e["value"] == "cline"), None)
        check("engine choices carry ClinePass with its /backend readiness mark",
              cline_row is not None and cline_row["label"] == "ClinePass (Cline subscription)  · OAuth signed in",
              cline_row and cline_row["label"])
        models = await a.page.js(f"{snap}.choices.default_model")
        check("default-model choices are the 14 cline-pass ids",
              len(models) == 14 and all(m.startswith("cline-pass/") for m in models), f"{len(models)} ids")
        await a.page.goto("Model")
        engine = await a.page.js(f"{snap}.fields.backend")
        check("A's engine field shows the LAUNCH value, as the TUI's /settings does",
              engine["effective"] == "cline" and engine["source"] == "override" and engine.get("override_by") == "launch",
              json.dumps({k: engine.get(k) for k in ("saved", "effective", "source", "override_by")}))
        check("the launch-set engine is EDITABLE in A's sidecar (full parity), showing the value in effect",
              await a.page.js("(document.querySelector('select[data-edit=\"backend\"]')||{}).value") == "cline")
        check("the row says it was set at launch",
              "set when this LiteTUI was started" in await a.page.js("document.querySelector('.settingsPanel').innerText"))
        b_engine = await b.page.js(f"{snap}.fields.backend")
        check("B (no launch flag) keeps an editable engine field",
              b_engine["source"] == "saved" and await b.page.js("!!document.querySelector('[data-edit=\"backend\"]')"))
        check("local-server rows are unsupported on ClinePass: shown, no editor",
              await a.page.js("['custom_base_url','lm_host','llama_host','default_context_length'].every(k => "
                              "!document.querySelector('[data-edit=\"' + k + '\"]') && "
                              f"{snap}.fields[k].control.owner === 'unsupported')"))
        await a.page.shot("sidecar-A-cline-model.png")
        await a.page.goto("Generation")
        options = await a.page.js("[...document.querySelectorAll('select[data-edit=\"thinking_level\"] option')]"
                                  ".map(o => o.value)")
        check("thinking choices are ClinePass's per-model levels, 'off' not the wire 'none'",
              options[:4] == ["off", "low", "high", "max"] and "none" not in options, str(options))
        check("thinking_level is marked native on ClinePass",
              await a.page.js(f"{snap}.fields.thinking_level.control.owner") == "native")
        await a.page.shot("sidecar-A-cline-generation.png")

        b_before = sb["disk_saved"]
        await a.page.edit("Generation", "thinking_level", "high")
        await a.page.wait("SidecarShell.state().status.startsWith('Saved')", "A's thinking save")
        await a.page.edit("Compaction", "autocompact_at_percent", "55")
        await a.page.wait("SidecarShell.state().status.startsWith('Saved')", "A's compaction save")
        sa, sb = await a.ask(), await b.ask()
        record["after_A_edits"] = {"A": sa, "B": sb}
        check("A's TUI holds both edits, in memory and on disk",
              sa["disk_saved"]["thinking_level"] == "high" and sa["in_memory"]["_thinking_level"] == "high"
              and sa["disk_saved"]["autocompact_at_percent"] == 55 and sa["in_memory"]["autocompact_at_percent"] == 55,
              json.dumps({"disk": sa["disk_saved"], "think_mem": sa["in_memory"]["_thinking_level"]}))
        check("B's TUI is untouched by A's ClinePass edits",
              sb["disk_saved"]["thinking_level"] == b_before["thinking_level"]
              and sb["disk_saved"]["autocompact_at_percent"] == b_before["autocompact_at_percent"]
              and sb["in_memory"]["autocompact_at_percent"] == b_before["autocompact_at_percent"],
              json.dumps(sb["disk_saved"]))
        check("B's sidecar does not show them",
              await b.page.field("thinking_level") == b_before["thinking_level"]
              and await b.page.field("autocompact_at_percent") == b_before["autocompact_at_percent"])
        check("A stays on ClinePass, B on Claude", sa["backend"] == "cline" and sb["backend"] == "claude")

        sa = await a.ask("turn", timeout=180, text="Reply with the single word: edited")
        record["after_A_turn"] = sa
        check("a live ClinePass turn runs in A after the sidecar edit",
              sa["backend"] == "cline" and sa["in_memory"]["_thinking_level"] == "high"
              and bool(sa["last_reply"]), repr((sa["last_reply"] or "")[:80]))
        await a.page.goto("Generation"); await a.page.shot("sidecar-A-cline-after-edit.png")
        await b.page.goto("Generation"); await b.page.shot("sidecar-B-claude-untouched.png")
        # Full parity: change A's launch-set engine in A's sidecar; it must be what
        # A's next reconnect adopts, and B must not move.
        b_before = await b.ask()
        await a.page.edit("Model", "backend", "codex")
        await a.page.wait("SidecarShell.state().status.startsWith('Saved')", "A's engine save")
        sa, sb = await a.ask(), await b.ask()
        record["after_A_engine_edit"] = {"A": sa, "B": sb}
        check("A saved codex and released its launch value; still on cline until reconnect",
              sa["disk_saved"]["backend"] == "codex" and "backend" not in sa["launch_pin"] and sa["backend"] == "cline",
              json.dumps({"saved": sa["disk_saved"]["backend"], "pin": sa["launch_pin"], "running": sa["backend"]}))
        check("B's engine, saved value and pin are untouched",
              sb["backend"] == "claude" and sb["disk_saved"]["backend"] == b_before["disk_saved"]["backend"]
              and sb["launch_pin"] == b_before["launch_pin"])
        sa = await a.ask("reconnect", timeout=180)
        sb = await b.ask()
        record["after_A_reconnect"] = {"A": sa, "B": sb}
        check("A's edit SURVIVES reconnect: A now runs codex", sa["backend"] == "codex" and sa["disk_saved"]["backend"] == "codex",
              f"running={sa['backend']} saved={sa['disk_saved']['backend']}")
        check("B still runs claude, its saved engine unchanged",
              sb["backend"] == "claude" and sb["disk_saved"]["backend"] == b_before["disk_saved"]["backend"])
        print("PASS live: ClinePass in the editable sidecar, TUI-exact choices, edits isolated from instance B")
    finally:
        ART.mkdir(exist_ok=True)
        (ART / "sidecar-cline-two-instance.json").write_text(json.dumps(record, indent=2, default=str), encoding="utf-8")
        for inst in (a, b):
            await inst.quit()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve()))
