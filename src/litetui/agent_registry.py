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
            columns = {row[1] for row in db.execute('PRAGMA table_info(agents)')}
            if 'parent_conversation' not in columns:
                db.execute('ALTER TABLE agents ADD COLUMN parent_conversation TEXT')
            if 'recovery_marker' not in columns:
                db.execute('ALTER TABLE agents ADD COLUMN recovery_marker TEXT')

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

    def claim(self, parent, child_id, *, limit, parent_conversation=None):
        parent, child_id = _identity(parent), _identity(child_id)
        if parent_conversation is not None:
            parent_conversation = _identity(parent_conversation)
        if type(limit) is not int or limit < 1:
            raise LaunchBlocked('Invalid concurrency budget')
        with self._transaction() as db:
            # Database-wide bound, not per process or per connection.
            count = db.execute("SELECT count(*) FROM agents WHERE state != 'settled'").fetchone()[0]
            if count >= limit:
                raise LaunchBlocked('Child concurrency budget exhausted')
            try:
                db.execute('INSERT INTO agents(child_id,parent,state,created,parent_conversation) '
                           'VALUES (?,?,?,?,?)',
                           (child_id, parent, 'claimed', time.time(), parent_conversation))
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

    def parent_conversation(self, parent, child_id):
        """Trusted launch routing remains available after process settlement."""
        with self._transaction() as db:
            row = db.execute('SELECT parent_conversation FROM agents WHERE parent=? AND child_id=?',
                             (_identity(parent), _identity(child_id))).fetchone()
        return row[0] if row else None

    def completion_route(self, parent, event):
        """Return launch routing only for the authenticated child's conversation."""
        result = event['result']
        with self._transaction() as db:
            row = db.execute('SELECT parent_conversation,conversation_id,completion_id '
                             'FROM agents WHERE parent=? AND child_id=?',
                             (_identity(parent), _identity(result['child_id']))).fetchone()
        if (row is None or not row['parent_conversation']
                or row['conversation_id'] != result.get('conversation_id')
                or (row['completion_id'] and row['completion_id'] != event['completion_id'])):
            return None
        return row['parent_conversation']

    def active(self, parent):
        with self._transaction() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM agents WHERE parent=? AND state != 'settled' ORDER BY created,child_id",
                (_identity(parent),))]

    def mark_recovery(self, parent, child_id, note):
        """Durable, best-effort flag that a retained claim needs recovery.

        Written to the registry -- the store that is still healthy when the
        inbox outcome write failed. Pure annotation: it never changes the claim
        state, never releases the concurrency slot, and never invents a result.
        A marker write that fails (or a claim already settled) returns False and
        leaves the already-retained claim untouched; the caller must not let it
        mask the original failure.
        """
        parent, child_id = _identity(parent), _identity(child_id)
        if not isinstance(note, str) or not note:
            raise ValueError('Recovery marker requires a non-empty note')
        try:
            with self._transaction() as db:
                return db.execute(
                    "UPDATE agents SET recovery_marker=? WHERE parent=? AND child_id=? "
                    "AND state != 'settled'",
                    (note, parent, child_id)).rowcount == 1
        except Exception:
            return False

    def needs_recovery(self, parent):
        """Active claims whose outcome persistence or settlement did not complete.

        Returns the retained claims carrying a recovery marker, so a stranded
        no-outcome claim is distinguishable from a healthy active one and a
        startup recovery can surface it. Reconciliation settles (and clears) a
        marked claim once a matching confirmed durable outcome exists.
        """
        with self._transaction() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM agents WHERE parent=? AND state != 'settled' "
                "AND recovery_marker IS NOT NULL ORDER BY created,child_id",
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
            db.execute("UPDATE agents SET state='settled', completion_id=?, recovery_marker=NULL "
                       "WHERE parent=? AND child_id=?",
                       (completion_id, parent, child_id))

    def observe(self, parent, *, probe):
        rows = self.active(parent)
        for row in rows:
            current = probe(row['pid']) if row['pid'] else None
            row['identity_state'] = ('unknown' if not current else
                                     'matching' if current == row['process_created'] else 'mismatch')
        return rows

    def settle_completion(self, parent, child_id, *, inbox, completion_id):
        """Release a bound hosted claim only against its immutable stored outcome.

        This accounts for a process slot, never a local model resource lease.
        Registry and inbox use separate transactions: a crash leaves a retained
        claim and retry is safe because completion payloads are immutable.
        """
        result = inbox.get(parent, completion_id)
        if (result is None or result.get('child_id') != child_id
                or result.get('cleanup', {}).get('state') != 'confirmed'):
            raise LaunchBlocked('Matching durable completion with confirmed cleanup required')
        with self._transaction() as db:
            row = db.execute('SELECT * FROM agents WHERE parent=? AND child_id=?',
                             (_identity(parent), _identity(child_id))).fetchone()
            if (row is None or not row['conversation_id']
                    or row['conversation_id'] != result.get('conversation_id')
                    or (row['completion_id'] and row['completion_id'] != completion_id)):
                raise LaunchBlocked('Completion differs from registered child identity')
            db.execute("UPDATE agents SET state='settled', completion_id=?, recovery_marker=NULL "
                       "WHERE parent=? AND child_id=?",
                       (completion_id, parent, child_id))
    def reconcile(self, parent, *, inbox):
        """Settle retained claims with known completions; never ACK parent mail.

        No completion, unknown identity, or unconfirmed cleanup remains active.
        Does not infer process death from a failed PID probe or terminate PIDs.
        """
        settled = []
        for row in self.active(parent):
            event = inbox.for_child(parent, row['child_id'])
            if event is None:
                continue
            try:
                self.settle_completion(parent, row['child_id'], inbox=inbox,
                                       completion_id=event['completion_id'])
            except LaunchBlocked:
                continue
            settled.append(event['completion_id'])
        return settled