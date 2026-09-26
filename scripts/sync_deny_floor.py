"""Copy the canonical deny floor from liteharness-oss into LiteTUI.

    python scripts/sync_deny_floor.py [path/to/liteharness-oss]

Default source: the first `liteharness-oss` folder beside this checkout or any
parent of it. tests/test_deny_floor.py fails while the two files differ.
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src" / "litetui" / "deny_floor.py"


def canonical(explicit: str | None = None) -> Path | None:
    roots = [Path(explicit)] if explicit else [p / "liteharness-oss" for p in ROOT.parents]
    for root in roots:
        source = root / "liteharness" / "deny_floor.py"
        if source.is_file():
            return source
    return None


if __name__ == "__main__":
    source = canonical(sys.argv[1] if len(sys.argv) > 1 else None)
    if source is None:
        sys.exit("no liteharness-oss/liteharness/deny_floor.py found; pass the repo path")
    shutil.copyfile(source, TARGET)
    print(f"{source} -> {TARGET}")
