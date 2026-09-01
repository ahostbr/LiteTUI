"""Tool schemas, one JSON file per tool — INSIDE the package on purpose.

These files used to live at the repo root in ``tools/`` and were read through
a path anchored on ``Path(__file__).parent.parent.parent`` (the repo root).
That anchor is a lie inside an installed wheel: there the package sits in
site-packages, so "two levels up" lands OUTSIDE the install, and first launch
from the PyPI wheel died with::

    FileNotFoundError ...\\Lib\\tools\\harness.json

T135 (2026-08-30): the schemas now ship inside ``litetui`` and are read with
importlib.resources, which asks the package where its own files are instead of
counting directories. The repo-root ``tools/`` layout is kept as a fallback
location for consumers that still write there; see ``tool_schemas.py``.

The drift gate (tests/test_tool_schemas.py) asserts this folder's file set and
the registered tool set agree in BOTH directions — it follows the files here,
not the old location.
"""
