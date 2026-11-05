"""A private metadata thread remains owned until Quit has joined its read."""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path
from unittest.mock import patch

path = Path(os.environ["RUST_CONTEXT_COMMON_FIXTURE"])
spec = importlib.util.spec_from_file_location("_model_lifecycle_quit_boundaries", path)
assert spec and spec.loader
common = importlib.util.module_from_spec(spec)
spec.loader.exec_module(common)
common.history.serve = common.serve
from litetui import native_model_lifecycle

dispatch = native_model_lifecycle.dispatch
custom_get = common.history.boundary.custom_get
held = False
connect_case = os.environ.get("RUST_MODEL_QUIT_CONNECT") == "1"
did_hold = False


async def lifecycle(app, cmd):
    global held
    if not connect_case and cmd["type"] == native_model_lifecycle.PREFIX + "snapshot":
        held = True
    return await dispatch(app, cmd)


def read(*args, **kwargs):
    global held, did_hold
    owner = common.history.boundary.app_instance
    if not did_hold and (
        held
        or connect_case
        and getattr(owner, "_native_model_lifecycle_connect", None) is not None
    ):
        did_hold = True
        held = False
        common.mark("model-read-enter")
        deadline = time.monotonic() + 20
        while not (common.root / "stage-command-shutdown.json").is_file():
            if time.monotonic() > deadline:
                raise TimeoutError("Controlled Quit was not supplied")
            time.sleep(0.01)
        common.mark("model-read-exit")
    return custom_get(*args, **kwargs)


if __name__ == "__main__":
    common.history.boundary.custom_get = read
    with patch.object(native_model_lifecycle, "dispatch", lifecycle):
        common.history.main()
