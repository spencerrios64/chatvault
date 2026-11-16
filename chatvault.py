#!/usr/bin/env python3
"""chatvault - local-first, incremental archive of your AI chats.

Imports Claude and ChatGPT data exports into one SQLite file, merges
re-imports incrementally (only new/changed conversations are touched),
supports full-text search, and exports JSONL for fine-tuning.
Standard library only (needs SQLite with FTS5, which Python ships with).
"""
import argparse
import json
import sqlite3
import sys
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DEFAULT_DB = Path.home() / ".chatvault" / "vault.db"

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
    role TEXT NOT NULL,           -- user | assistant | system | tool
    text TEXT NOT NULL,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conv_id, idx);
CREATE VIRTUAL TABLE IF NOT EXISTS msg_fts USING fts5(
    text, content='messages', content_rowid='rowid', tokenize='porter unicode61'
);
CREATE TRIGGER IF NOT EXISTS msg_ai AFTER INSERT ON messages BEGIN
    INSERT INTO msg_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS msg_ad AFTER DELETE ON messages BEGIN
    INSERT INTO msg_fts(msg_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
"""


def connect(db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
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
        # Walk from current_node back to the root to get the visible branch.
        chain, node = [], conv.get("current_node")
        while node:
            entry = mapping.get(node)
            if not entry:
                break
            chain.append(entry)
            node = entry.get("parent")
        chain.reverse()
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
    """Ingest-endpoint format: {source, id, title, updated_at, messages:[{role,text}]}"""
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
                if m.get("text", "").strip()
            ],
        }


def detect_and_parse(data):
    if isinstance(data, list) and data:
        first = data[0]
        if "mapping" in first:
            return parse_chatgpt(data)
        if "chat_messages" in first:
            return parse_claude(data)
    if isinstance(data, (list, dict)):
        return parse_generic(data)
    raise ValueError("Unrecognised export format")


# ---------------------------------------------------------------- storage
def upsert(con, conv):
    """Insert or refresh one conversation. Returns 'new' | 'updated' | 'same'."""
    cid = f"{conv['source']}:{conv['native_id']}"
    row = con.execute(
        "SELECT updated_at, msg_count FROM conversations WHERE id=?", (cid,)
    ).fetchone()
    n = len(conv["messages"])
    if row and row[0] == conv["updated_at"] and row[1] == n:
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
        counts[upsert(con, conv)] += 1
    con.commit()
    return counts


def load_export(path):
    path = Path(path)
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            name = next((n for n in z.namelist() if n.endswith("conversations.json")), None)
            if not name:
                raise ValueError("No conversations.json inside the zip")
            return json.loads(z.read(name))
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------- commands
def cmd_import(args):
    con = connect(args.db)
    total = {"new": 0, "updated": 0, "same": 0}
    for f in args.files:
        counts = ingest(con, detect_and_parse(load_export(f)))
        print(f"{f}: {counts['new']} new, {counts['updated']} updated, {counts['same']} unchanged")
        for k in total:
            total[k] += counts[k]
    return 0


def cmd_search(args):
    con = connect(args.db)
    sql = """
        SELECT c.source, c.title, m.role, c.id,
               snippet(msg_fts, 0, '[', ']', ' … ', 12)
        FROM msg_fts JOIN messages m ON m.rowid = msg_fts.rowid
        JOIN conversations c ON c.id = m.conv_id
        WHERE msg_fts MATCH ?"""
    params = [args.query]
    if args.source:
        sql += " AND c.source = ?"
        params.append(args.source)
    sql += " ORDER BY rank LIMIT ?"
    params.append(args.limit)
    rows = con.execute(sql, params).fetchall()
    for source, title, role, cid, snip in rows:
        print(f"[{source}] {title or '(untitled)'}  ({role})  {cid}\n    {snip}\n")
    if not rows:
        print("No matches.")
    return 0


def cmd_stats(args):
    con = connect(args.db)
    for source, n, msgs in con.execute(
        "SELECT source, COUNT(*), SUM(msg_count) FROM conversations GROUP BY source"
    ):
        print(f"{source}: {n} conversations, {msgs or 0} messages")
    last = con.execute("SELECT MAX(synced_at) FROM conversations").fetchone()[0]
    print(f"last sync: {last or 'never'}")
    return 0


def cmd_show(args):
    con = connect(args.db)
    for role, text in con.execute(
        "SELECT role, text FROM messages WHERE conv_id=? ORDER BY idx", (args.conv_id,)
    ):
        print(f"--- {role} ---\n{text}\n")
    return 0


def cmd_export(args):
    """JSONL for fine-tuning. --as-user exports only YOUR messages (style data);
    default is chat-format records ({"messages":[...]}) per conversation."""
    con = connect(args.db)
    q = "SELECT id FROM conversations"
    params = []
    if args.source:
        q += " WHERE source=?"
        params.append(args.source)
    out = open(args.out, "w", encoding="utf-8") if args.out else sys.stdout
    n = 0
    for (cid,) in con.execute(q, params).fetchall():
        msgs = [
            {"role": r, "content": t}
            for r, t in con.execute(
                "SELECT role, text FROM messages WHERE conv_id=? ORDER BY idx", (cid,)
            )
        ]
        if args.as_user:
            msgs = [m for m in msgs if m["role"] == "user"]
            for m in msgs:
                out.write(json.dumps({"text": m["content"]}, ensure_ascii=False) + "\n")
                n += 1
        elif msgs:
            out.write(json.dumps({"messages": msgs}, ensure_ascii=False) + "\n")
            n += 1
    if args.out:
        out.close()
        print(f"wrote {n} records to {args.out}")
    return 0


def cmd_serve(args):
    """Local ingest endpoint so a browser extension / userscript can push chats live.
    POST /ingest with a JSON conversation (or list). Bound to 127.0.0.1 only and
    protected by a shared token (--token)."""
    db = args.db

    class H(BaseHTTPRequestHandler):
        def _cors(self):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Vault-Token")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")

        def do_OPTIONS(self):
            self.send_response(204)
            self._cors()
            self.end_headers()

        def do_POST(self):
            if self.path != "/ingest" or self.headers.get("X-Vault-Token") != args.token:
                self.send_response(403)
                self._cors()
                self.end_headers()
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                data = json.loads(self.rfile.read(length))
                con = connect(db)
                counts = ingest(con, detect_and_parse(data))
                con.close()
                body, code = json.dumps(counts).encode(), 200
            except Exception as e:  # report bad payloads, keep serving
                body, code = json.dumps({"error": str(e)}).encode(), 400
            self.send_response(code)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    print(f"listening on http://127.0.0.1:{args.port}/ingest  (header X-Vault-Token required)")
    HTTPServer(("127.0.0.1", args.port), H).serve_forever()
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="chatvault", description=__doc__.split("\n\n")[0])
    p.add_argument("--db", default=str(DEFAULT_DB), help=f"database path (default {DEFAULT_DB})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("import", help="import Claude/ChatGPT export (.json or .zip); safe to re-run")
    s.add_argument("files", nargs="+")
    s.set_defaults(fn=cmd_import)

    s = sub.add_parser("search", help="full-text search (FTS5 syntax)")
    s.add_argument("query")
    s.add_argument("--source", choices=["claude", "chatgpt", "other"])
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_search)

    s = sub.add_parser("stats", help="archive summary")
    s.set_defaults(fn=cmd_stats)

    s = sub.add_parser("show", help="print one conversation")
    s.add_argument("conv_id")
    s.set_defaults(fn=cmd_show)

    s = sub.add_parser("export", help="export JSONL for fine-tuning")
    s.add_argument("--source", choices=["claude", "chatgpt", "other"])
    s.add_argument("--as-user", action="store_true", help="only your own messages")
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
