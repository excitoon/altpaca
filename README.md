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
# see your account partitions, the email signed into each, and where your sessions are
altpaca accounts

# list sessions in an account (uuid prefix is fine)
altpaca list aaaaaaaa

# any read command (accounts/list/groups/doctor) can emit JSON for scripting
altpaca accounts --json
altpaca list aaaaaaaa --json | jq -r '.accounts[].sessions[].title'

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

# drop (delete) sessions from an account — DRY RUN by default, backed up so you can restore
altpaca drop aaaaaaaa --all                          # preview
altpaca drop aaaaaaaa --project my-project --apply   # delete the metadata (transcript kept)
altpaca drop aaaaaaaa --session 11111111 --apply --with-transcript  # also delete the transcript

# archive a WHOLE account into ONE .zip (one per run — never overwrites)
altpaca dump aaaaaaaa                              # -> ~/.altpaca/dumps/altpaca-dump_*.zip
altpaca dump aaaaaaaa --out ~/backups/             # archive into another dir
altpaca dump aaaaaaaa -n                           # preview the archive contents

# groups — read from the app (Work, Home, Travel, …)
altpaca groups                                      # list the app's groups + members
altpaca list --group Home                       # filter (group shown as a column)
altpaca move aaaaaaaa bbbbbbbb --group Travel       # move a whole group at once

# tenants — a sibling "Claude-<suffix>" app-data dir is a second tenant (see below)
altpaca list excitoon/9cc54b3d                      # address a named tenant's account
altpaca move excitoon/9cc54b3d aaaaaaaa --all       # cross-tenant move — group membership carried automatically

# regroup — re-file sidebar group membership across tenants by group NAME (also runs automatically on a move)
altpaca regroup aaaaaaaa excitoon/9cc54b3d          # dry-run: show what would be (re)grouped
altpaca regroup aaaaaaaa excitoon/9cc54b3d --apply  # write it (backs up first; quit the app)

# undo the last operation
altpaca restore 20260608-141230 --apply

# environment / sanity check
altpaca doctor
```

## Safety

- **Dry-run by default.** `move`/`copy`/`drop` only print a plan unless you pass `--apply`.
- **Backups.** Every applied move/copy/drop snapshots the affected files into a single
  `~/.altpaca/backups/altpaca-backup_<timestamp>.zip`; `altpaca restore <name>` reverts it
  (a dropped session — and its transcript, if you deleted that too — comes right back).
- **Verify before delete.** A move copies, sha256-checks the copy, *then* removes the
  source.
- **Won't fight the app.** Refuses to apply while Claude is running.
- **No transcript, no move.** Sessions whose transcript is missing are skipped
  (override with `--force`).
- `move`/`copy`/`drop` only ever touch `local_*.json` metadata files; never your transcripts —
  **unless** you pass `drop --with-transcript`, which also deletes the `.jsonl`, and even then
  only when no *surviving* session still references it (so a copy in another account is never
  orphaned). (The separate `regroup` command also writes the destination's Local Storage — see
  **Regrouping**.)

## Running two accounts at once (instead of moving)

`altpaca` *relocates* history so it follows you after you switch logins. If you'd
rather keep both accounts **open simultaneously**, give each its own app-support
tree with Electron's `--user-data-dir` — the desktop app honours it even when
launched by path:

```bash
# a second, fully isolated account: own login, own session list, own window
/Applications/Claude.app/Contents/MacOS/Claude \
  --user-data-dir="$HOME/Library/Application Support/Claude-alt"
```

Each `--user-data-dir` is an independent profile — separate web login (kept in the
profile, not shared), separate `claude-code-sessions/`, no shared state — and the
two run concurrently.

> ⚠️ One profile dir = one running instance. Pointing a second launch at a dir
> that is already open fails with `LevelDB … LOCK` errors; use a *different*
> `--user-data-dir` per simultaneous account.

`CLAUDE_CONFIG_DIR` is a **different** knob — it relocates the Claude *Code* CLI
config (`~/.claude`), not the desktop app's login or session list. For the desktop
app, `--user-data-dir` is what isolates an account. Point altpaca at one of these
instances with `ALTPACA_CLAUDE_DIR`:

```bash
ALTPACA_CLAUDE_DIR="$HOME/Library/Application Support/Claude-alt" altpaca accounts
```

You usually don't need that override, though — see **Tenants** below: altpaca
auto-discovers sibling `Claude-*` dirs and shows them all in one listing.

### A distinct Dock icon per instance

A Dock tile stores only a *reference* to the app (bundle path + id) and renders its
icon live, so two pins of the same `Claude.app` always look identical. A visually
distinct alt needs a separate **bundle**:

```bash
cp -R /Applications/Claude.app "/Applications/Claude Alt.app"
cd "/Applications/Claude Alt.app/Contents"
/usr/libexec/PlistBuddy -c 'Set :CFBundleName "Claude Alt"' Info.plist
/usr/libexec/PlistBuddy -c 'Set :CFBundleIdentifier com.anthropic.claudefordesktop.alt' Info.plist
mv MacOS/Claude MacOS/Claude.real   # bake the profile into the bundle
printf '#!/bin/bash\nexec "$(dirname "$0")/Claude.real" --user-data-dir="$HOME/Library/Application Support/Claude-alt" "$@"\n' > MacOS/Claude
chmod +x MacOS/Claude
codesign --force --deep --sign - "/Applications/Claude Alt.app"   # editing the bundle breaks signing
```

Set the icon **without** re-signing via a Finder custom-icon overlay (Get Info →
paste the image, or `fileicon set`) — it is stored as an `Icon\r` resource plus the
`kHasCustomIcon` flag in the bundle's `com.apple.FinderInfo` xattr, layered on top
without touching the signed contents. (Replacing `Contents/Resources/electron.icns`
directly works too, but breaks signing — re-sign as above.) The clone won't
auto-update; rebuild it after each Claude release.

## Caveats

This pokes at the desktop app's private on-disk layout, which Anthropic can change
at any time. It is an unofficial tool — keep the backups. Set `ALTPACA_CLAUDE_DIR`
to point at a non-default app-support location.

## Tenants (multiple app-data dirs)

If you run more than one isolated profile (see *Running two accounts at once*), each
lives in its own app-data dir: the bare `Claude` (the **default tenant**) and any
`Claude-<suffix>` siblings under `~/Library/Application Support/`. altpaca
**auto-discovers** them as tenants and lists them together.

- An account is addressed as `<uuid>` in the default tenant, or `<suffix>/<uuid>` in a
  named one (e.g. `excitoon/9cc54b3d`). Both the suffix and the uuid accept a prefix.
- The same account uuid can exist in two tenants; the bare form always means the
  *default* tenant, so refs stay unambiguous.
- Each tenant has its **own** group store, so group ids and even group *names* are
  tenant-local.

`altpaca accounts`, `list`, `groups`, and `doctor` span all tenants. The default tenant is
always the bare `Claude` dir and named tenants are always its `Claude-*` siblings —
`ALTPACA_CLAUDE_DIR` relocates the whole install (for tests or a non-standard location) but
never promotes a sibling to be the default.

> A **cross-tenant** `move`/`copy` relocates the session file *and* carries sidebar group
> membership over — re-filed by group **name** in the destination (see **Regrouping** below).

## Groups

The desktop app's sidebar groups (Work, Home, Travel, …) are stored in its
**Local Storage** (a Chromium leveldb) under the `dframe-store` key — a group list plus
a `session → group` map. altpaca reads them via a small built-in leveldb/Snappy parser (no
external deps). Each tenant has its own store, so altpaca reads groups **per tenant**.

- `altpaca groups` lists them with their members (grouped by tenant).
- `--group NAME` selects a group in `list`/`move`/`copy`, case-insensitive.

Moving a session **within a tenant** is keyed by session id, so its group membership is
unaffected. Moving **across tenants** would otherwise drop group membership (the assignment and
group ids live in the source tenant's store), so altpaca re-files it automatically — see
**Regrouping**. For the freshest read, quit the app first (recent group edits can sit in the
leveldb write-ahead log until it flushes). "Projects" are just the session `cwd` — select with
`--project`, no setup needed.

## Regrouping (carrying groups across tenants)

A cross-tenant move relocates only the session file, so on its own the session would land
ungrouped in the destination. `regroup` puts it back in the right group — matched by group
**name** — and it runs **automatically** as the tail of a cross-tenant `move`/`copy`. You can
also run it standalone, e.g. to fix sessions moved earlier:

```bash
altpaca regroup <src> <dst>            # dry-run: show what would be (re)grouped
altpaca regroup <src> <dst> --apply    # write it (asks to confirm; backs up first)
altpaca move <src> <dst> --all --apply # move + auto-regroup
```

How it recovers the grouping, even *after* a move: a move removes the session **file** from
the source tenant but leaves its `customGroupAssignments` row intact, so altpaca reads each
session's original group **name** from the source tenant's store, finds (or creates) the
same-named group in the destination tenant, and assigns the session to it. Sessions already
grouped in the destination are left alone unless you pass `--force`. When regroup runs as part
of a move it is *best-effort*: if it can't write (no destination group store, app reopened,
unexpected schema), it warns and leaves the sessions ungrouped rather than failing the move.

Unlike everything else altpaca does, `regroup` **writes** the destination's Local Storage. It
does so by *appending* a single record to that store's leveldb write-ahead log (it never
rewrites existing bytes), with framing verified against the leveldb on-disk format. Because the
write is strictly additive, the worst case of a malformed write is "the record is ignored / the
sessions stay ungrouped" — not loss of your existing groups. It still:

- **backs up** the touched log file first (undo with `altpaca restore <name>`),
- refuses to run while the Claude app is open (it owns that store; `--force` overrides),
- checks the store's schema `VERSION` and bails if the app's on-disk layout has changed.

Quit the app before regrouping, and **restart it afterward** to see the result.

## Environment

- `ALTPACA_CLAUDE_DIR` — Claude app-support dir (default `~/Library/Application Support/Claude`).
- `CLAUDE_CONFIG_DIR` — if set, transcripts are read from `$CLAUDE_CONFIG_DIR/projects` (matches Claude Code).
- `ALTPACA_PROJECTS_DIR` — override the transcripts dir directly (wins over the above).
- `ALTPACA_BACKUP_DIR` — where backups are written (default `~/.altpaca/backups`).

## License

MIT
