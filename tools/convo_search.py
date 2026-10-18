#!/usr/bin/env python3
"""convo_search — ranked search over LiteHarness conversation transcripts.

One tier, over the raw layer (per Sentinel's measured corpus analysis):
  * SQLite FTS5, one row per message: convo_id, ts, role, text
  * role-split FTS columns so bm25 can weight user/assistant >> tool
  * convos meta table (agent_name, agent_id, model, created) for --agent/--since
  * incremental: (file_size, byte_offset) per convo, append-only tail parse,
    full re-parse on file shrink
  * images -> "[image: N KB]" placeholder, never indexed as base64

Usage:
  python convo_search.py "query words"            one row per convo (limit 10, best match as snippet)
  python convo_search.py "query words" --messages flat per-message view
  python convo_search.py "query" --agent LiteTUI --since 30
  python convo_search.py --show <convo-id-or-prefix>
  python convo_search.py --raw <convo-id-or-prefix> "regex"  refuses: no bounded ownership resolver
  python convo_search.py --refresh                detached hidden refresh; immediate log receipt
  python convo_search.py --reindex                detached full rebuild; immediate log receipt
  python convo_search.py --stats

Lookup reads only a root-bound index snapshot. It never builds or scans sources.
The first result line reports the last successful refresh UTC, or UNKNOWN.
"""
import argparse
import hashlib
import io
import json
import os
import re
import sqlite3
import sys
import time
import subprocess
import shlex
import uuid
from datetime import datetime, timezone

from pathlib import Path

# Direct script execution uses the package shipped with this checkout.
if str(Path(__file__).resolve().parents[1] / 'src') not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from litetui.paths import data_root
from litetui.storage_catalog import conversations
from litetui.agent_store import StoreError

# Published 0.25.0 boundary: conversations under .convos/ and the index at
# tools/convo_search.db, both under the data root (LITETUI_DATA_ROOT when set).
_DATA_ROOT = data_root()
ROOT = str(_DATA_ROOT / ".convos")
DB_PATH = str(_DATA_ROOT / "tools" / "convo_search.db")


def catalog_paths():
    rows = conversations(_DATA_ROOT, include_archives=True)
    selected = {}
    for row in rows:
        previous = selected.get(row.conversation_id)
        if previous is not None:
            if not previous.archive and not row.archive:
                raise StoreError('Search conversation has ambiguous owned membership')
            if not previous.archive:
                continue
        selected[row.conversation_id] = row
    return {cid: row.transcript for cid, row in selected.items()}


def schema(conn):
    conn.executescript(SCHEMA)
    columns = {row[1] for row in conn.execute('PRAGMA table_info(convos)')}
    for name, kind in (('source_path', 'TEXT'), ('mtime_ns', 'INTEGER')):
        if name not in columns:
            conn.execute(f'ALTER TABLE convos ADD COLUMN {name} {kind}')
MAX_MSG_CHARS = 100_000  # cap per message stored in the index (raw stays on disk)

SCHEMA = """
CREATE TABLE IF NOT EXISTS convos(
  id TEXT PRIMARY KEY,
  agent_name TEXT, agent_id TEXT, model TEXT,
  created REAL,
  msg_count INTEGER DEFAULT 0,
  ua_chars INTEGER DEFAULT 0,
  turns INTEGER DEFAULT 0,
  file_size INTEGER DEFAULT 0,
  byte_offset INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS messages(
  convo_id TEXT NOT NULL,
  ts REAL,
  role TEXT,
  text TEXT,
  hash TEXT,
  UNIQUE(convo_id, hash)
);
CREATE INDEX IF NOT EXISTS idx_messages_convo ON messages(convo_id, ts);
CREATE VIRTUAL TABLE IF NOT EXISTS msgs_fts USING fts5(
  convo_id UNINDEXED, ts UNINDEXED, role UNINDEXED,
  user_text, assistant_text, tool_text, other_text,
  tokenize='unicode61'
);
"""

ROLE_COL = {"user": "user_text", "assistant": "assistant_text", "tool": "tool_text"}
ROLE_COLIDX = {"user": 4, "assistant": 5, "tool": 6, "other": 7}
# bm25 weights: convo_id, ts, role unweighted; user 10, assistant 10, tool 1, other 0.5
BM25_WEIGHTS = "0,0,0,10.0,10.0,1.0,0.5"


def content_text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        t = content
    elif isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                t = p.get("type")
                if t == "text":
                    parts.append(p.get("text") or "")
                elif t == "image_url":
                    url = (p.get("image_url") or {}).get("url", "") or ""
                    data = url.split(",", 1)[-1] if url.startswith("data:") else url
                    parts.append(f"[image: {len(data) // 1024} KB]")
                elif t == "tool_use":
                    parts.append(f"[tool_use {p.get('name', '?')}]")
                else:
                    s = json.dumps(p, ensure_ascii=False, default=str)
                    parts.append(s if len(s) < 5000 else s[:5000] + " …")
            # unknown parts: skip
        t = "\n".join(parts)
    else:
        t = json.dumps(content, ensure_ascii=False, default=str)
    if len(t) > MAX_MSG_CHARS:
        t = t[:MAX_MSG_CHARS] + f" …[truncated, {len(t) // 1000}KB total]"
    return t


def msg_hash(convo_id, role, text) -> str:
    return hashlib.sha1(f"{convo_id}|{role}|{text}".encode("utf-8", "replace")).hexdigest()


def iter_messages(obj):
    """Yield message dicts from one parsed jsonl line."""
    t = obj.get("type")
    if t == "snapshot":
        for m in obj.get("messages") or []:
            if isinstance(m, dict):
                yield m
    elif t == "msg":
        m = obj.get("message")
        if isinstance(m, dict):
            yield m
    elif t == "truncate":
        for m in obj.get("prepend") or []:
            if isinstance(m, dict):
                yield m


def parse_line(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception:
        return None


def index_convo(conn, path, force=False):
    convo_id = path.parent.name
    stat = path.stat()
    size = stat.st_size
    row = conn.execute("SELECT file_size, byte_offset, source_path, mtime_ns FROM convos WHERE id=?", (convo_id,)).fetchone()
    stored_size = row[0] if row else -1
    stored_offset = row[1] if row else 0
    changed_source = row is not None and (row[2] != str(path) or row[3] != stat.st_mtime_ns)

    if not force and row and size == stored_size and not changed_source:
        return 0  # up to date

    full = force or size < stored_offset or row is None or (changed_source and size == stored_size) or (row is not None and row[2] != str(path))
    if full:
        conn.execute("DELETE FROM messages WHERE convo_id=?", (convo_id,))
        conn.execute("DELETE FROM msgs_fts WHERE convo_id=?", (convo_id,))
        start = 0
    else:
        start = stored_offset

    new_count = 0
    offset = start
    with open(path, "rb") as f:
        if start:
            f.seek(start)
        while True:
            line = f.readline()
            if not line:
                offset = f.tell()
                break
            if not line.strip():
                offset = f.tell()
                continue
            obj = parse_line(line)
            if obj is None:
                break  # partial write: keep offset at line start, resume next run
            t = obj.get("type")
            if t == "meta":
                conn.execute(
                    """INSERT INTO convos(id, agent_name, agent_id, model, created)
                       VALUES(?,?,?,?,?)
                       ON CONFLICT(id) DO UPDATE SET
                         agent_name=excluded.agent_name, agent_id=excluded.agent_id,
                         model=excluded.model, created=excluded.created""",
                    (convo_id, obj.get("agent_name"),
                     obj.get("agent_id"), obj.get("model"), obj.get("created")),
                )
            else:
                for m in iter_messages(obj):
                    role = m.get("role") or "other"
                    text = content_text(m.get("content"))
                    if not text.strip():
                        continue
                    h = msg_hash(convo_id, role, text)
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO messages(convo_id, ts, role, text, hash) VALUES(?,?,?,?,?)",
                        (convo_id, m.get("ts") or obj.get("ts"), role, text, h),
                    )
                    if cur.rowcount:
                        col = ROLE_COL.get(role, "other_text")
                        conn.execute(
                            f"INSERT INTO msgs_fts(rowid, convo_id, ts, role, {col}) VALUES(?,?,?,?,?)",
                            (cur.lastrowid, convo_id, m.get("ts") or obj.get("ts"), role, text),
                        )
                        new_count += 1
            offset = f.tell()

    conn.execute(
        """INSERT INTO convos(id, file_size, byte_offset, source_path, mtime_ns) VALUES(?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET file_size=excluded.file_size,
             byte_offset=excluded.byte_offset, source_path=excluded.source_path,
             mtime_ns=excluded.mtime_ns""",
        (convo_id, size, offset, str(path), stat.st_mtime_ns),
    )
    stats = conn.execute(
        """SELECT COUNT(*), SUM(LENGTH(text)) FILTER (WHERE role IN ('user','assistant')),
                  SUM(role='user') FROM messages WHERE convo_id=?""",
        (convo_id,),
    ).fetchone()
    conn.execute(
        "UPDATE convos SET msg_count=?, ua_chars=?, turns=? WHERE id=?",
        (stats[0] or 0, stats[1] or 0, stats[2] or 0, convo_id),
    )
    return new_count


INDEX_VERSION = '1'
REFRESH_BUDGET = 3600
FRESHNESS_UNKNOWN = 'Last successful refresh UTC: UNKNOWN'


def root_identity():
    return os.path.normcase(str(_DATA_ROOT.resolve()))


def build_index(force=False):
    convos = catalog_paths()  # validate all owned catalog before creating index state
    conn = open_db()
    t0 = time.time()
    total = 0
    try:
        # Publish rows and successful-refresh metadata together. A failed or killed
        # refresh must not leave partial rows wearing the previous success stamp.
        with conn:
            for cid, path in convos.items():
                total += index_convo(conn, path, force=force)
            for (cid,) in conn.execute('SELECT id FROM convos').fetchall():
                if cid not in convos:
                    conn.execute('DELETE FROM messages WHERE convo_id=?', (cid,))
                    conn.execute('DELETE FROM msgs_fts WHERE convo_id=?', (cid,))
                    conn.execute('DELETE FROM convos WHERE id=?', (cid,))
            conn.execute('CREATE TABLE IF NOT EXISTS search_meta (key TEXT PRIMARY KEY, value TEXT)')
            conn.executemany('INSERT OR REPLACE INTO search_meta VALUES (?, ?)', [
                ('version', INDEX_VERSION), ('root', root_identity()),
                ('refreshed_utc', datetime.now(timezone.utc).isoformat()),
            ])
        print(f"indexed {len(convos)} convos, {total} new message rows in {time.time() - t0:.1f}s -> {DB_PATH}", file=sys.stderr)
    finally:
        conn.close()


def refresh_command():
    argv = [sys.executable, str(Path(__file__).resolve()), '--root', str(_DATA_ROOT.resolve()), '--refresh']
    # Copy/paste into PowerShell on Windows, POSIX shell elsewhere.
    if os.name == 'nt':
        return '& ' + ' '.join("'" + part.replace("'", "''") + "'" for part in argv)
    return shlex.join(argv)


def open_snapshot(*, refusing=True):
    """Read only; no schema migration, creation, source reads or catalog scan."""
    conn = None
    freshness = FRESHNESS_UNKNOWN
    try:
        conn = sqlite3.connect(Path(DB_PATH).resolve().as_uri() + '?mode=ro', uri=True, timeout=0.2)
        conn.execute('BEGIN')  # metadata and results describe the same snapshot
        meta = dict(conn.execute('SELECT key, value FROM search_meta'))
        if meta.get('version') != INDEX_VERSION:
            raise ValueError('incompatible index version')
        if meta.get('root') != root_identity():
            raise ValueError('index belongs to a different data root')
        # Check the fields actually consumed by search/show/stats, without reading sources.
        conn.execute('SELECT id, agent_name, model, created, ua_chars, turns, msg_count, source_path FROM convos LIMIT 0')
        conn.execute('SELECT convo_id, ts, role, text FROM messages LIMIT 0')
        conn.execute('SELECT convo_id, ts, role, user_text, assistant_text, tool_text, other_text FROM msgs_fts LIMIT 0')
        stamp = meta.get('refreshed_utc')
        age = None
        if stamp:
            try:
                refreshed = datetime.fromisoformat(stamp)
                if refreshed.utcoffset() is None:
                    raise ValueError('timestamp has no timezone')
                refreshed = refreshed.astimezone(timezone.utc)
                freshness = 'Last successful refresh UTC: ' + refreshed.isoformat()
                age = (datetime.now(timezone.utc) - refreshed).total_seconds()
            except (ValueError, TypeError):
                pass
        print(freshness, flush=True)
        if age is not None and age > 86400:
            print(f'Warning: index snapshot is {age / 3600:.1f} hours old (over 24h); lookup remains available.')
        return conn
    except (OSError, sqlite3.Error, ValueError) as exc:
        if conn is not None:
            conn.close()
        print(freshness, flush=True)
        if refusing:
            print(f'Refused: missing, incompatible or unavailable index: {exc}')
            print('Refresh explicitly: ' + refresh_command())
        return None


def start_refresh(force=False):
    """Detach a hidden supervisor; the caller never waits for catalog/index work."""
    log_dir = _DATA_ROOT / 'tools' / 'convo-search-refresh'
    log_dir.mkdir(parents=True, exist_ok=True)
    log = log_dir / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex + '.log')
    cmd = [sys.executable, str(Path(__file__).resolve()), '--root', str(_DATA_ROOT.resolve()), '--refresh-worker']
    if force:
        cmd.append('--reindex')
    options = {'creationflags': getattr(subprocess, 'CREATE_NO_WINDOW', 0)} if os.name == 'nt' else {'start_new_session': True}
    with log.open('x', encoding='utf-8') as output:
        output.write(f'Refresh requested UTC: {datetime.now(timezone.utc).isoformat()}; budget {REFRESH_BUDGET}s\n')
        output.flush()
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, close_fds=True, **options)
        except OSError as exc:
            output.write(f'Refresh supervisor launch failed: {exc}\n')
            raise OSError(f'{exc}; log: {log}') from exc
    print(f'Refresh launched (not completed): supervisor PID {proc.pid}; budget {REFRESH_BUDGET}s; log: {log}')
    print('The requesting agent/operator owns monitoring this log for completion, failure or timeout. No automatic watcher.')


def refresh_worker(force=False):
    """Bound the actual refresh in a child, retaining its output in the launch log."""
    cmd = [sys.executable, '-u', str(Path(__file__).resolve()), '--root', str(_DATA_ROOT.resolve()), '--build-index']
    if force:
        cmd.append('--reindex')
    try:
        result = subprocess.run(cmd, stdin=subprocess.DEVNULL, timeout=REFRESH_BUDGET,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        print(f'Refresh finished UTC: {datetime.now(timezone.utc).isoformat()}; exit {result.returncode}', flush=True)
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f'Refresh timed out after {REFRESH_BUDGET}s; child killed and waited; completion unconfirmed, inspect snapshot timestamp.', flush=True)
        return 1
    except OSError as exc:
        print(f'Refresh failed to launch: {exc}', flush=True)
        return 1


def open_db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    schema(conn)
    return conn


def resolve_convo(conn, prefix):
    rows = conn.execute("SELECT id FROM convos WHERE id LIKE ? ORDER BY LENGTH(id) LIMIT 2",
                        (prefix + "%",)).fetchall()
    if len(rows) == 1:
        return rows[0][0]
    if not rows:
        print(f"no convo matching '{prefix}'")
        sys.exit(1)
    print(f"ambiguous prefix '{prefix}': " + ", ".join(r[0] for r in rows))
    sys.exit(1)


def sanitize_query(q: str) -> str:
    toks = re.findall(r"[\w.\-]+", q, re.UNICODE)
    if not toks:
        print("no searchable tokens in query")
        sys.exit(1)
    return " ".join('"%s"' % t.replace('"', "") for t in toks)


def _search_rows(conn, query, limit, agent, since):
    """One row per matched MESSAGE (score ascending worst -> best last)."""
    fts = sanitize_query(query)
    where = []
    if agent:
        where.append("c.agent_name = ?")
    if since is not None:
        where.append("c.created >= ?")
    where_sql = " AND ".join(where) if where else "1=1"
    sql = f"""
      SELECT bm25(msgs_fts, {BM25_WEIGHTS}) AS score,
             m.convo_id, m.ts, m.role, m.text,
             c.agent_name, c.model, c.created, c.ua_chars, c.turns, c.msg_count
      FROM msgs_fts AS f
      JOIN messages AS m ON m.rowid = f.rowid
      JOIN convos   AS c ON c.id = f.convo_id
      WHERE msgs_fts MATCH ? AND {where_sql}
      ORDER BY score LIMIT ?
    """
    params = [fts]
    if agent:
        params.append(agent)
    if since is not None:
        params.append(time.time() - since)
    params.append(limit)
    return conn.execute(sql, params).fetchall()


def do_search(conn, query, limit, agent, since, per_convo=True):
    # Fetch a wider pool so a single convo's many matching messages can't
    # crowd out distinct convos from the default (one-row-per-convo) view.
    pool = limit if not per_convo else max(limit * 10, 200)
    rows = _search_rows(conn, query, pool, agent, since)
    if not rows:
        print("no matches")
        return
    tokens = re.findall(r"[\w.\-]+", query, re.UNICODE)
    now = time.time()
    if not per_convo:
        seen = set()
        for score, cid, ts, role, text, agent_name, model, created, ua_chars, turns, msg_count in rows:
            snip = make_snippet(text, tokens)
            key = (cid, snip)
            if key in seen:
                continue
            seen.add(key)
            age = f"{(now - (created or now)) / 86400:.0f}d"
            print(f"{score:8.2f}  {cid[:8]}  {age:>4}  {role:<9} {agent_name or '?':<10} "
                  f"[ua {ua_chars}ch / {turns}t]  {snip}")
        return
    # Group by convo; rank by the best (lowest) bm25 in the group;
    # snippet = that best message.
    best = {}
    for r in rows:
        cid = r[1]
        if cid not in best or r[0] < best[cid][0]:
            best[cid] = r
    ranked = sorted(best.values(), key=lambda r: r[0])[:limit]
    for score, cid, ts, role, text, agent_name, model, created, ua_chars, turns, msg_count in ranked:
        snip = make_snippet(text, tokens)
        age = f"{(now - (created or now)) / 86400:.0f}d"
        n_hits = sum(1 for r in rows if r[1] == cid)
        print(f"{score:8.2f}  {cid[:8]}  {age:>4}  {role:<9} {agent_name or '?':<10} "
              f"[ua {ua_chars}ch / {turns}t / {n_hits} hits]  {snip}")


def make_snippet(text, tokens, width=60):
    low = text.lower().replace("\n", " ")
    pos = -1
    for t in tokens:
        i = low.find(t.lower())
        if i >= 0 and (pos < 0 or i < pos):
            pos = i
    flat = text.replace("\n", " ")
    if pos < 0:
        return flat[:width] + (" …" if len(flat) > width else "")
    a = max(0, pos - width // 3)
    b = min(len(flat), pos + width)
    snip = ("… " if a > 0 else "") + flat[a:b] + (" …" if b < len(flat) else "")
    for t in tokens:  # first occurrence only; no global pass (would hit tokens inside markers)
        snip = snip.replace(t, f">>{t}<<", 1)
    return re.sub(r"\s+", " ", snip)


def do_show(conn, prefix, maxchars=400):
    cid = resolve_convo(conn, prefix)
    meta = conn.execute("SELECT agent_name, model, created, ua_chars, turns FROM convos WHERE id=?", (cid,)).fetchone()
    print(f"convo {cid}  agent={meta[0]}  model={meta[1]}  created={time.strftime('%Y-%m-%d', time.localtime(meta[2]))}  "
          f"ua={meta[3]}ch turns={meta[4]}")
    rows = conn.execute("SELECT ts, role, text FROM messages WHERE convo_id=? ORDER BY ts", (cid,)).fetchall()
    print(f"--- {len(rows)} messages ---")
    for ts, role, text in rows:
        t = text.replace("\n", " ")
        if len(t) > maxchars:
            t = t[:maxchars] + f" …[{len(text)}ch]"
        print(f"[{time.strftime('%m-%d %H:%M', time.localtime(ts or 0))}] {role:<9} {t}")


def do_raw(conn, prefix, pattern):
    # The authoritative resolver scans the fleet, even with an agent selector.
    # An indexed path alone cannot establish current ownership or precedence.
    raise StoreError('Raw lookup unavailable: current ownership requires a fleet catalog scan. '
                     'Refresh does not enable raw access; --show reads an indexed snapshot only.')


def do_stats(conn):
    c = conn.execute("SELECT COUNT(*) FROM convos").fetchone()[0]
    m = conn.execute("SELECT COUNT(*), SUM(LENGTH(text)) FROM messages").fetchone()
    by_role = conn.execute("SELECT role, COUNT(*), SUM(LENGTH(text)) FROM messages GROUP BY role ORDER BY 3 DESC").fetchall()
    size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    print(f"convos: {c}   message rows: {m[0]}   indexed chars: {(m[1] or 0) / 1e6:.1f}M   db: {size / 1e6:.1f} MB")
    for role, n, ch in by_role:
        print(f"  {role:<9} {n:>6} rows   {ch / 1e6:6.2f}M chars")


def main():
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    class Parser(argparse.ArgumentParser):
        def print_help(self, file=None):
            print(FRESHNESS_UNKNOWN, flush=True)
            super().print_help(file)

        def error(self, message):
            print(FRESHNESS_UNKNOWN, flush=True)
            super().error(message)

    ap = Parser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="?", help="search terms (implicit AND)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--messages", action="store_true",
                    help="flat per-message view (default output is one row per convo, ranked by its best match)")
    ap.add_argument("--agent", help="filter by agent_name")
    ap.add_argument("--since", type=float, help="only convos created within N days (epoch seconds also accepted if > 1e9)")
    ap.add_argument("--show", metavar="CONVO_ID", help="dump one convo's indexed messages")
    ap.add_argument("--raw", metavar="CONVO_ID", help="currently refuses: current ownership needs a fleet scan; refresh does not enable raw")
    ap.add_argument('--refresh', action='store_true', help='launch a detached, hidden, logged refresh and return immediately')
    ap.add_argument('--refresh-worker', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--build-index', action='store_true', help=argparse.SUPPRESS)
    ap.add_argument('--reindex', action='store_true', help='launch detached full rebuild (not a lookup)')
    ap.add_argument('--stats', action='store_true')
    ap.add_argument('--index', action='store_true', help='legacy alias for detached --refresh; cannot combine with lookup')
    ap.add_argument('--root', type=Path, help='configured durable data root (owned plus read-only archives)')
    args = ap.parse_args()
    global _DATA_ROOT, ROOT, DB_PATH
    if args.root is not None:
        _DATA_ROOT = args.root.resolve()
        ROOT = str(_DATA_ROOT / '.convos')
        DB_PATH = str(_DATA_ROOT / 'tools' / 'convo_search.db')
    refresh = any((args.refresh, args.refresh_worker, args.build_index, args.index, args.reindex))
    lookup = any((args.query, args.show, args.raw, args.stats))
    if refresh and lookup:
        ap.error('refresh/index actions cannot be combined with lookup')
    if args.raw and not args.query:
        ap.error('--raw requires the search pattern as the query argument')
    if not (refresh or lookup):
        ap.print_help()
        return 0
    if args.refresh_worker:
        return refresh_worker(force=args.reindex)
    conn = open_snapshot(refusing=not (refresh or args.raw))
    if refresh:
        if conn is not None:
            conn.close()
        try:
            if args.build_index:
                build_index(force=args.reindex)
            else:
                start_refresh(force=args.reindex)
            return 0
        except (OSError, sqlite3.Error, StoreError) as exc:
            print(f'Refresh failed: {exc}')
            return 1
    if conn is None and not args.raw:
        return 1
    try:
        if args.raw:
            do_raw(conn, args.raw, args.query)
        else:
            print('Indexed snapshot; source ownership validated at refresh, not revalidated during lookup.')
            if args.stats:
                do_stats(conn)
            elif args.show:
                do_show(conn, args.show)
            else:
                if args.since and args.since > 1e9:
                    args.since = time.time() - args.since
                do_search(conn, args.query, args.limit, args.agent, args.since, per_convo=not args.messages)
        return 0
    except (sqlite3.Error, StoreError) as exc:
        print(f'Refused: {exc}')
        if isinstance(exc, sqlite3.Error):
            print('Refresh explicitly: ' + refresh_command())
        return 1
    finally:
        if conn is not None:
            conn.close()


if __name__ == '__main__':
    sys.exit(main())
