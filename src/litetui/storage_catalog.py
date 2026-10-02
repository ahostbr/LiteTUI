"""Read-only storage catalog: owned conversations and retained legacy archives.

No locks, repair, directory creation, registry pointers or mutation are permitted.
Owned mutable consumers select a validated agent explicitly. Archives never become
owned write targets merely because their UUID/name resembles a current seat.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from litetui.agent_store import AgentStore, StoreError, _unlinked, valid_id


@dataclass(frozen=True)
class ConversationLocation:
    conversation_id: str
    transcript: Path
    agent_id: str | None
    agent_name: str | None
    archive: bool


def owned_transcript(session, conversation_id: str) -> Path:
    """Exact selected agent confinement, including absent staged conversation."""
    session.authority
    return _unlinked(session.conversation_directory(valid_id(conversation_id)) / 'convo.jsonl')


def conversations(data_root: Path | str, *, agent_id: str | None = None,
                  include_archives: bool = False) -> list[ConversationLocation]:
    store = AgentStore(data_root)
    agents = ([store.find_agent(agent_id=agent_id)] if agent_id is not None else store.list_agents())
    result = []
    for agent in agents:
        for directory in store.list_conversations(agent):
            transcript = _unlinked(directory / 'convo.jsonl')
            if transcript.is_file():
                result.append(ConversationLocation(directory.name, transcript, agent.agent_id, agent.name, False))
    if include_archives:
        source = _unlinked(store.legacy_root)
        if source.exists():
            for directory in sorted(source.iterdir(), key=lambda p: p.name):
                _unlinked(directory)
                if not directory.is_dir():
                    continue
                # Preserve even malformed legacy folder names for readonly
                # browsing/export; never use them as owned IDs or selectors.
                transcript = _unlinked(directory / 'convo.jsonl')
                if transcript.is_file():
                    result.append(ConversationLocation(directory.name, transcript, None, None, True))
    return result


def locate_owned(data_root: Path | str, conversation_id: str, *,
                 agent_id: str | None = None) -> ConversationLocation:
    identity = valid_id(conversation_id)
    rows = [row for row in conversations(data_root, agent_id=agent_id)
            if row.conversation_id == identity]
    if len(rows) != 1:
        raise StoreError('Owned conversation is absent or ambiguous; select its agent first')
    return rows[0]
