#!/usr/bin/env python3
# altpaca — move Claude Desktop sessions between account partitions.
#
# On-disk model (verified empirically; subject to change if the app changes):
#
#   $CLAUDE/claude-code-sessions/<ACCOUNT>/<WORKSPACE>/local_<uuid>.json
#       One JSON per session (sessionId, cliSessionId, cwd, title, model, ...).
#       A session's ACCOUNT is its folder location ONLY — the JSON does not embed
#       the account or workspace id, so moving a session == relocating this file.
#
#   ~/.claude/projects/<encoded-cwd>/<cliSessionId>.jsonl
#       The real transcript, keyed by cwd + cliSessionId. Account-agnostic, so it
#       stays put on a move and the destination account resolves it by id.
#
#   The desktop app builds its history list from these files (it does not keep a
#   separate IndexedDB/LevelDB index by session id), so a relocated file appears
#   after an app restart. Quit Claude before moving — it may flush in-memory state
#   on exit and clobber changes.
#
# Pure stdlib. MIT licensed.

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

HOME = Path.home()
DEFAULT_BASE = HOME / "Library" / "Application Support" / "Claude"
SESSIONS_DIRNAME = "claude-code-sessions"


# --------------------------------------------------------------------------- #
# paths / helpers
# --------------------------------------------------------------------------- #
def base_dir() -> Path:
    return Path(os.environ.get("ALTPACA_CLAUDE_DIR", str(DEFAULT_BASE)))


def sessions_root() -> Path:
    return base_dir() / SESSIONS_DIRNAME


def projects_dir() -> Path:
    # transcripts live alongside the active config dir; honor overrides
    env = os.environ.get("ALTPACA_PROJECTS_DIR")
    if env:
        return Path(env)
    cfg = os.environ.get("CLAUDE_CONFIG_DIR")
    if cfg:
        return Path(cfg) / "projects"
    return HOME / ".claude" / "projects"


def backup_root() -> Path:
    return Path(os.environ.get("ALTPACA_BACKUP_DIR", str(HOME / ".altpaca" / "backups")))


def die(msg: str, code: int = 1):
    print(f"altpaca: error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def warn(msg: str):
    print(f"altpaca: warning: {msg}", file=sys.stderr)


def fmt_ts(ms) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return "?"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    h.update(Path(path).read_bytes())
    return h.hexdigest()


def claude_running() -> bool:
    # the desktop app's main process is "Claude"; also match its executable path
    # (case-sensitive, so it won't collide with the lowercase claude-code helper).
    for argv in (["pgrep", "-x", "Claude"], ["pgrep", "-f", "Claude.app/Contents/MacOS/Claude"]):
        try:
            if subprocess.run(argv, capture_output=True).returncode == 0:
                return True
        except Exception:
            pass
    return False


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
class Session:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.workspace = self.path.parent.name
        self.account = self.path.parent.parent.name
        try:
            self.meta = json.loads(self.path.read_text())
        except Exception as e:  # keep going; surface a warning
            self.meta = {}
            warn(f"could not parse {self.path.name}: {e}")

    @property
    def session_id(self) -> str:
        return self.meta.get("sessionId") or self.path.stem

    @property
    def uuid(self) -> str:
        sid = self.session_id
        return sid[len("local_") :] if sid.startswith("local_") else sid

    @property
    def cli_id(self) -> str:
        return self.meta.get("cliSessionId") or ""

    @property
    def cwd(self) -> str:
        return self.meta.get("cwd", "")

    @property
    def title(self) -> str:
        return (self.meta.get("title") or "(untitled)").strip()

    @property
    def archived(self) -> bool:
        return bool(self.meta.get("isArchived"))

    @property
    def created(self) -> int:
        return int(self.meta.get("createdAt") or 0)

    @property
    def last_activity(self) -> int:
        return int(self.meta.get("lastActivityAt") or self.meta.get("lastFocusedAt") or self.created or 0)

    def transcript(self):
        if not self.cli_id:
            return None
        hits = glob.glob(str(projects_dir() / "*" / f"{self.cli_id}.jsonl"))
        return Path(hits[0]) if hits else None

    def matches(self, token: str) -> bool:
        return (
            token == self.uuid
            or token == self.session_id
            or token == self.cli_id
            or self.uuid.startswith(token)
            or self.cli_id.startswith(token)
        )


def all_accounts() -> list:
    root = sessions_root()
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def workspaces_of(account: str) -> list:
    accdir = sessions_root() / account
    if not accdir.exists():
        return []
    return sorted(p.name for p in accdir.iterdir() if p.is_dir())


def discover() -> list:
    root = sessions_root()
    if not root.exists():
        die(f"sessions dir not found: {root}\nis the Claude desktop app installed? set ALTPACA_CLAUDE_DIR to override.")
    out = []
    for acc in sorted(p for p in root.iterdir() if p.is_dir()):
        for ws in sorted(p for p in acc.iterdir() if p.is_dir()):
            for f in sorted(ws.glob("local_*.json")):
                out.append(Session(f))
    return out


def by_account(sessions: list) -> dict:
    d = {}
    for s in sessions:
        d.setdefault(s.account, []).append(s)
    return d


def current_account(sessions: list):
    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if env_sid:
        for s in sessions:
            if s.cli_id == env_sid or s.uuid == env_sid:
                return s.account
    best, acc = -1, None
    for s in sessions:
        if s.last_activity > best:
            best, acc = s.last_activity, s.account
    return acc


def resolve_account(ref: str) -> str:
    accs = all_accounts()
    if not accs:
        die("no account partitions found")
    m = [a for a in accs if a == ref or a.startswith(ref)]
    if not m:
        die(f"no account matches '{ref}'. known: " + ", ".join(a[:8] for a in accs))
    if len(m) > 1:
        die(f"'{ref}' is ambiguous: " + ", ".join(x[:8] for x in m))
    return m[0]


def select(sessions: list, args) -> list:
    """Apply positive selectors + skip-archived. Caller enforces --all/selector presence."""
    out = list(sessions)
    if getattr(args, "session", None):
        out = [s for s in out if any(s.matches(t) for t in args.session)]
    if getattr(args, "project", None):
        out = [s for s in out if args.project in s.cwd]
    if getattr(args, "title", None):
        t = args.title.lower()
        out = [s for s in out if t in s.title.lower()]
    if getattr(args, "skip_archived", False):
        out = [s for s in out if not s.archived]
    return out


def has_positive_selector(args) -> bool:
    return bool(getattr(args, "session", None) or getattr(args, "project", None) or getattr(args, "title", None))


# --------------------------------------------------------------------------- #
# printing
# --------------------------------------------------------------------------- #
def print_session_rows(sessions: list):
    for s in sorted(sessions, key=lambda x: x.last_activity, reverse=True):
        base = os.path.basename(s.cwd.rstrip("/")) or s.cwd or "?"
        flag = "A" if s.archived else " "
        miss = "" if (s.transcript() or not s.cli_id) else "  [no transcript!]"
        title = s.title.replace("\n", " ")
        if len(title) > 46:
            title = title[:45] + "…"
        print(f"  {s.uuid[:8]}  {fmt_ts(s.last_activity)}  {flag}  {base[:18]:18}  {title}{miss}")


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_accounts(args):
    sessions = discover()
    groups = by_account(sessions)
    cur = current_account(sessions)
    if not all_accounts():
        print("no account partitions found.")
        return
    print(f"Claude session partitions under {sessions_root()}\n")
    for acc in all_accounts():
        ss = groups.get(acc, [])
        arch = sum(1 for s in ss if s.archived)
        projs = len({s.cwd for s in ss})
        wss = workspaces_of(acc)
        mark = "  <- current login (guess)" if acc == cur else ""
        print(f"{acc}")
        print(f"  sessions={len(ss)}  archived={arch}  projects={projs}  workspaces={len(wss)}{mark}")
        newest = max(ss, key=lambda s: s.last_activity, default=None)
        if newest:
            print(f"  newest: {fmt_ts(newest.last_activity)}  {newest.title[:54]}")
        print()
    print("Pick source/destination by the uuid (a prefix like the first 8 chars is fine):")
    print("  altpaca list <account>")
    print("  altpaca move <src> <dst> --all          # dry-run by default")


def cmd_list(args):
    acc = resolve_account(args.account)
    ss = [s for s in discover() if s.account == acc]
    if has_positive_selector(args) or args.skip_archived:
        ss = select(ss, args)
    print(f"account {acc}  ({len(ss)} session(s))")
    print(f"  {'uuid':8}  {'last activity':16}  {'':1}  {'project':18}  title")
    print_session_rows(ss)


def make_backup(originals: list, dests: list) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = backup_root() / ts
    fdir = bdir / "files"
    fdir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "version": 1,
        "created_at": ts,
        "sessions_root": str(sessions_root()),
        "originals": [],
        "created_destinations": [],
    }
    to_backup = set()
    for p in originals:
        if Path(p).exists():
            to_backup.add(Path(p))
    for d in dests:
        if Path(d).exists():
            to_backup.add(Path(d))  # pre-existing destination (would be clobbered)
        else:
            manifest["created_destinations"].append(str(d))
    for i, p in enumerate(sorted(to_backup)):
        bf = fdir / f"{i:04d}_{p.name}"
        shutil.copy2(p, bf)
        manifest["originals"].append({"path": str(p), "backup": str(bf.relative_to(bdir))})
    (bdir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return bdir


def transfer(args, remove_source: bool):
    verb = "move" if remove_source else "copy"
    src = resolve_account(args.src)
    dst = resolve_account(args.dst)
    if src == dst:
        die("source and destination are the same account")

    sessions = discover()
    src_ss = [s for s in sessions if s.account == src]

    if has_positive_selector(args):
        chosen = select(src_ss, args)
    elif args.all:
        chosen = select(src_ss, args)  # still honors --skip-archived
    else:
        die("refusing to act without a selection — pass --all or a selector (--session/--project/--title)")

    if not chosen:
        die("no matching sessions in source account")

    # resolve destination workspace
    wss = workspaces_of(dst)
    if not wss:
        die(
            f"destination account {dst[:8]} has no workspace yet.\n"
            "open the Claude app once while logged into that account to initialize it, then retry."
        )
    if args.workspace:
        cand = [w for w in wss if w == args.workspace or w.startswith(args.workspace)]
        if not cand:
            die(f"workspace '{args.workspace}' not in {dst[:8]}: " + ", ".join(w[:8] for w in wss))
        target_ws = cand[0]
    elif len(wss) == 1:
        target_ws = wss[0]
    else:
        recent = {}
        for s in (x for x in sessions if x.account == dst):
            recent[s.workspace] = max(recent.get(s.workspace, 0), s.last_activity)
        target_ws = max(wss, key=lambda w: recent.get(w, 0))
        warn(f"destination has {len(wss)} workspaces; using most-recent {target_ws[:8]} (override with --workspace)")

    dst_dir = sessions_root() / dst / target_ws
    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")

    plan, skipped = [], []
    for s in chosen:
        if env_sid and (s.cli_id == env_sid or s.uuid == env_sid):
            skipped.append((s, "currently running session"))
            continue
        if s.transcript() is None and s.cli_id and not args.force:
            skipped.append((s, "transcript not found (use --force)"))
            continue
        dest = dst_dir / s.path.name
        if dest.exists() and not args.force:
            skipped.append((s, "already present in destination"))
            continue
        plan.append((s, dest))

    print(f"{verb.upper()}  {src[:8]}  ->  {dst[:8]} / {target_ws[:8]}")
    print(f"{len(plan)} session(s) to {verb}:")
    print_session_rows([s for s, _ in plan])
    if skipped:
        print(f"\nskipping {len(skipped)}:")
        for s, why in skipped:
            print(f"  {s.uuid[:8]}  {why}  ({s.title[:40]})")

    if not plan:
        die("nothing to do")

    if not args.apply:
        print(f"\nDRY-RUN — nothing changed. Re-run with --apply to {verb}. (add --yes to skip the prompt)")
        return

    if claude_running() and not args.force:
        die(
            "the Claude desktop app is running — quit it first (it can overwrite changes on exit), "
            "then retry. use --force to override."
        )

    if not args.yes:
        if not sys.stdin.isatty():
            die("refusing to apply without confirmation; pass --yes")
        if input(f"\nProceed to {verb} {len(plan)} session(s)? [y/N] ").strip().lower() not in ("y", "yes"):
            die("aborted")

    backup_dir = None
    if not args.no_backup:
        backup_dir = make_backup([s.path for s, _ in plan], [d for _, d in plan])
        print(f"backup: {backup_dir}")

    done = 0
    for s, dest in plan:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s.path, dest)
        if sha256(dest) != sha256(s.path):
            die(f"verification failed for {s.path.name}; aborting." + (f" backup: {backup_dir}" if backup_dir else ""))
        if remove_source:
            s.path.unlink()
        done += 1

    print(f"\n{verb}d {done} session(s) into {dst[:8]} / {target_ws[:8]}.")
    print("restart the Claude desktop app to see them under that account.")
    if backup_dir:
        print(f"undo with:  altpaca restore {backup_dir.name}")


def cmd_move(args):
    transfer(args, remove_source=True)


def cmd_copy(args):
    transfer(args, remove_source=False)


def cmd_restore(args):
    ref = args.backup
    bdir = Path(ref) if ("/" in ref or os.path.isabs(ref)) else backup_root() / ref
    man_path = bdir / "manifest.json"
    if not man_path.exists():
        die(f"no manifest at {man_path}")
    man = json.loads(man_path.read_text())

    created = [Path(p) for p in man.get("created_destinations", [])]
    originals = man.get("originals", [])
    print(f"restore from {bdir}")
    print(f"  remove {len(created)} created file(s); restore {len(originals)} original(s)")

    if not args.apply:
        print("DRY-RUN — nothing changed. Re-run with --apply.")
        return
    if claude_running() and not args.force:
        die("quit the Claude desktop app first, then retry (or use --force).")
    if not args.yes:
        if not sys.stdin.isatty():
            die("refusing to apply without confirmation; pass --yes")
        if input("Proceed to restore? [y/N] ").strip().lower() not in ("y", "yes"):
            die("aborted")

    for p in created:
        if p.exists():
            p.unlink()
    for o in originals:
        dst = Path(o["path"])
        src = bdir / o["backup"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
    print("restored. restart the Claude desktop app to see the result.")


def cmd_doctor(args):
    print(f"base dir         : {base_dir()}  ({'ok' if base_dir().exists() else 'MISSING'})")
    print(f"sessions root    : {sessions_root()}  ({'ok' if sessions_root().exists() else 'MISSING'})")
    pj = projects_dir()
    print(f"projects dir     : {pj}  ({'ok' if pj.exists() else 'MISSING'})")
    print(f"backup root      : {backup_root()}")
    print(f"Claude running   : {'yes (quit before moving)' if claude_running() else 'no'}")
    print(f"env session id   : {os.environ.get('CLAUDE_CODE_SESSION_ID', '(unset)')}")
    if sessions_root().exists():
        accs = all_accounts()
        print(f"accounts         : {len(accs)}")
        for a in accs:
            print(f"  {a}  workspaces={len(workspaces_of(a))}")


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def add_selectors(sp):
    sp.add_argument("--all", action="store_true", help="select every session in the source account")
    sp.add_argument("--session", nargs="+", metavar="ID", help="select by session/cli uuid (prefix ok)")
    sp.add_argument("--project", metavar="PATH", help="select sessions whose cwd contains PATH")
    sp.add_argument("--title", metavar="SUBSTR", help="select sessions whose title contains SUBSTR (case-insensitive)")
    sp.add_argument("--skip-archived", action="store_true", help="exclude archived sessions")


def build_parser():
    p = argparse.ArgumentParser(
        prog="altpaca",
        description="Move Claude Desktop sessions between account partitions.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("accounts", help="list account partitions and their session counts")
    sp.set_defaults(func=cmd_accounts)

    sp = sub.add_parser("list", help="list sessions in an account")
    sp.add_argument("account", help="account uuid (prefix ok)")
    add_selectors(sp)
    sp.set_defaults(func=cmd_list)

    for name, helptext, remove in (
        ("move", "move sessions to another account (removes from source)", True),
        ("copy", "copy sessions to another account (keeps source)", False),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("src", help="source account uuid (prefix ok)")
        sp.add_argument("dst", help="destination account uuid (prefix ok)")
        add_selectors(sp)
        sp.add_argument("--workspace", help="destination workspace uuid (if the account has several)")
        sp.add_argument("--apply", action="store_true", help="actually perform it (default: dry-run)")
        sp.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
        sp.add_argument("--no-backup", action="store_true", help="do not back up before mutating")
        sp.add_argument("--force", action="store_true", help="proceed despite warnings")
        sp.set_defaults(func=cmd_move if remove else cmd_copy)

    sp = sub.add_parser("restore", help="undo a previous move/copy from its backup")
    sp.add_argument("backup", help="backup id (timestamp) or path under ~/.altpaca/backups")
    sp.add_argument("--apply", action="store_true")
    sp.add_argument("-y", "--yes", action="store_true")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("doctor", help="show detected paths and environment")
    sp.set_defaults(func=cmd_doctor)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
