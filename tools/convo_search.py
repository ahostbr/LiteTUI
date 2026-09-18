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
  python convo_search.py --raw <convo-id-or-prefix> "regex"
  python convo_search.py --reindex                full rebuild
  python convo_search.py --stats
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

ROOT = r"C:\Projects\LiteTUI\.convos"
DB_PATH = r"C:\Projects\LiteTUI\tools\convo_search.db"
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
    convo_id = path.name
    size = path.stat().st_size
    row = conn.execute("SELECT file_size, byte_offset FROM convos WHERE id=?", (convo_id,)).fetchone()
    stored_size = row[0] if row else -1
    stored_offset = row[1] if row else 0

    if not force and row and size == stored_size:
        return 0  # up to date

    full = force or size < stored_offset or row is None
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
                    (obj.get("id") or convo_id, obj.get("agent_name"),
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
        """INSERT INTO convos(id, file_size, byte_offset) VALUES(?,?,?)
           ON CONFLICT(id) DO UPDATE SET file_size=excluded.file_size,
             byte_offset=excluded.byte_offset""",
        (convo_id, size, offset),
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
    conn.commit()
    return new_count


def build_index(force=False):
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    convos = sorted(os.listdir(ROOT))
    t0 = time.time()
    total = 0
    for name in convos:
        p = os.path.join(ROOT, name)
        cj = os.path.join(p, "convo.jsonl")
        if os.path.isdir(p) and os.path.exists(cj):
            total += index_convo(conn, _Path(cj))
    print(f"indexed {len(convos)} convos, {total} new message rows in {time.time() - t0:.1f}s -> {DB_PATH}")
    conn.close()


class _Path:
    def __init__(self, path):
        self.full = path
        self.name = os.path.basename(os.path.dirname(path))

    def __fspath__(self):
        return self.full

    def stat(self):
        return os.stat(self.full)


def open_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
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
    cid = resolve_convo(conn, prefix)
    path = os.path.join(ROOT, cid, "convo.jsonl")
    rx = re.compile(pattern)
    n = 0
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for i, line in enumerate(f, 1):
            if rx.search(line):
                n += 1
                m = rx.search(line)
                a, b = max(0, m.start() - 120), min(len(line), m.end() + 120)
                print(f"L{i}: …{line[a:b].strip()}…")
                if n >= 40:
                    print("…(stopped at 40 matches)")
                    break
    print(f"{n} matching lines")


def do_stats(conn):
    c = conn.execute("SELECT COUNT(*) FROM convos").fetchone()[0]
    m = conn.execute("SELECT COUNT(*), SUM(LENGTH(text)) FROM messages").fetchone()
    by_role = conn.execute("SELECT role, COUNT(*), SUM(LENGTH(text)) FROM messages GROUP BY role ORDER BY 3 DESC").fetchall()
    size = os.path.getsize(DB_PATH) if os.path.exists(DB_PATH) else 0
    print(f"convos: {c}   message rows: {m[0]}   indexed chars: {m[1] / 1e6:.1f}M   db: {size / 1e6:.1f} MB")
    for role, n, ch in by_role:
        print(f"  {role:<9} {n:>6} rows   {ch / 1e6:6.2f}M chars")


def main():
    if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("query", nargs="?", help="search terms (implicit AND)")
    ap.add_argument("--limit", type=int, default=10)
    ap.add_argument("--messages", action="store_true",
                    help="flat per-message view (default output is one row per convo, ranked by its best match)")
    ap.add_argument("--agent", help="filter by agent_name")
    ap.add_argument("--since", type=float, help="only convos created within N days (epoch seconds also accepted if > 1e9)")
    ap.add_argument("--show", metavar="CONVO_ID", help="dump one convo's indexed messages")
    ap.add_argument("--raw", metavar="CONVO_ID", help="grep the raw convo.jsonl (requires pattern as query)")
    ap.add_argument("--reindex", action="store_true", help="force full rebuild")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--index", action="store_true", help="incremental index pass, then exit (or before a search)")
    args = ap.parse_args()

    if args.reindex:
        build_index(force=True)
        return
    if not os.path.exists(DB_PATH):
        print("no index yet — building…")
        build_index()

    if args.index:
        build_index()
        if not args.query:
            return
    conn = open_db()
    try:
        if args.stats:
            do_stats(conn)
            return
        if args.show:
            do_show(conn, args.show)
            return
        if args.raw:
            if not args.query:
                ap.error("--raw requires the search pattern as the query argument")
            do_raw(conn, args.raw, args.query)
            return
        if not args.query:
            ap.print_help()
            return
        if args.since and args.since > 1e9:
            args.since = time.time() - args.since  # epoch given
        do_search(conn, args.query, args.limit, args.agent, args.since, per_convo=not args.messages)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
