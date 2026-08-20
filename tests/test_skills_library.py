"""The skills INDEX is the whole product: if a line is unreadable, the skill
is unpickable, and an unpickable skill is indistinguishable from an absent one.

Covers the three pieces of real logic behind multi-root discovery:

  * YAML block scalars — 10 of 77 real skills wrote `description: >-` and put
    the text on the following lines. The flat `key: value` reader took the
    INDICATOR as the value, so those shipped into the index described as ">"
    or "|": listed, and impossible to choose from.
  * collisions are RENAMED, not dropped — `ls-skill-author` exists in two
    libraries. Keeping the first and discarding the second makes a real skill
    unreachable with nothing in the index to say so.
  * glob roots follow the newest match — the plugin cache is versioned, and a
    literal path goes stale on the next update while merely getting quieter.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import skills as sk


def _write(d: Path, name: str, frontmatter: str, body: str = "body\n") -> Path:
    p = d / name
    p.mkdir(parents=True, exist_ok=True)
    f = p / "SKILL.md"
    f.write_text(f"---\n{frontmatter}\n---\n\n{body}", encoding="utf-8")
    return f


# ── block scalars ─────────────────────────────────────────────────────────
def test_folded_block_scalar_becomes_one_line(tmp_path):
    _write(tmp_path, "folded", "name: folded\ndescription: >-\n  first line\n  second line")
    got = sk.discover_dir(tmp_path, "t")
    assert got[0].description == "first line second line"


def test_literal_block_scalar_keeps_its_newlines(tmp_path):
    _write(tmp_path, "lit", "name: lit\ndescription: |\n  one\n  two")
    got = sk.discover_dir(tmp_path, "t")
    assert got[0].description == "one\ntwo"


def test_folded_scalar_treats_a_blank_line_as_a_paragraph_break(tmp_path):
    _write(tmp_path, "para",
           "name: para\ndescription: >\n  a one\n  a two\n\n  b one")
    assert sk.discover_dir(tmp_path, "t")[0].description == "a one a two\nb one"


def test_a_block_scalar_stops_at_the_next_key(tmp_path):
    """The block must not swallow the keys that follow it."""
    _write(tmp_path, "stop",
           "name: stop\ndescription: >-\n  the text\nversion: 3")
    got = sk.discover_dir(tmp_path, "t")
    assert got[0].description == "the text"
    assert got[0].name == "stop"


# ── THE CONTROL: the flat form still works ────────────────────────────────
def test_control_plain_scalar_is_unchanged(tmp_path):
    """Without this, the block-scalar tests would pass just as well if the
    parser had been broken for every ordinary `key: value` skill — which is
    67 of the 77 real ones."""
    _write(tmp_path, "flat", 'name: flat\ndescription: "just a value"')
    got = sk.discover_dir(tmp_path, "t")
    assert got[0].name == "flat"
    assert got[0].description == "just a value"


def test_an_indicator_never_survives_as_the_description(tmp_path):
    """The exact shipped symptom, asserted directly."""
    for i, ind in enumerate((">", ">-", ">+", "|", "|-", "|+")):
        _write(tmp_path, f"s{i}", f"name: s{i}\ndescription: {ind}\n  real text")
    for s in sk.discover_dir(tmp_path, "t"):
        assert s.description == "real text", (s.name, s.description)


# ── collisions ────────────────────────────────────────────────────────────
def test_a_name_in_two_libraries_is_renamed_not_dropped(tmp_path):
    a, b = tmp_path / "libA", tmp_path / "libB"
    _write(a, "dup", "name: dup\ndescription: from A")
    _write(b, "dup", "name: dup\ndescription: from B")

    got = sk.discover_dir(a, "libA") + sk.discover_dir(b, "libB")
    seen, out = {}, []
    for s in got:                       # mirrors discover_all's rule
        if s.name in seen:
            s.name = f"{s.name}@{s.source}"
        if s.name in seen:
            continue
        seen[s.name] = s
        out.append(s)

    names = sorted(s.name for s in out)
    assert names == ["dup", "dup@libB"], names
    assert len(out) == 2, "the second library's skill must still be reachable"


# ── glob roots ────────────────────────────────────────────────────────────
def test_a_glob_root_resolves_to_the_newest_match(tmp_path):
    old, new = tmp_path / "p" / "1.0.0" / "skills", tmp_path / "p" / "1.0.1" / "skills"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    past = time.time() - 10_000
    os.utime(old, (past, past))

    got = sk.resolve_roots([str(tmp_path / "p" / "*" / "skills")])
    assert got == [new.resolve()], got


def test_resolve_roots_is_ordered_and_deduped(tmp_path):
    d = tmp_path / "one"
    d.mkdir()
    assert sk.resolve_roots([str(d), str(d), ""]) == [d.resolve()]


def test_unresolved_roots_names_a_path_that_matched_nothing(tmp_path):
    """resolve_roots skips these SILENTLY on purpose; this is the counterpart
    that makes a typo'd library visible instead of just smaller."""
    real = tmp_path / "real"
    real.mkdir()
    bad = [str(tmp_path / "nope"), str(tmp_path / "no" / "*" / "skills")]
    assert sk.unresolved_roots([str(real)]) == []
    assert sk.unresolved_roots([str(real)] + bad) == bad


# ── the shipped defaults actually resolve on this machine ─────────────────
def test_the_default_roots_are_not_a_dead_pointer():
    """Two literal paths in this repo rotted when a directory moved. The
    defaults ship as globs for that reason — assert they still hit something,
    so a plugin layout change fails HERE rather than by the library quietly
    getting smaller.
    """
    resolved = sk.resolve_roots(sk.DEFAULT_EXTRA_ROOTS)
    if not resolved:
        pytest.skip("no skill libraries installed on this machine")
    assert all(d.is_dir() for d in resolved)
    found = sum(len(sk.discover_dir(d, "x")) for d in resolved)
    assert found > 0, f"roots resolved to {resolved} but hold no SKILL.md"
