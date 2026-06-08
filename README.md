# 🦙 altpaca

[![ci](https://github.com/excitoon/altpaca/actions/workflows/ci.yml/badge.svg)](https://github.com/excitoon/altpaca/actions/workflows/ci.yml)

Move Claude Desktop sessions between accounts. `altpaca` copies, verifies,
then optionally removes — so your chat history follows you to your **alt** account
instead of vanishing when you switch logins.

## The problem

The Claude desktop app keys its session-history list **by account**. Log into a
different account and the app only lists *that* account's sessions — your other
account's history disappears from the sidebar (it is **not** deleted, just hidden).
There is no built-in "move this conversation to my other account."

## How it actually works (the part that makes a correct move possible)

```
~/Library/Application Support/Claude/
  claude-code-sessions/<ACCOUNT>/<WORKSPACE>/local_<uuid>.json   <- session metadata
~/.claude/projects/<encoded-cwd>/<cliSessionId>.jsonl           <- the real transcript
```

- A session's **account is its folder location only**. The metadata JSON stores
  `sessionId`, `cliSessionId`, `cwd`, `title`, … but **not** the account/workspace
  id. So a correct move is just *relocating the JSON file* — no field rewriting.
- The **transcript** lives separately, keyed by `cwd` + `cliSessionId`, and is
  account-agnostic. It stays put on a move and the destination account resolves it
  by id — no ghosts.
- The app reads its list from these files (no IndexedDB index by id), so a moved
  session shows up **after an app restart**.

> ⚠️ Quit the Claude desktop app before moving. It can flush in-memory state on
> exit and clobber your changes. `altpaca` refuses to apply while Claude is running
> (override with `--force`).

## Install

No dependencies — pure Python 3 stdlib.

```bash
# run directly
python3 altpaca.py accounts

# or install the `altpaca` command
pipx install .        # or: pip install --user .
```

## Usage

```bash
# see your account partitions and where your sessions actually are
altpaca accounts

# list sessions in an account (uuid prefix is fine)
altpaca list aaaaaaaa

# move them — DRY RUN by default, nothing changes until --apply
altpaca move aaaaaaaa bbbbbbbb --all
altpaca move aaaaaaaa bbbbbbbb --all --apply        # do it (asks to confirm)
altpaca move aaaaaaaa bbbbbbbb --all --apply --yes  # no prompt

# selective
altpaca move aaaaaaaa bbbbbbbb --project my-project
altpaca move aaaaaaaa bbbbbbbb --title "meeting notes"
altpaca move aaaaaaaa bbbbbbbb --session 11111111 22222222

# copy instead of move (keep the originals in the source account)
altpaca copy aaaaaaaa bbbbbbbb --all

# archive a WHOLE account into ONE .zip (one per run — never overwrites)
altpaca dump aaaaaaaa                              # -> ~/.altpaca/dumps/altpaca-dump_*.zip
altpaca dump aaaaaaaa --out ~/backups/             # archive into another dir
altpaca dump aaaaaaaa -n                           # preview the archive contents

# groups — read from the app (Work, Home, Travel, …)
altpaca groups                                      # list the app's groups + members
altpaca list --group Home                       # filter (group shown as a column)
altpaca move aaaaaaaa bbbbbbbb --group Travel       # move a whole group at once

# undo the last operation
altpaca restore 20260608-141230 --apply

# environment / sanity check
altpaca doctor
```

## Safety

- **Dry-run by default.** `move`/`copy` only print a plan unless you pass `--apply`.
- **Backups.** Every applied operation snapshots the affected files to
  `~/.altpaca/backups/<timestamp>/` with a manifest; `altpaca restore <id>` reverts it.
- **Verify before delete.** A move copies, sha256-checks the copy, *then* removes the
  source.
- **Won't fight the app.** Refuses to apply while Claude is running.
- **No transcript, no move.** Sessions whose transcript is missing are skipped
  (override with `--force`).
- Only ever touches `local_*.json` metadata files; never your transcripts.

## Caveats

This pokes at the desktop app's private on-disk layout, which Anthropic can change
at any time. It is an unofficial tool — keep the backups. Set `ALTPACA_CLAUDE_DIR`
to point at a non-default app-support location.

## Groups

The desktop app's sidebar groups (Work, Home, Travel, …) are stored in its
**Local Storage** (a Chromium leveldb) under the `dframe-store` key — a group list plus
a `session → group` map. altpaca reads them **read-only** via a small built-in
leveldb/Snappy parser (no external deps); it never writes them back.

- `altpaca groups` lists them with their members.
- `--group NAME` selects a group in `list`/`move`/`copy`, case-insensitive.

Moving a session between accounts is keyed by session id, so its group membership is
unaffected. For the freshest read, quit the app first (recent group edits can sit in the
leveldb write-ahead log until it flushes). "Projects" are just the session `cwd` —
select with `--project`, no setup needed.

## Environment

- `ALTPACA_CLAUDE_DIR` — Claude app-support dir (default `~/Library/Application Support/Claude`).
- `CLAUDE_CONFIG_DIR` — if set, transcripts are read from `$CLAUDE_CONFIG_DIR/projects` (matches Claude Code).
- `ALTPACA_PROJECTS_DIR` — override the transcripts dir directly (wins over the above).
- `ALTPACA_BACKUP_DIR` — where backups are written (default `~/.altpaca/backups`).

## License

MIT
