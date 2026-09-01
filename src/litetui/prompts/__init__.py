"""Authored prompt text — INSIDE the package on purpose.

These files used to live at the repo root in ``prompts/`` and were read
through ``paths.PROMPTS_DIR``, a path anchored on the repo root. Inside an
installed wheel that anchor resolves outside the install, so:

* ``compact.md`` / ``wake-after-compact.md`` — read at IMPORT time by app.py —
  crashed first launch from the PyPI wheel (T135);
* ``tools.md`` — read on every turn while tools are enabled — would have
  crashed the first message;
* ``systemprompt.md`` — guarded by an exists() check, so it degraded SILENTLY:
  the agent running with no base prompt at all.

T135 (2026-08-30): the files ship inside ``litetui`` and ``paths.PROMPTS_DIR``
resolves to this folder when the repo-root layout is absent. A repo-root
``prompts/`` still wins when present, so a dev checkout can override any file
without touching src/.
"""
