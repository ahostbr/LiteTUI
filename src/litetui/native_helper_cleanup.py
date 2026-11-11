"""Bounded disposal after an owned helper process tree has already joined."""
from __future__ import annotations

import asyncio
import time


async def remove_directory(temporary, *, wait_seconds):
    """Retry only Windows' late release of a drained helper's private cwd.

    Job completion can precede the release of the directory reference. Keep the
    owner and its TemporaryDirectory intact until removal succeeds; a persistent
    sharing violation or any other filesystem failure still refuses retirement.
    Cancellation propagates with that same retained owner for joined shutdown.
    """
    deadline = time.monotonic() + wait_seconds
    while True:
        try:
            temporary.cleanup()
            return
        except PermissionError as error:
            if getattr(error, "winerror", None) != 32 or time.monotonic() >= deadline:
                raise
        await asyncio.sleep(0.025)
