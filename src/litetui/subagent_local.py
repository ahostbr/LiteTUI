"""Fail-closed expert admission: reuse one resident model, never start a load.

The live server's status is evidence, not cached UI rows. This checks the target
server; it is not a GPU memory estimator or a reservation against unrelated apps.
"""
from __future__ import annotations

import asyncio


async def admit_local(backend, model, enabled):
    if not enabled:
        raise ValueError('Local subagents require the expert allow_local_subagents toggle')
    status = getattr(backend, 'subagent_model_states', None)
    if not callable(status):
        raise ValueError('Cannot verify local model loading status; child refused')  # noqa: TRY004 - admission refusal contract
    states = await asyncio.to_thread(status)
    if not isinstance(states, dict) or any(value not in ('loaded', 'loading', 'unloaded') for value in states.values()):
        raise ValueError('Cannot verify local model loading status; child refused')
    if any(value == 'loading' for value in states.values()):
        raise ValueError('A local model is loading; wait until it finishes before running a child')
    if any(key != model and value == 'loaded' for key, value in states.items()):
        raise ValueError('A different local model is loaded; child refused to avoid a second VRAM model')
    if states.get(model) != 'loaded':
        raise ValueError('The requested local child model must be already loaded; subagents never load models')
