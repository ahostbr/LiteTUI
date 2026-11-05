"""One canonical explicit speech dispatch within the source-owned private job."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

from litetui import voice_backend


def run(directory):
    tempfile.tempdir = str(directory)
    parameters = json.loads((directory / "parameters.json").read_text(encoding="utf8"))
    owner = object()
    receipt = voice_backend.dispatch_speech(parameters["text"], engine=parameters["engine"],
        voice=parameters["voice"] or None, timeout=parameters["timeout"], owner=owner,
        litesuite_first=parameters["litesuite_first"])
    route = receipt.route
    (directory / "route").write_text(route, encoding="ascii")
    if route == "local":
        if receipt.process is None or receipt.completed is None or not receipt.completed.wait(parameters["timeout"] + 10):
            raise RuntimeError("Local speech helper did not settle")
        route = "local_finished" if receipt.process.returncode == 0 else "failed"
    (directory / "result.json").write_text(json.dumps({"status": route}), encoding="utf8")


if __name__ == "__main__":
    run(Path(sys.argv[1]).resolve())
