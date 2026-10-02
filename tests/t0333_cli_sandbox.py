"""CLI test subprocess: isolate HOME before importing the installed harness.

Invocation uses the system interpreter containing the installed authoritative
CLI. No sys.path edits, live identities, live mailbox writes or tool actions.
"""
import sys
from pathlib import Path

home = Path(sys.argv.pop(1))
Path.home = classmethod(lambda cls: home)
from liteharness.cli import main

main()
