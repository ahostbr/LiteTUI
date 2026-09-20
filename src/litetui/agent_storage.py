"""Locate child-owned existing conversation storage; never create a profile copy."""
from pathlib import Path
from litetui.agent_inbox import _identity
from litetui.agent_launcher import LaunchBlocked


def conversation_evidence(data_root, conversation_id):
    """Verify materialization after the child's first prompt, not just readiness.

    Caller owns the lifetime of data_root: it must not be a temporary directory
    for production launches. This function neither acquires the child's write
    lease nor modifies/copies its transcript or settings.
    """
    identity = _identity(conversation_id)
    root = (Path(data_root).resolve() / '.convos').resolve()
    directory = (root / identity).resolve()
    if directory.parent != root:
        raise LaunchBlocked('Child conversation storage escapes its data root')
    result = {'conversation_dir': str(directory)}
    for key, name in (('transcript', 'convo.jsonl'), ('settings', 'settings.json')):
        path = directory / name
        if path.resolve().parent != directory or not path.is_file():
            raise LaunchBlocked(f'Child {key} is absent or outside its conversation')
        result[key] = str(path.resolve())
    return result
