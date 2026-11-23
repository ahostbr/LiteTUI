"""Rebuild the egress blocklist and the firewall script (T1418).

    python scripts/egress_refresh.py            # fetch, print the diff, change nothing
    python scripts/egress_refresh.py --write    # fetch, write data/egress_blocklist.json and egress_firewall_rules.ps1

The firewall script is generated as a FILE ONLY. Nothing here runs it or touches the Windows Firewall.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from litetui import egress_policy, egress_refresh  # noqa: E402

FIREWALL_PATH = ROOT / "scripts" / "egress_firewall_rules.ps1"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--write", action="store_true", help="write the data file and the firewall script")
    args = ap.parse_args(argv)
    data, warnings = egress_refresh.build()
    for w in warnings:
        print("WARNING:", w)
    for line in egress_refresh.summary(data):
        print(line)
    print("--- diff against", egress_policy.DATA_PATH)
    for line in egress_refresh.diff(egress_refresh.load_existing(), data):
        print(line)
    if args.write:
        egress_refresh.write(data, egress_policy.DATA_PATH)
        egress_refresh.write_firewall(data, FIREWALL_PATH)
        print("wrote", egress_policy.DATA_PATH)
        print("wrote", FIREWALL_PATH, "(file only; never run by LiteTUI)")
    else:
        print("(dry run: pass --write to write)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
