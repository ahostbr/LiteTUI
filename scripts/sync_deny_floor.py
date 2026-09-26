"""Copy the canonical deny floor from liteharness-oss into LiteTUI.

    python scripts/sync_deny_floor.py [path/to/liteharness-oss]

Source, first that exists: the argument, $LITEHARNESS_SRC, then the first
`liteharness-oss` checkout beside this one or beside any parent of it.
tests/test_deny_floor.py fails while the two files differ.
"""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "litetui" / "deny_floor.py"


def sibling(explicit: str | None = None) -> Path | None:
    """The liteharness-oss checkout to compare against, or None if there is none."""
    named = [explicit, os.environ.get("LITEHARNESS_SRC")]
    roots = [Path(p) for p in named if p] + [p / "liteharness-oss" for p in ROOT.parents]
    for root in roots:
        if (root / "liteharness").is_dir():
            return root
    return None


def canonical(explicit: str | None = None) -> Path | None:
    repo = sibling(explicit)
    return repo / "liteharness" / "deny_floor.py" if repo else None


if __name__ == "__main__":
    source = canonical(sys.argv[1] if len(sys.argv) > 1 else None)
    if source is None or not source.is_file():
        sys.exit(f"no liteharness/deny_floor.py at {source}; pass the repo path")
    shutil.copyfile(source, TARGET)
    print(f"{source} -> {TARGET}")
