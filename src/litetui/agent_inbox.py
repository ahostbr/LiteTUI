"""Durable child completions with at-least-once notification and explicit ACK.

Parent routing is supplied by trusted runtime, never accepted from model args.
Consumers deduplicate on completion_id and acknowledge only after their own
inbox commit. This store does not promise exactly-once network delivery.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3
import time
from uuid import uuid4


def _identity(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value):
        raise ValueError('Invalid agent or conversation identity')
    return value


def _payload(result):
    if not isinstance(result, dict):
        raise ValueError('Completion must be an object')
    _identity(result.get('child_id'))
    _identity(result.get('conversation_id'))
    if result.get('status') not in ('completed', 'failed', 'cancelled', 'blocked', 'lost'):
        raise ValueError('Completion must have an explicit terminal status')
    if not isinstance(result.get('summary'), str):
        raise ValueError('Completion summary required')
    if not isinstance(result.get('cleanup'), dict):
        raise ValueError('Resource cleanup outcome required')
    if not isinstance(result.get('evidence'), list) or any(not isinstance(x, str) for x in result['evidence']):
        raise ValueError('Evidence must be a list of paths or commands')
    return json.dumps(result, sort_keys=True, separators=(',', ':'), allow_nan=False)


class AgentInbox:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS completions ('
                       'id TEXT PRIMARY KEY, parent TEXT NOT NULL, child TEXT NOT NULL, '
                       'payload TEXT NOT NULL, created REAL NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0, '
                       'UNIQUE(parent, child))')

    @contextmanager
    def _transaction(self):
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
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

    def persist(self, parent, result):
        parent = _identity(parent)
        payload = _payload(result)
        with self._transaction() as db:
            prior = db.execute('SELECT id,payload FROM completions WHERE parent=? AND child=?',
                               (parent, result['child_id'])).fetchone()
            if prior:
                if prior[1] != payload:
                    raise ValueError('Completion conflict: terminal result is immutable')
                return prior[0]
            completion = uuid4().hex
            db.execute('INSERT INTO completions(id,parent,child,payload,created) VALUES (?,?,?,?,?)',
                       (completion, parent, result['child_id'], payload, time.time()))
            return completion

    def pending(self, parent):
        with self._transaction() as db:
            rows = db.execute('SELECT id,payload FROM completions WHERE parent=? AND acknowledged=0 ORDER BY created,id',
                              (_identity(parent),)).fetchall()
        return [{'completion_id': ident, 'result': json.loads(payload)} for ident, payload in rows]

    def get(self, parent, completion):
        with self._transaction() as db:
            row = db.execute('SELECT payload FROM completions WHERE parent=? AND id=?',
                             (_identity(parent), completion)).fetchone()
        return json.loads(row[0]) if row else None

    def acknowledge(self, parent, completion):
        with self._transaction() as db:
            return db.execute('UPDATE completions SET acknowledged=1 WHERE parent=? AND id=?',
                              (_identity(parent), completion)).rowcount == 1

    def publish(self, parent, result, *, notify):
        completion = self.persist(parent, result)
        notify({'completion_id': completion, 'result': self.get(parent, completion)})
        return completion
