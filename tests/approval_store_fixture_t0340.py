"""Explicit temporary repository preconditions for approval unit fixtures.

This helper's mocked _edit is NOT durable-write proof. Real repository+actual
LiteTUI._edit success/failure controls live in test_approval_persistence_t0340.
"""
from types import MethodType

from litetui.app import LiteTUI
from litetui.conversation import ConversationRepository


def bind_origin(app, directory, *, actual_edit=False):
    directory.mkdir(parents=True, exist_ok=True)
    store = ConversationRepository()
    store.convo_id = directory.name
    store.convo_dir = directory
    store.convo_path = directory / 'convo.jsonl'
    store.acquire = lambda *args: None  # no shared fleet lease in unit fixtures
    app._store = store
    if not any(message.get('role') == 'user' for message in getattr(app, 'conversation', [])):
        app.conversation = [{'role': 'user', 'content': 'explicit test origin'}]
    if actual_edit:
        app._edit = MethodType(LiteTUI._edit, app)
    return store
