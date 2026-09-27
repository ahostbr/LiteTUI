"""Copy the canonical deny floor (T1026) and fleet floor (T1043) from
liteharness-oss into LiteTUI: one rule set each, kept byte-identical.

    python scripts/sync_deny_floor.py [path/to/liteharness-oss]

Source, first that exists: the argument, $LITEHARNESS_SRC, then the first
`liteharness-oss` checkout beside this one or beside any parent of it.
tests/test_deny_floor.py and tests/test_fleet_floor_t1043.py fail while a
copy differs from its canonical file.
"""
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SHARED = ("deny_floor.py", "fleet_policy.py")


def sibling(explicit: str | None = None) -> Path | None:
    """The liteharness-oss checkout to compare against, or None if there is none."""
    named = [explicit, os.environ.get("LITEHARNESS_SRC")]
    roots = [Path(p) for p in named if p] + [p / "liteharness-oss" for p in ROOT.parents]
    for root in roots:
        if (root / "liteharness").is_dir():
            return root
    return None


def canonical(explicit: str | None = None, name: str = "deny_floor.py") -> Path | None:
    repo = sibling(explicit)
    return repo / "liteharness" / name if repo else None


if __name__ == "__main__":
    for name in SHARED:
        source = canonical(sys.argv[1] if len(sys.argv) > 1 else None, name)
        if source is None or not source.is_file():
            sys.exit(f"no liteharness/{name} at {source}; pass the repo path")
        target = ROOT / "src" / "litetui" / name
        shutil.copyfile(source, target)
        print(f"{source} -> {target}")
