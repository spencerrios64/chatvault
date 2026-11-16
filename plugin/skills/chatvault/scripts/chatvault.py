#!/usr/bin/env python3
"""chatvault - local-first, incremental archive of your AI chats.

Imports Claude and ChatGPT data exports into one SQLite file, merges
re-imports incrementally (only new/changed conversations are touched),
supports full-text search, and exports JSONL for fine-tuning.
Standard library only. Runs on Windows, macOS, Linux and Android (Termux).
"""
import argparse
import hmac
import json
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

__version__ = "0.2.0"
DEFAULT_DB = Path.home() / ".chatvault" / "vault.db"
MAX_BODY = 50 * 1024 * 1024  # largest ingest request accepted by `serve`
ALLOWED_ORIGINS = {"https://claude.ai", "https://chatgpt.com", "https://chat.openai.com"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,          -- "<source>:<native id>"
    source TEXT NOT NULL,         -- claude | chatgpt | other
    title TEXT,
    created_at TEXT,
    updated_at TEXT,
    msg_count INTEGER DEFAULT 0,
    synced_at TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,          -- "<conv id>#<index>"
    conv_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    role TEXT NOT NULL,           -- user | assistant
    text TEXT NOT NULL,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, idx);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE msg_fts USING fts5(
    text, content='messages', content_rowid='rowid', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS msg_ai AFTER INSERT ON messages BEGIN
    INSERT INTO msg_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS msg_ad AFTER DELETE ON messages BEGIN
    INSERT INTO msg_fts(msg_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
"""


def has_fts(con):
    return con.execute("SELECT 1 FROM sqlite_master WHERE name='msg_fts'").fetchone() is not None


def connect(db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    if not has_fts(con):
        try:
            con.executescript(FTS_SCHEMA)
            con.execute("INSERT INTO msg_fts(msg_fts) VALUES('rebuild')")
            con.commit()
        except sqlite3.OperationalError:
            pass  # SQLite built without FTS5: search falls back to LIKE
    return con


def iso(value):
    """Normalise unix seconds / ISO strings to ISO-8601 UTC text (or None)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()
    return str(value)


# ---------------------------------------------------------------- parsers
def parse_chatgpt(data):
    for conv in data:
        mapping = conv.get("mapping") or {}
        node = conv.get("current_node")
        if node:
            # Walk from current_node back to the root: the visible branch.
            chain = []
            while node and mapping.get(node):
                chain.append(mapping[node])
                node = mapping[node].get("parent")
            chain.reverse()
        else:
            chain = sorted(
                mapping.values(),
                key=lambda e: ((e.get("message") or {}).get("create_time") or 0),
            )
        msgs = []
        for entry in chain:
            m = entry.get("message")
            if not m:
                continue
            role = (m.get("author") or {}).get("role", "")
            if role not in ("user", "assistant"):
                continue
            parts = (m.get("content") or {}).get("parts") or []
            text = "\n".join(p for p in parts if isinstance(p, str)).strip()
            if text:
                msgs.append((role, text, iso(m.get("create_time"))))
        yield {
            "source": "chatgpt",
            "native_id": conv.get("id") or conv.get("conversation_id"),
            "title": conv.get("title"),
            "created_at": iso(conv.get("create_time")),
            "updated_at": iso(conv.get("update_time")),
            "messages": msgs,
        }


def parse_claude(data):
    for conv in data:
        msgs = []
        for m in conv.get("chat_messages", []):
            role = "user" if m.get("sender") == "human" else "assistant"
            text = (m.get("text") or "").strip()
            if not text:
                text = "\n".join(
                    b.get("text", "")
                    for b in m.get("content", [])
                    if b.get("type") == "text"
                ).strip()
            if text:
                msgs.append((role, text, iso(m.get("created_at"))))
        yield {
            "source": "claude",
            "native_id": conv.get("uuid"),
            "title": conv.get("name"),
            "created_at": iso(conv.get("created_at")),
            "updated_at": iso(conv.get("updated_at")),
            "messages": msgs,
        }


def parse_generic(data):
    """chatvault's own format (ingest endpoint, `export --format archive`, web app):
    {source, id, title, updated_at, messages:[{role, text}]} or a list of those."""
    items = data if isinstance(data, list) else [data]
    for c in items:
        yield {
            "source": c.get("source", "other"),
            "native_id": str(c["id"]),
            "title": c.get("title"),
            "created_at": iso(c.get("created_at")),
            "updated_at": iso(c.get("updated_at")),
            "messages": [
                (m["role"], m["text"], iso(m.get("created_at")))
                for m in c.get("messages", [])
                if (m.get("text") or "").strip()
            ],
        }


def detect_and_parse(data):
    if isinstance(data, list) and data and isinstance(data[0], dict):
        if "mapping" in data[0]:
            return parse_chatgpt(data)
        if "chat_messages" in data[0]:
            return parse_claude(data)
    if isinstance(data, (list, dict)):
        return parse_generic(data)
    raise ValueError("Unrecognised export format")


# ---------------------------------------------------------------- storage
def upsert(con, conv):
    """Insert or refresh one conversation. Returns 'new' | 'updated' | 'same'.
    An older copy never overwrites a newer one already in the vault."""
    cid = f"{conv['source']}:{conv['native_id']}"
    row = con.execute(
        "SELECT updated_at, msg_count FROM conversations WHERE id=?", (cid,)
    ).fetchone()
    n = len(conv["messages"])
    if row:
        if row[0] == conv["updated_at"] and row[1] == n:
            return "same"
        if row[0] and conv["updated_at"] and conv["updated_at"] < row[0]:
            return "same"
    status = "updated" if row else "new"
    con.execute("DELETE FROM messages WHERE conv_id=?", (cid,))
    con.execute(
        """INSERT INTO conversations(id, source, title, created_at, updated_at, msg_count, synced_at)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET title=excluded.title, created_at=excluded.created_at,
             updated_at=excluded.updated_at, msg_count=excluded.msg_count, synced_at=excluded.synced_at""",
        (cid, conv["source"], conv["title"], conv["created_at"], conv["updated_at"], n,
         datetime.now(timezone.utc).isoformat()),
    )
    con.executemany(
        "INSERT INTO messages(id, conv_id, idx, role, text, created_at) VALUES(?,?,?,?,?,?)",
        [(f"{cid}#{i}", cid, i, r, t, ts) for i, (r, t, ts) in enumerate(conv["messages"])],
    )
    return status


def ingest(con, convs):
    counts = {"new": 0, "updated": 0, "same": 0}
    for conv in convs:
        if not conv["native_id"]:
            continue
        counts[upsert(con, conv)] += 1
    con.commit()
    return counts


def load_export(path):
    """Return a list of parsed JSON documents from a .json file or an export .zip."""
    path = Path(path)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            names = sorted(
                n for n in z.namelist()
                if Path(n).name.startswith("conversations") and n.endswith(".json")
            )
            if not names:
                raise ValueError("no conversations.json inside the zip")
            return [json.loads(z.read(n)) for n in names]
    return [json.loads(path.read_text(encoding="utf-8"))]


def search(con, query, source=None, limit=20, force_like=False):
    """Return [(source, title, role, conv_id, snippet)]. Uses FTS5 when available;
    a query FTS5 can't parse is retried as plain words, then as a LIKE scan."""
    terms = query.split()
    if not terms:
        return []
    src_sql = " AND c.source = ?" if source else ""
    src_arg = [source] if source else []
    if has_fts(con) and not force_like:
        sql = f"""
            SELECT c.source, c.title, m.role, c.id,
                   snippet(msg_fts, 0, '[', ']', ' … ', 12)
            FROM msg_fts JOIN messages m ON m.rowid = msg_fts.rowid
            JOIN conversations c ON c.id = m.conv_id
            WHERE msg_fts MATCH ?{src_sql} ORDER BY rank LIMIT ?"""
        quoted = " ".join('"' + t.replace('"', '""') + '"' for t in terms)
        for q in (query, quoted):
            try:
                return con.execute(sql, [q] + src_arg + [limit]).fetchall()
            except sqlite3.OperationalError:
                continue
    esc = lambda t: t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    where = " AND ".join("m.text LIKE ? ESCAPE '\\'" for _ in terms)
    sql = f"""
        SELECT c.source, c.title, m.role, c.id, m.text
        FROM messages m JOIN conversations c ON c.id = m.conv_id
        WHERE {where}{src_sql} ORDER BY c.updated_at DESC LIMIT ?"""
    rows = con.execute(sql, [f"%{esc(t)}%" for t in terms] + src_arg + [limit]).fetchall()
    out = []
    for src, title, role, cid, text in rows:
        i = max(text.lower().find(terms[0].lower()), 0)
        out.append((src, title, role, cid, text[max(i - 60, 0):i + 100].replace("\n", " ")))
    return out


def export_records(con, fmt="chat", source=None, since=None, min_chars=0):
    """Yield export records. fmt: chat | user | archive."""
    q, params = "SELECT id, source, title, created_at, updated_at FROM conversations WHERE 1=1", []
    if source:
        q += " AND source=?"
        params.append(source)
    if since:
        q += " AND updated_at >= ?"
        params.append(since)
    for cid, src, title, created, updated in con.execute(q + " ORDER BY updated_at", params).fetchall():
        msgs = con.execute(
            "SELECT role, text, created_at FROM messages WHERE conv_id=? ORDER BY idx", (cid,)
        ).fetchall()
        if fmt == "user":
            for role, text, _ in msgs:
                if role == "user" and len(text) >= min_chars:
                    yield {"text": text}
        elif fmt == "archive":
            yield {
                "source": src, "id": cid.split(":", 1)[1], "title": title,
                "created_at": created, "updated_at": updated,
                "messages": [{"role": r, "text": t, "created_at": ts} for r, t, ts in msgs],
            }
        elif msgs:
            yield {"messages": [{"role": r, "content": t} for r, t, _ in msgs]}


# ---------------------------------------------------------------- commands
def cmd_import(args):
    con = connect(args.db)
    failed = False
    for f in args.files:
        try:
            counts = {"new": 0, "updated": 0, "same": 0}
            for doc in load_export(f):
                for k, v in ingest(con, detect_and_parse(doc)).items():
                    counts[k] += v
            print(f"{f}: {counts['new']} new, {counts['updated']} updated, {counts['same']} unchanged")
        except (OSError, ValueError, KeyError, TypeError) as e:
            failed = True
            print(f"{f}: could not import ({e})", file=sys.stderr)
    return 1 if failed else 0


def cmd_search(args):
    rows = search(connect(args.db), args.query, args.source, args.limit)
    for source, title, role, cid, snip in rows:
        print(f"[{source}] {title or '(untitled)'}  ({role})  {cid}\n    {snip}\n")
    if not rows:
        print("No matches.")
    return 0


def cmd_list(args):
    con = connect(args.db)
    q, params = "SELECT updated_at, source, msg_count, title, id FROM conversations", []
    if args.source:
        q += " WHERE source=?"
        params.append(args.source)
    for updated, source, n, title, cid in con.execute(
        q + " ORDER BY updated_at DESC LIMIT ?", params + [args.limit]
    ):
        print(f"{(updated or '')[:10]:10}  [{source}] {title or '(untitled)'}  ({n} msgs)  {cid}")
    return 0


def cmd_stats(args):
    con = connect(args.db)
    for source, n, msgs in con.execute(
        "SELECT source, COUNT(*), SUM(msg_count) FROM conversations GROUP BY source"
    ):
        print(f"{source}: {n} conversations, {msgs or 0} messages")
    last = con.execute("SELECT MAX(synced_at) FROM conversations").fetchone()[0]
    print(f"last sync: {last or 'never'}")
    print(f"search: {'FTS5' if has_fts(con) else 'LIKE fallback (SQLite has no FTS5)'}")
    return 0


def cmd_show(args):
    con = connect(args.db)
    rows = con.execute(
        "SELECT role, text FROM messages WHERE conv_id=? ORDER BY idx", (args.conv_id,)
    ).fetchall()
    for role, text in rows:
        print(f"--- {role} ---\n{text}\n")
    if not rows:
        print(f"No conversation with id {args.conv_id}", file=sys.stderr)
        return 1
    return 0


def cmd_export(args):
    fmt = "user" if args.as_user else args.format
    records = export_records(connect(args.db), fmt, args.source, args.since, args.min_chars)
    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    n = 0
    if fmt == "archive":
        records = list(records)
        n = len(records)
        json.dump(records, out, ensure_ascii=False)
    else:
        for rec in records:
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    if args.out:
        out.close()
        print(f"wrote {n} records to {args.out}")
    return 0


def cmd_serve(args):
    """Local ingest endpoint so a browser extension / userscript can push chats live.
    POST /ingest with a JSON conversation (or list). Bound to 127.0.0.1 only,
    protected by a shared token, and only the chat sites may call it from a browser."""
    db = args.db

    class H(BaseHTTPRequestHandler):
        def _reply(self, code, body=b""):
            self.send_response(code)
            origin = self.headers.get("Origin")
            if origin in ALLOWED_ORIGINS:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Vault-Token")
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Private-Network", "true")
            if body:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self._reply(204)

        def do_POST(self):
            token = self.headers.get("X-Vault-Token") or ""
            if self.path != "/ingest" or not hmac.compare_digest(token, args.token):
                return self._reply(403)
            try:
                length = int(self.headers.get("Content-Length", 0))
                if length > MAX_BODY:
                    return self._reply(413)
                data = json.loads(self.rfile.read(length))
                con = connect(db)
                counts = ingest(con, detect_and_parse(data))
                con.close()
                self._reply(200, json.dumps(counts).encode())
            except Exception as e:  # report bad payloads, keep serving
                self._reply(400, json.dumps({"error": str(e)}).encode())

        def log_message(self, *a):
            pass

    print(f"listening on http://127.0.0.1:{args.port}/ingest  (header X-Vault-Token required)")
    HTTPServer(("127.0.0.1", args.port), H).serve_forever()
    return 0


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):  # Windows consoles: never crash on emoji
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sources = ["claude", "chatgpt", "other"]
    p = argparse.ArgumentParser(prog="chatvault", description=__doc__.split("\n\n")[0])
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--db", default=str(DEFAULT_DB), help=f"database path (default {DEFAULT_DB})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("import", help="import Claude/ChatGPT export (.json or .zip); safe to re-run")
    s.add_argument("files", nargs="+")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("search", help="full-text search")
    s.add_argument("query")
    s.add_argument("--source", choices=sources)
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("list", help="most recently updated conversations")
    s.add_argument("--source", choices=sources)
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_list)

    s = sub.add_parser("stats", help="archive summary")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("show", help="print one conversation")
    s.add_argument("conv_id")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("export", help="export for fine-tuning or for moving the archive")
    s.add_argument("--format", choices=["chat", "user", "archive"], default="chat",
                   help="chat: JSONL of {messages}; user: JSONL of only your messages; "
                        "archive: one JSON file the CLI and the web app can both import")
    s.add_argument("--as-user", action="store_true", help="same as --format user")
    s.add_argument("--source", choices=sources)
    s.add_argument("--since", help="only conversations updated on/after this date (YYYY-MM-DD)")
    s.add_argument("--min-chars", type=int, default=0,
                   help="with --format user: skip messages shorter than this")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_export)

    s = sub.add_parser("serve", help="local ingest endpoint for live capture")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--token", required=True)
    s.set_defaults(fn=cmd_serve)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
