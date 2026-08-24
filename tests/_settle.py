"""Wait for the CONDITION a swap or an open produces — never for a tick count.

🔴 WHY THIS IS A SHARED MODULE AND NOT A HELPER IN EACH FILE.

`request_swap` does `call_next(ctrl.swap)`, and `swap()` then awaits
`_mount_view()`, whose view defers `_settle` again until the new body has
composed before it calls `apply_carried_state`. So NOTHING a swap produces —
not `ctrl.style`, not the carried highlight, not focus — is observable at a
fixed frame number. It depends on the host, on the body, and on how busy the
loop is. `await pilot.pause()` twice is a guess about all three.

Measured 2026-08-24 at 8484923, six sidebar files, ten iterations of
`pytest -q tests/test_*sidebar*.py`: FOUR tests in THREE files failed
intermittently, all on the same shape — assert carried state N bare pauses
after the swap. The worst ran 3-in-10.

The same mechanism was already fixed twice, in two files, by hand. It came back
in three more because the fix lived in the files rather than in one place. A
per-file copy of this loop is how the fourth instance gets written.

⚠️ THIS MUST NEVER MAKE AN ASSERTION UNFAILABLE. The wait is BOUNDED and its
result is not asserted here: when the product genuinely drops the carried
state, the loop exhausts and the caller's ORIGINAL assertion fires with its
ORIGINAL message. Waiting changes WHEN the state is read, never WHETHER it has
to be right.
"""

from __future__ import annotations

from textual.css.query import NoMatches


async def settle_until(pilot, predicate, n: int = 25) -> bool:
    """Pause up to `n` frames until `predicate()` is true. Returns whether it became true.

    `NoMatches` is swallowed because a predicate legitimately queries a body
    that has not composed yet. Nothing else is: a predicate that raises for any
    other reason is a broken test, and it should surface as that error rather
    than as a quiet timeout.
    """
    for _ in range(n):
        await pilot.pause()
        try:
            if predicate():
                return True
        except NoMatches:
            continue
    return False
