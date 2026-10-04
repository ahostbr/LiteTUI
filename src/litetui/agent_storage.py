"""Read child conversation evidence from owned catalog first, archives read-only."""
from pathlib import Path
from litetui.agent_inbox import _identity
from litetui.agent_launcher import LaunchBlocked
from litetui.agent_store import StoreError, _unlinked
from litetui.storage_catalog import conversations


def conversation_evidence(data_root, conversation_id):
    """Verify materialized evidence without acquiring leases or modifying stores."""
    identity = _identity(conversation_id)
    try:
        rows = [row for row in conversations(Path(data_root), include_archives=True)
                if row.conversation_id == identity]
        owned = [row for row in rows if not row.archive]
        if len(owned) > 1:
            raise StoreError('Child conversation has ambiguous owned membership')
        selected = owned or rows
        if len(selected) != 1:
            raise StoreError('Child conversation is absent or ambiguous')
        directory = selected[0].transcript.parent
        result = {'conversation_dir': str(directory)}
        for key, name in (('transcript', 'convo.jsonl'), ('settings', 'settings.json')):
            path = _unlinked(directory / name)
            if not path.is_file():
                raise StoreError(f'Child {key} is absent or outside its conversation')
            result[key] = str(path)
        return result
    except (ValueError, OSError) as exc:
        raise LaunchBlocked(str(exc)) from exc
