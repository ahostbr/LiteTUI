"""Always-on metadata-only rotating diagnostics."""
from __future__ import annotations

from pathlib import Path

from litetui import paths, runtime_log
from litetui.plugins import PluginManifest

PLUGIN_ID = "runtime-log"
def register(ctx, root: Path | None = None) -> None:
    runtime_log.install(runtime_log.default_log_path(root or paths.ROOT))
    ctx.observe(runtime_log.record_signal)


PLUGIN = PluginManifest(id=PLUGIN_ID, register=register)
