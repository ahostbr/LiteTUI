"""Read-only durable child evidence for reload eligibility.

No migrations, acknowledgements, recovery or process probes. Missing stores
mean no persisted work at that path; unreadable/incompatible stores mean unknown.
This is a point-in-time check, not a cross-process launch lock.
"""
from pathlib import Path
import sqlite3
from litetui.agent_inbox import _identity


def children_pending(root, parent):
    """Return exact True/False, or None when evidence cannot establish idle."""
    try:
        _identity(parent)
        root = Path(root)
        checks = (
            ('registry.sqlite', "SELECT 1 FROM agents WHERE parent=? AND state != 'settled' LIMIT 1"),
            ('inbox.sqlite', 'SELECT 1 FROM completions WHERE parent=? AND acknowledged=0 LIMIT 1'),
            ('receipts.sqlite', "SELECT 1 FROM receipts WHERE parent=? AND (applied=0 OR wake_state != 'finished') LIMIT 1"),
        )
        for name, query in checks:
            path = root / name
            try:
                path.stat()
            except FileNotFoundError:
                continue
            db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=0.2)
            try:
                db.execute('PRAGMA query_only=ON')
                if db.execute(query, (parent,)).fetchone() is not None:
                    return True
            finally:
                db.close()
        return False
    except (OSError, ValueError, TypeError, sqlite3.Error):
        return None
