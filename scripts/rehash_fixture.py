"""Recompute content digests in a recorded fixture after an edit (e.g. scrub_fixture_paths.py).

Recorded fixtures chain SHA-256 digests over their own objects (a readback's result_sha256 is
the digest of the result; a shutdown's readback_sha256 is the digest of the readback). The
digest is serde_json's compact, key-sorted encoding, which Python reproduces with
json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False).

The ORIGINAL fixture says which field digests which object: every 64-hex field whose value
equals the digest of some object or string in that file. Those same fields are recomputed in
the EDITED fixture, repeating until nothing changes (digests of digests).

    python scripts/rehash_fixture.py ORIGINAL.json EDITED.json   (rewrites EDITED in place)
"""
import hashlib
import json
import sys
from pathlib import Path


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def digest(value):
    data = value.encode("utf-8") if isinstance(value, str) else canonical(value)
    return hashlib.sha256(data).hexdigest()


def nodes(value, path=()):
    yield path, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from nodes(item, (*path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from nodes(item, (*path, index))


def get(value, path):
    for part in path:
        value = value[part]
    return value


def put(value, path, new):
    for part in path[:-1]:
        value = value[part]
    value[path[-1]] = new


original_path, edited_path = map(Path, sys.argv[1:3])
original = json.loads(original_path.read_text(encoding="utf-8"))
raw = edited_path.read_text(encoding="utf-8")
edited = json.loads(raw)
sources = {}
for path, value in nodes(original):
    if isinstance(value, (dict, list, str)):
        sources.setdefault(digest(value), path)
links = [(path, sources[value]) for path, value in nodes(original)
         if isinstance(value, str) and len(value) == 64 and value in sources and sources[value] != path]
changed = 0
for _ in range(16):
    round_changes = 0
    for field, source in links:
        new = digest(get(edited, source))
        if get(edited, field) != new:
            put(edited, field, new)
            round_changes += 1
    changed += round_changes
    if not round_changes:
        break
pretty = ", " in raw[:2000] or ": " in raw[:2000]
text = json.dumps(edited, ensure_ascii=False, separators=(", ", ": ") if pretty else (",", ":"))
edited_path.write_bytes((text + ("\n" if raw.endswith("\n") else "")).encode("utf-8"))
print(f"{len(links)} digest links, {changed} recomputed: {edited_path}")
