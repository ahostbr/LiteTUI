"""The version and the changelog must agree.

Two literals for one fact will always disagree eventually, and the version is
the fact most likely to be read by someone with nothing to check it against.
This is the check.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from version import __version__  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"

#: "## [0.7.0] — 2026-08-20"  — em dash or hyphen, either is accepted.
RELEASE_HEADING = re.compile(r"^## \[(\d+\.\d+\.\d+)\]\s*[—-]\s*(\d{4}-\d{2}-\d{2})\s*$", re.M)
SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


def _text() -> str:
    return CHANGELOG.read_text(encoding="utf-8")


def test_changelog_exists():
    assert CHANGELOG.is_file(), "CHANGELOG.md is the only human-readable history there is"


def test_version_is_semver():
    assert SEMVER.match(__version__), f"{__version__!r} is not MAJOR.MINOR.PATCH"


def test_top_release_matches_version():
    """THE POINT OF THIS FILE. version.py and the changelog's newest released
    heading are the same fact written twice; this is what stops them drifting."""
    releases = RELEASE_HEADING.findall(_text())
    assert releases, "no release headings found — the regex or the format changed"
    newest = releases[0][0]
    assert newest == __version__, (
        f"version.py says {__version__}, newest CHANGELOG release is {newest}"
    )


def test_releases_are_ordered_newest_first():
    # A changelog read top-down must descend. Out of order, the "newest" entry
    # the test above pins is not the newest, and the check passes while
    # guarding the wrong line.
    versions = [tuple(int(p) for p in v.split(".")) for v, _ in RELEASE_HEADING.findall(_text())]
    assert versions == sorted(versions, reverse=True), f"out of order: {versions}"


def test_release_versions_are_unique():
    versions = [v for v, _ in RELEASE_HEADING.findall(_text())]
    assert len(versions) == len(set(versions)), f"duplicate release: {versions}"


def test_dates_are_ordered_newest_first():
    dates = [d for _, d in RELEASE_HEADING.findall(_text())]
    assert dates == sorted(dates, reverse=True), f"dates out of order: {dates}"


def test_unreleased_section_exists():
    # Work that has landed but is not shipped needs somewhere honest to sit.
    # Without this section it either gets omitted or gets misfiled under a
    # released version, and both read as "shipped" to anyone downstream.
    assert "## [Unreleased]" in _text()


def test_CONTROL_the_heading_regex_actually_matches_this_changelog():
    # A regex that matches nothing makes every test above vacuously true: no
    # headings found, nothing compared, all green. Pin a real count.
    assert len(RELEASE_HEADING.findall(_text())) >= 7


def test_CONTROL_the_regex_rejects_a_malformed_heading():
    # And it must not match anything shaped roughly like a heading, or the
    # ordering checks would be comparing noise.
    for bad in ("## [1.0] — 2026-08-20", "## 0.7.0 — 2026-08-20", "### [0.7.0] — 2026-08-20"):
        assert not RELEASE_HEADING.findall(bad), f"should not match: {bad}"
