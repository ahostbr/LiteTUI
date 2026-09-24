"""LIVE proof: the free-tier key fields in the real sidecar.

Ryan 2026-09-24 (liteask a-29b8bd60): "Add a key field per source in /settings +
sidecar (stored like other secrets)".

One real LiteTUI instance on the free backend opens its own real litetui-sidecar
window. On its "Free tier" page:
  - the seven keyed sources each have a masked (password) field, all "unset";
  - a key typed there and saved lands in settings.json and makes the source live
    (free_tier.status_mark, read from the same data root);
  - the page is then told only "set": the value is in neither the snapshot nor
    the DOM;
  - Clear removes it again.
The key is FAKE; nothing is sent to any source by this script.

    python e2e/sidecar_free_keys_live.py <path-to-litetui-sidecar.exe>
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from sidecar_cline_live import ClineAwareInstance
from sidecar_two_instance_live import ART, check

KEYS = ("groq_api_key", "cerebras_api_key", "nvidia_api_key", "mistral_api_key",
        "github_models_token", "openrouter_api_key", "gemini_api_key")
FAKE = "gsk_e2e_fake_" + "x" * 40


async def main(exe: Path):
    root = Path(tempfile.mkdtemp(prefix="litetui-free-keys-"))
    (root / "settings.json").write_text(json.dumps({
        "backend": "free", "backend_chosen": True, "sidecar_enabled": True, "tools_enabled": False,
        "skills_enabled": False, "mcp_enabled": False, "autocompact_enabled": False,
    }), encoding="utf-8")
    os.environ["LITETUI_DATA_ROOT"] = str(root)  # status_mark below reads the same settings.json
    from litetui import free_tier

    for name in ("GROQ_API_KEY",):
        os.environ.pop(name, None)
    a = ClineAwareInstance("A", root, exe, 9351, backend="free")
    snap = "SidecarShell.state().snapshot"
    try:
        await a.page.connect()
        await a.page.wait(f"window.SidecarShell && {snap} && SidecarShell.state().canWrite", "an editable snapshot")
        await a.page.goto("Free tier")
        types = await a.page.js("Object.fromEntries([...document.querySelectorAll('[data-edit]')]"
                                ".map(e => [e.dataset.edit, e.type]))")
        check("the Free tier page has the seven key fields, all masked",
              types == {k: "password" for k in KEYS}, json.dumps(types))
        check("each is unset", await a.page.js(f"{json.dumps(KEYS)}.every(k => {snap}.fields[k].set === false)"))
        before = free_tier.status_mark()
        await a.page.shot("sidecar-free-keys-unset.png")

        await a.page.edit("Free tier", "groq_api_key", FAKE)
        await a.page.wait("SidecarShell.state().status.startsWith('Saved')", "the key save")
        await a.page.wait(f"{snap}.fields.groq_api_key.set === true", "the snapshot to say set")
        disk = json.loads((root / "settings.json").read_text(encoding="utf-8"))
        check("the key is saved to settings.json", disk.get("groq_api_key") == FAKE)
        after = free_tier.status_mark()
        check("the saved key makes the Groq source live", before != after and "6 need a key" in after,
              f"{before} -> {after}")
        wire = await a.page.js(f"JSON.stringify({snap})")
        dom = await a.page.js("document.documentElement.outerHTML")
        check("the sidecar is never sent the value", FAKE not in wire and "gsk_" not in wire)
        check("the page does not hold it after save", FAKE not in dom and "gsk_" not in dom)
        check("the row says set", "set — type to replace" in await a.page.js(
            "document.querySelector('[data-edit=\"groq_api_key\"]').placeholder"))
        await a.page.shot("sidecar-free-keys-set.png")

        await a.page.js("document.querySelector('[data-clear=\"groq_api_key\"]').click();"
                        "document.getElementById('save').click(); true")
        await a.page.wait(f"{snap}.fields.groq_api_key.set === false", "the clear")
        disk = json.loads((root / "settings.json").read_text(encoding="utf-8"))
        check("Clear removes it from settings.json", disk.get("groq_api_key") == "")
        check("and the source needs a key again", free_tier.status_mark() == before)
        print("PASS live: free-tier keys are masked, saved, activate their source, and never sent back")
    finally:
        ART.mkdir(exist_ok=True)
        await a.quit()


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve()))
