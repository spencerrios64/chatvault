# chatvault plugin

Lets Claude keep a searchable archive of your Claude and ChatGPT chat exports.

## What you can ask

- "Import this export" (attach the `.zip` you downloaded from Claude or ChatGPT)
- "Search my old chats for tilemap"
- "Show me that conversation"
- "Export only my own messages as JSONL for fine-tuning"

## How it works

The plugin bundles the chatvault script. Claude runs it to import your export into a
local database file, search it, and write exports. Re-importing a newer export only
adds what changed.

It works from the export files you give it. It cannot log in to your accounts.

Project: https://github.com/spencerrios64/chatvault

## Install

In Claude Code:

```
/plugin install chatvault --marketplace spencerrios64/chatvault
```

In the Claude app, download this folder as a `.plugin` file (a zip of the `plugin` folder) and open it in a chat.
