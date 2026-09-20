"""Durable launch claims. Observation is not permission to kill or release.

Internal registry: callers must verify completion and actual process/resource
cleanup before settlement. Unknown identities retain their concurrency slot.
"""
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import time
from litetui.agent_inbox import _identity
from litetui.agent_launcher import LaunchBlocked


class AgentRegistry:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS agents ('
                       'child_id TEXT PRIMARY KEY, parent TEXT NOT NULL, '
                       'state TEXT NOT NULL, conversation_id TEXT, pid INTEGER, '
                       'process_created TEXT, completion_id TEXT, created REAL NOT NULL)')

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA synchronous=FULL')
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def claim(self, parent, child_id, *, limit):
        parent, child_id = _identity(parent), _identity(child_id)
        if type(limit) is not int or limit < 1:
            raise LaunchBlocked('Invalid concurrency budget')
        with self._transaction() as db:
            # Database-wide bound, not per process or per connection.
            count = db.execute("SELECT count(*) FROM agents WHERE state != 'settled'").fetchone()[0]
            if count >= limit:
                raise LaunchBlocked('Child concurrency budget exhausted')
            try:
                db.execute('INSERT INTO agents(child_id,parent,state,created) VALUES (?,?,?,?)',
                           (child_id, parent, 'claimed', time.time()))
            except sqlite3.IntegrityError as exc:
                raise LaunchBlocked('Child identity already claimed') from exc

    def bind(self, parent, child_id, *, conversation_id, pid, created):
        parent, child_id = _identity(parent), _identity(child_id)
        conversation_id = _identity(conversation_id)
        if type(pid) is not int or pid <= 0 or not isinstance(created, str) or not created:
            raise LaunchBlocked('Verified child process identity required')
        with self._transaction() as db:
            changed = db.execute("UPDATE agents SET state='running', conversation_id=?, pid=?, "
                                 "process_created=? WHERE parent=? AND child_id=? AND state='claimed'",
                                 (conversation_id, pid, created, parent, child_id)).rowcount
            if changed != 1:
                raise LaunchBlocked('Child is not an unbound claim owned by this parent')

    def active(self, parent):
        with self._transaction() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM agents WHERE parent=? AND state != 'settled' ORDER BY created,child_id",
                (_identity(parent),))]

    def settle(self, parent, child_id, *, completion_id, cleanup_confirmed):
        if cleanup_confirmed is not True or not completion_id:
            raise LaunchBlocked('Durable completion and confirmed cleanup required')
        completion_id = _identity(completion_id)
        with self._transaction() as db:
            row = db.execute('SELECT * FROM agents WHERE parent=? AND child_id=?',
                             (_identity(parent), _identity(child_id))).fetchone()
            if row is None or (row['completion_id'] and row['completion_id'] != completion_id):
                raise LaunchBlocked('Child settlement ownership or completion conflict')
            db.execute("UPDATE agents SET state='settled', completion_id=? WHERE parent=? AND child_id=?",
                       (completion_id, parent, child_id))

    def observe(self, parent, *, probe):
        rows = self.active(parent)
        for row in rows:
            current = probe(row['pid']) if row['pid'] else None
            row['identity_state'] = ('unknown' if not current else
                                     'matching' if current == row['process_created'] else 'mismatch')
        return rows
