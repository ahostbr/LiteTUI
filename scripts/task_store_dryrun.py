"""Dry run of the per-conversation task-store migration (T0132). READ-ONLY.

    python scripts/task_store_dryrun.py <legacy background-tasks.json> <.convos dir>

Point it at COPIES when in doubt: it opens both paths for reading only and
writes nothing anywhere. For each conversation named by a row it prints how many
rows a top-up would copy, how many the conversation's store already holds, and
which rows are unreachable (their conversation directory does not exist, so they
stay in the legacy file as history). Counts only - no task text is printed.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

STORE = "background-tasks.json"


def _rows(path: Path) -> list[dict]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return []
    return [r for r in raw if isinstance(r, dict)] if isinstance(raw, list) else []


def main(legacy: Path, convos: Path) -> int:
    by_convo: dict[str, list[dict]] = defaultdict(list)
    for row in _rows(legacy):
        if "id" in row:
            by_convo[str(row.get("convo_id") or "")].append(row)

    copy = present = unreachable = 0
    print(f"{'convo':<38} {'would copy':>10} {'present':>8}")
    for convo_id, rows in sorted(by_convo.items()):
        directory = convos / convo_id
        if not convo_id or not directory.is_dir():
            unreachable += len(rows)
            print(f"{convo_id or '(no convo id)':<38} {'UNREACHABLE':>10} {len(rows):>8} rows stay in the legacy file")
            continue
        have = {r["id"] for r in _rows(directory / STORE) if "id" in r}
        already = sum(1 for r in rows if r["id"] in have)
        present += already
        copy += len(rows) - already
        print(f"{convo_id:<38} {len(rows) - already:>10} {already:>8}")

    total = copy + present + unreachable
    print(f"\ntotal rows {total}: would copy {copy}, already present {present}, unreachable {unreachable}")
    print(f"reachable {copy + present} of {total}; conversations named: {len(by_convo)}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(Path(sys.argv[1]), Path(sys.argv[2])))
