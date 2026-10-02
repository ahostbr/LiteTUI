"""CLI test subprocess: isolate HOME before importing the installed harness.

Invocation uses the system interpreter containing the installed authoritative
CLI. No sys.path edits, live identities, live mailbox writes or tool actions.
"""
import sys
import time
from pathlib import Path

home = Path(sys.argv.pop(1))
Path.home = classmethod(lambda cls: home)
from liteharness.cli import main

if "--sandbox-delay" in sys.argv:
    index = sys.argv.index("--sandbox-delay")
    delay = float(sys.argv[index + 1])
    del sys.argv[index:index + 2]
    time.sleep(delay)
main()
