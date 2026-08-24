"""Always-on metadata-only rotating diagnostics."""
from __future__ import annotations

from pathlib import Path

from litetui import paths
from litetui.plugins import PluginManifest
from litetui.runtime_log import RuntimeRecorder, default_log_path

PLUGIN_ID = "runtime-log"
_RECORDER: RuntimeRecorder | None = None


def register(ctx, root: Path | None = None) -> None:
    global _RECORDER
    _RECORDER = RuntimeRecorder(default_log_path(root or paths.ROOT))
    ctx.observe(_RECORDER.write_signal)


PLUGIN = PluginManifest(id=PLUGIN_ID, register=register)
