# chatvault

Local-first, incremental archive of your AI chats (Claude + ChatGPT). Import your
data exports, search them, and export JSONL for fine-tuning. Nothing leaves your device.

Two ways to use it:

| | Where it runs | Storage |
|---|---|---|
| **Web app** (`docs/index.html`) | Any modern browser, including Chrome on Android | The browser's own storage on that device |
| **CLI** (`chatvault.py`) | Windows, macOS, Linux, Android (Termux) | One SQLite file |

Both read the same exports and can hand an archive to each other.

## Web app (Android and desktop browsers)

Open the hosted page (GitHub Pages, served from `docs/`), tap **Import export file**
and pick the `.zip` you downloaded from Claude or ChatGPT. Then search, open chats,
or export. In Chrome on Android, use **Add to Home screen** to get an icon; after
the first visit the page also opens offline.

It is a web page, not a Play Store app. Chats stay in that browser's storage, so
clearing site data deletes them; use **Export > Full archive** for a backup.
Very large exports (zip64, over 4 GB) are not supported in the browser; use the CLI.

## CLI

Python 3.9+, no dependencies.

```
python chatvault.py import data-export.zip          # Claude or ChatGPT export (.zip or .json)
python chatvault.py import newer-export.zip         # re-run any time: only new/changed chats are touched
python chatvault.py list                            # most recently updated chats
python chatvault.py search "tilemap" --source claude
python chatvault.py show claude:<uuid>
python chatvault.py stats
python chatvault.py export --out train.jsonl                         # {"messages":[...]} per chat
python chatvault.py export --format user --min-chars 40 --out me.jsonl   # only YOUR messages
python chatvault.py export --format archive --out archive.json      # move to the web app or another machine
```

Default DB: `~/.chatvault/vault.db` (override with `--db`).

### Android (Termux)

```
pkg install python
python chatvault.py import /sdcard/Download/data-export.zip
```

Run `termux-setup-storage` first so Termux can read your Downloads folder.

### Live capture (for a browser extension / userscript)

```
python chatvault.py serve --token YOURSECRET
```

POST to `http://127.0.0.1:8765/ingest` with header `X-Vault-Token`:

```json
{"source":"claude","id":"abc","title":"My chat","updated_at":"2026-11-16T12:00:00Z",
 "messages":[{"role":"user","text":"hi"},{"role":"assistant","text":"hello"}]}
```

Bound to localhost only; requests without the token get 403, and only claude.ai and
chatgpt.com pages may call it from a browser. The extension itself is not written yet.

## Notes

- ChatGPT exports: only the currently visible branch of each chat is kept.
- A re-import replaces a chat when its `updated_at` or message count changed; an older
  export never overwrites a newer copy.
- Search uses SQLite FTS5 and falls back to a plain scan where FTS5 is missing.
- Exports contain personal data; keep the DB, archive and JSONL files private.

## Tests

```
python -m unittest discover tests
```
