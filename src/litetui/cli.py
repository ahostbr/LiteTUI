"""The console-script entry point — thin on purpose.

This used to be ``litetui.app:main``, but importing app.py builds a Textual
application (it imports textual at module level), so even ``litetui --version``
paid the full cold-start cost before printing one line. Every package manager,
wizard, and curious user runs exactly that probe on first install; this launcher
checks argv BEFORE touching app, so the version path costs only sys + version.

The heavy import is deferred to the last possible moment — a ``--version`` probe
never sees it, a real launch pays it once. See version.py for why reading the
number itself must stay cheap.
"""

from __future__ import annotations

import sys


def main() -> None:
    # Fast path for probes: --version / -V print and exit before app.py (and its
    # Textual import) is ever loaded. Cold-start matters — this is the first thing
    # a stranger's shell runs after `uv pip install litetui`.
    if any(a in ("--version", "-V") for a in sys.argv[1:]):
        from litetui.version import __version__  # cheap by design (see version.py)

        print(f"litetui {__version__}")
        return

    from litetui.app import LiteTUI, wants_ansi_fallback  # heavy — deferred past the probe

    LiteTUI(ansi_color=wants_ansi_fallback()).run()


if __name__ == "__main__":
    main()
