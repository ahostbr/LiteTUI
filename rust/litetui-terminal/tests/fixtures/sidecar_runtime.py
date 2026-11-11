"""Native Sidecar with an actual controlled stdio child and normal source owners."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from unittest.mock import patch

path = Path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
spec = importlib.util.spec_from_file_location("_sidecar_boundaries", path)
assert spec and spec.loader
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
original_seed = common.seed
original_serve = common.serve
windows = []


def seed(app):
    original_seed(app)
    common.report["sidecar_original"] = {"sidecar_enabled": app.settings.sidecar_enabled,
        "show_stop_time": app.settings.show_stop_time, "tool_iterations": app.settings.tool_iterations,
        "temperature": app.settings.temperature}
    (common.root / "sidecar-seed.json").write_text(json.dumps(common.report["sidecar_original"]), encoding="utf8")


async def serve(app):
    from litetui import native_sidecar
    from litetui.sidecar_launch import SidecarWindow

    child = Path(__file__).with_name("sidecar_child.py")

    def spawn(argv, **kwargs):
        assert argv[0] == sys.executable and argv[1] == "--view" and argv[-1] == "--parent-pipe"
        return common.real_popen([sys.executable, "-B", str(child)], **kwargs)

    def window(path):
        value = SidecarWindow(path, spawn=spawn, timeout=10)
        windows.append(value)
        return value

    with patch.object(native_sidecar, "executable", lambda: Path(sys.executable)), patch.object(native_sidecar, "SidecarWindow", window):
        try:
            await original_serve(app)
        finally:
            facts = [{"retained": w.process is not None, "reader_alive": w._reader is not None and w._reader.is_alive()} for w in windows]
            (common.root / "sidecar-retired.json").write_text(json.dumps(facts), encoding="utf8")
            assert all(not row["retained"] and not row["reader_alive"] for row in facts)


common.seed = seed
common.history.serve = serve

if __name__ == "__main__":
    common.history.main()
