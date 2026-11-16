# chatvault

Local-first, incremental archive of your AI chats (Claude + ChatGPT) in one SQLite file.
Python 3.9+, no dependencies.

## Use

```
python chatvault.py import data-export.zip        # Claude or ChatGPT export (.zip or conversations.json)
python chatvault.py import newer-export.zip       # re-run any time: only new/changed chats are touched
python chatvault.py search "tilemap" --source claude
python chatvault.py show claude:<uuid>
python chatvault.py stats
python chatvault.py export --out train.jsonl      # {"messages":[...]} per chat, for fine-tuning
python chatvault.py export --as-user --out me.jsonl   # only YOUR messages (style data)
```

Default DB: `~/.chatvault/vault.db` (override with `--db`).

## Live capture (for a browser extension / userscript)

```
python chatvault.py serve --token YOURSECRET
```

POST to `http://127.0.0.1:8765/ingest` with header `X-Vault-Token`:

```json
{"source":"claude","id":"abc","title":"My chat","updated_at":"2026-11-16T12:00:00Z",
 "messages":[{"role":"user","text":"hi"},{"role":"assistant","text":"hello"}]}
```

Bound to localhost only; requests without the token get 403. The extension itself
is not written yet.

## Notes
- ChatGPT exports: only the currently visible branch of each chat is kept.
- Re-import replaces a conversation when its `updated_at` or message count changed.
- Exports contain personal data; keep the DB and JSONL files private.
