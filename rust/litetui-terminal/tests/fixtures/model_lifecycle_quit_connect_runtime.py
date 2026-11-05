"""Select the canonical connection read as the disposable Quit boundary."""

import os
import runpy
from pathlib import Path

os.environ["RUST_MODEL_QUIT_CONNECT"] = "1"
runpy.run_path(
    str(Path(__file__).with_name("model_lifecycle_quit_runtime.py")),
    run_name="__main__",
)
