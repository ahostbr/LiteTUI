"""Explicit UI launch of another owned agent in its own process/seat.

No identity transfer, registration, memory copy or archive mutation in this host.
The canonical harness independently validates the selected folder and launch.
"""
import asyncio
import os
from pathlib import Path
import sys

from litetui.agent_store import AgentStore, StoreError, valid_id


async def open_seat(app, agent_id: str, conversation_id: str | None) -> str:
    from litetui import paths
    session = getattr(app, '_agent_session', None)
    root = session.store.data_root if session is not None else paths.data_root()
    store = AgentStore(root)
    agent = store.find_agent(agent_id=valid_id(agent_id))
    if session is not None and session.authority.agent_id == agent.agent_id:
        raise StoreError('This agent already owns the current seat')
    if conversation_id is not None:
        target = store.conversation_directory(agent, valid_id(conversation_id)) / 'convo.jsonl'
        if not target.is_file():
            raise StoreError('Selected conversation is absent from the agent')
    seat = app.seat
    if not getattr(seat, 'registered', False) or not getattr(seat, 'agent_id', None):
        raise StoreError('Opening another seat requires registered caller identity')
    command = [sys.executable, '-m', 'liteharness.cli', 'spawn', '--split', '--cli', 'litetui',
               '--resume', agent.name, '--cwd', str(Path.cwd()),
               '--tier', seat.tier, '--spawned-by', seat.agent_id]
    if conversation_id is not None:
        command.extend(['--convo', conversation_id])
    env = dict(os.environ, LITETUI_DATA_ROOT=str(root))
    process = await asyncio.create_subprocess_exec(*command, env=env,
                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=120)
    except BaseException:
        if process.returncode is None:
            process.kill()
            await process.wait()
        # Never kill a possibly created seat by guessed name/id after uncertain transport.
        raise
    output = (stdout + stderr).decode('utf-8', errors='replace').strip()
    if process.returncode != 0:
        raise StoreError(f'Own-seat launch refused: {output or process.returncode}')
    return output or f'Opened {agent.name} in its own seat'
