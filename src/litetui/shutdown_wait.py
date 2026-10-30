"""Durable shutdown rendezvous: caller cancellation never abandons an owned task."""
from __future__ import annotations

import asyncio


async def join_task(task):
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                if task.cancelled():
                    return None
                return task.result()
