"""The one place LiteTUI's version is written.

Kept in its own module rather than in app.py so that reading the version costs
nothing — importing app.py builds a Textual application, and a release script,
a test, or `python -c` should not have to.

🔴 THIS IS THE ONLY COPY. CHANGELOG.md's top released heading must match it,
and tests/test_version.py fails if they drift. Two literals for one fact will
always disagree eventually, and the version is the fact most likely to be read
by someone who cannot check it against anything else.
"""

from __future__ import annotations

__version__ = "0.16.0"

#: Pre-1.0 on purpose. The harness is two days old (first commit
#: 2026-08-18 23:42) and its interfaces are still moving weekly; a 1.0 would
#: promise a stability nobody has offered yet.
__all__ = ["__version__"]
