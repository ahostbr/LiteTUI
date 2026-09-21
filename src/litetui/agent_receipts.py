"""Parent-owned durable receipt queue, distinct from transient UI delivery.

ACK transfers responsibility here. Applying a receipt to conversation history
is a separate idempotent commit keyed by completion_id, never a bubble display.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import time
from litetui.agent_inbox import _identity, _payload


class ParentReceipts:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._transaction() as db:
            db.execute('CREATE TABLE IF NOT EXISTS receipts ('
                       'parent TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL, '
                       'created REAL NOT NULL, applied INTEGER NOT NULL DEFAULT 0, '
                       'PRIMARY KEY(parent,id))')
            columns = {row[1] for row in db.execute('PRAGMA table_info(receipts)')}
            if 'conversation' not in columns:
                db.execute('ALTER TABLE receipts ADD COLUMN conversation TEXT')
            if 'wake_state' not in columns:
                db.execute("ALTER TABLE receipts ADD COLUMN wake_state TEXT NOT NULL DEFAULT 'pending'")

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

    def replay_from_inbox(self, parent, *, inbox, registry):
        """Transfer only outcomes with durable launch-time parent routing.

        Unknown/legacy launches remain unacknowledged for explicit recovery.
        No dependency on which conversation the UI currently displays.
        """
        def accept(event):
            conversation = registry.completion_route(parent, event)
            if conversation is None:
                return False
            return self.accept_for_conversation(parent, conversation, event)
        return inbox.replay(parent, accept=accept)

    def accept(self, parent, event):
        parent = _identity(parent)
        ident = _identity(event['completion_id'])
        payload = _payload(event['result'])
        with self._transaction() as db:
            row = db.execute('SELECT payload FROM receipts WHERE parent=? AND id=?',
                             (parent, ident)).fetchone()
            if row:
                if row[0] != payload:
                    raise ValueError('Parent receipt conflict')
            else:
                db.execute('INSERT INTO receipts(parent,id,payload,created) VALUES (?,?,?,?)',
                           (parent, ident, payload, time.time()))
        return True

    def accept_for_conversation(self, parent, conversation, event):
        """Bind using trusted launch context, never the currently selected chat.

        Payload and routing commit atomically before the source can be ACKed.
        Legacy unbound receipts require an explicit trusted binding; selecting
        a conversation alone must never claim them.
        """
        parent, conversation = _identity(parent), _identity(conversation)
        ident = _identity(event['completion_id'])
        payload = _payload(event['result'])
        with self._transaction() as db:
            row = db.execute('SELECT payload,conversation FROM receipts WHERE parent=? AND id=?',
                             (parent, ident)).fetchone()
            if row:
                if row[0] != payload:
                    raise ValueError('Parent receipt conflict')
                if row[1] not in (None, conversation):
                    raise ValueError('Parent receipt conversation conflict')
                db.execute('UPDATE receipts SET conversation=? WHERE parent=? AND id=?',
                           (conversation, parent, ident))
            else:
                db.execute('INSERT INTO receipts(parent,id,payload,created,conversation) '
                           'VALUES (?,?,?,?,?)', (parent, ident, payload, time.time(), conversation))
        return True

    def pending_for_conversation(self, parent, conversation):
        with self._transaction() as db:
            rows = db.execute('SELECT id,payload FROM receipts WHERE parent=? AND conversation=? '
                              'AND applied=0 ORDER BY created,id',
                              (_identity(parent), _identity(conversation))).fetchall()
        return [{'completion_id': ident, 'result': json.loads(payload)} for ident, payload in rows]

    def claim_wake(self, parent, conversation):
        """Claim once before inference; interrupted claims require reconciliation.

        Never retry an uncertain model turn automatically: it may have already
        executed tools. History delivery alone does not prove inference ran.
        """
        scope = (_identity(parent), _identity(conversation))
        with self._transaction() as db:
            rows = db.execute("SELECT id FROM receipts WHERE parent=? AND conversation=? "
                              "AND applied=1 AND wake_state='pending' ORDER BY created,id", scope).fetchall()
            db.execute("UPDATE receipts SET wake_state='claimed' WHERE parent=? AND conversation=? "
                       "AND applied=1 AND wake_state='pending'", scope)
        return [row[0] for row in rows]

    def uncertain_wakes(self, parent, conversation):
        with self._transaction() as db:
            rows = db.execute("SELECT id FROM receipts WHERE parent=? AND conversation=? "
                              "AND wake_state='claimed' ORDER BY created,id",
                              (_identity(parent), _identity(conversation))).fetchall()
        return [row[0] for row in rows]

    def finish_wake(self, parent, conversation, completions):
        scope = (_identity(parent), _identity(conversation))
        identities = [_identity(ident) for ident in completions]
        with self._transaction() as db:
            for ident in identities:
                row = db.execute('SELECT wake_state FROM receipts WHERE parent=? AND conversation=? AND id=?',
                                 (*scope, ident)).fetchone()
                if row is None or row[0] not in ('claimed', 'finished'):
                    raise ValueError('Wake completion ownership or state conflict')
            for ident in identities:
                db.execute("UPDATE receipts SET wake_state='finished' WHERE parent=? AND conversation=? AND id=?",
                           (*scope, ident))
        return True

    def release_wake(self, parent, conversation, completions):
        """Return claimed wakes to 'pending' for a clean re-fire.

        ONLY valid when the turn provably never started — a pre-generation gate
        deferral (see TurnDeferred). A claimed wake whose turn MAY have run must
        stay claimed (uncertain); releasing it would re-run tools. Enforced by
        refusing any id not currently 'claimed'.
        """
        scope = (_identity(parent), _identity(conversation))
        identities = [_identity(ident) for ident in completions]
        with self._transaction() as db:
            for ident in identities:
                row = db.execute('SELECT wake_state FROM receipts WHERE parent=? AND conversation=? AND id=?',
                                 (*scope, ident)).fetchone()
                if row is None or row[0] != 'claimed':
                    raise ValueError('Wake release ownership or state conflict')
            for ident in identities:
                db.execute("UPDATE receipts SET wake_state='pending' WHERE parent=? AND conversation=? AND id=?",
                           (*scope, ident))
        return True

    def pending(self, parent):
        with self._transaction() as db:
            rows = db.execute('SELECT id,payload FROM receipts WHERE parent=? AND applied=0 '
                              'ORDER BY created,id', (_identity(parent),)).fetchall()
        return [{'completion_id': ident, 'result': json.loads(payload)} for ident, payload in rows]

    def mark_applied(self, parent, completion):
        """Caller must already have durably committed to parent history."""
        with self._transaction() as db:
            return db.execute('UPDATE receipts SET applied=1 WHERE parent=? AND id=?',
                              (_identity(parent), _identity(completion))).rowcount == 1

    def deliver_for_conversation(self, parent, conversation, *, commit):
        """Apply only receipts explicitly bound to this conversation.

        The consumer must revalidate its live conversation after any await;
        this synchronous dispatch supplies a stable, trusted routing identity.
        """
        applied = []
        for event in self.pending_for_conversation(parent, conversation):
            if commit(event) is True and self.mark_applied(parent, event['completion_id']):
                applied.append(event['completion_id'])
        return applied

    def deliver(self, parent, *, commit):
        """Consumer must persist/dedupe in history before returning literal True."""
        applied = []
        for event in self.pending(parent):
            if commit(event) is True and self.mark_applied(parent, event['completion_id']):
                applied.append(event['completion_id'])
        return applied
