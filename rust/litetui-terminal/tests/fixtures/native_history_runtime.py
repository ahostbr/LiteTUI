"""Native supervisor driver using the independently reviewed source boundary fixture."""
import os
import runpy
from pathlib import Path

os.environ["RUST_HISTORY_DRIVER"] = "rpc"
runpy.run_path(str(Path(__file__).with_name("history_runtime.py")), run_name="__main__")
