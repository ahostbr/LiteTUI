"""Source Voice catalogs from private inventories, with every hardware edge denied."""

import os
import runpy
import subprocess
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

shared = runpy.run_path(str(Path(__file__).with_name("voice_preferences_runtime.py")))
base = shared["serve"]
root = Path(os.environ["LITETUI_DATA_ROOT"])
real_popen = subprocess.Popen


async def serve(app):
    from litetui import native_voice_catalog

    script = root / "fixture_inventory.py"
    script.write_text(
        "import json,pathlib,sys\np=pathlib.Path(sys.argv[1])\nfield=json.loads((p/'parameters.json').read_text())['field']\n(p/'result.json').write_text(json.dumps({'items':['Microphone Δ'] if field=='stt_mic' else ['SAPI Ω']}),encoding='utf8')\n",
        encoding="utf8",
    )

    def command(directory):
        return [sys.executable, "-B", str(script), str(directory)]

    def launch(argv, **kwargs):
        if argv != command(app._native_voice_catalog.directory):
            raise AssertionError("Unexpected subprocess during private discovery")
        return real_popen(argv, **kwargs)

    with ExitStack() as stack:
        stack.enter_context(patch.object(native_voice_catalog, "_command", command))
        stack.enter_context(patch.object(subprocess, "Popen", launch))
        await base(app)


shared["shared"]["history"].serve = serve
if __name__ == "__main__":
    shared["shared"]["history"].main()
