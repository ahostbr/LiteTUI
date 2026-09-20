"""Single user-wide transactional reservation store, independent of worktree."""
from contextlib import contextmanager
from pathlib import Path
import os
import sqlite3


def coordinator_path():
    # Deliberately not LITETUI_DATA_ROOT: workspaces share physical memory.
    base = Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local' / 'share'))
    return base / 'LiteTUI' / 'resources.sqlite3'


class ResourceStore:
    def __init__(self, path=None):
        self.path = Path(path) if path is not None else coordinator_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS reservations '
                       '(id TEXT PRIMARY KEY, owner TEXT NOT NULL, demand TEXT NOT NULL, '
                       'state TEXT NOT NULL, created REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS models '
                       '(identity TEXT PRIMARY KEY, owned INTEGER NOT NULL, '
                       'keep_warm INTEGER NOT NULL, state TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS leases '
                       '(id TEXT PRIMARY KEY, reservation TEXT UNIQUE NOT NULL, '
                       'owner TEXT NOT NULL, model TEXT NOT NULL, active INTEGER NOT NULL)')

            db.execute('CREATE TABLE IF NOT EXISTS reload_claims '
                       '(reservation TEXT PRIMARY KEY, lease TEXT NOT NULL, model TEXT UNIQUE NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS unload_claims '
                       '(model TEXT PRIMARY KEY, lease TEXT NOT NULL, owner TEXT NOT NULL)')

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
