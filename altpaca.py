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
# TENANTS: several app-data dirs can sit side by side under
#   ~/Library/Application Support/ — the bare "Claude" (the *default* tenant) plus
#   "Claude-<suffix>" siblings (e.g. Claude-excitoon). Each tenant has its OWN
#   claude-code-sessions/<ACCOUNT>/ tree AND its own Local Storage group store, so
#   account uuids and group ids are unique only *within* a tenant. Accounts are
#   addressed as "<suffix>/<uuid>" for named tenants and just "<uuid>" for the
#   default one. Moving a session across tenants relocates the file but does NOT
#   automatically carry group membership (that lives in per-tenant Local Storage).
#   The `regroup` command (and `move/copy --regroup`) re-files membership by group
#   NAME: it recovers each session's source-tenant group name from the source's
#   still-present dframe-store assignments and writes the matching assignment into
#   the destination tenant's store, by *appending* one record to its leveldb
#   write-ahead log. The append is strictly additive (never rewrites existing
#   bytes), so the worst-case failure of a malformed write is "the record is
#   ignored / sessions stay ungrouped", not loss of existing Local Storage — and
#   a backup of the touched .log is taken first. Quit the app before regrouping.
#
# Pure stdlib. MIT licensed.

from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import uuid as _uuidlib
import zipfile
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


# --------------------------------------------------------------------------- #
# tenants & accounts
#
# A *tenant* is one app-data dir (default "Claude", or a "Claude-<suffix>"
# sibling). An *account* is a uuid folder under a tenant's claude-code-sessions/.
# Account refs are "<suffix>/<uuid>" for named tenants, bare "<uuid>" for default.
# --------------------------------------------------------------------------- #
class Tenant:
    def __init__(self, name: str, base):
        self.name = name  # "" for the default (bare-uuid) tenant
        self.base = Path(base)

    @property
    def sessions_root(self) -> Path:
        return self.base / SESSIONS_DIRNAME

    @property
    def local_storage(self) -> Path:
        return self.base / "Local Storage" / "leveldb"

    def __repr__(self):
        return f"Tenant({self.name or '(default)'!r})"


class Account:
    def __init__(self, tenant: Tenant, uuid: str):
        self.tenant = tenant
        self.uuid = uuid

    @property
    def ref(self) -> str:
        return f"{self.tenant.name}/{self.uuid}" if self.tenant.name else self.uuid

    @property
    def short(self) -> str:
        return f"{self.tenant.name}/{self.uuid[:8]}" if self.tenant.name else self.uuid[:8]

    @property
    def sessions_dir(self) -> Path:
        return self.tenant.sessions_root / self.uuid

    def __eq__(self, other):
        if isinstance(other, Account):
            return str(self.tenant.base) == str(other.tenant.base) and self.uuid == other.uuid
        return NotImplemented

    def __hash__(self):
        return hash((str(self.tenant.base), self.uuid))

    def __repr__(self):
        return f"Account({self.ref})"


def default_tenant() -> Tenant:
    return Tenant("", base_dir())


def discover_tenants() -> list:
    """The default tenant (base_dir()) plus any sibling 'Claude-<suffix>' dirs.

    The default tenant is always the bare base dir; the named-tenant prefix is
    always the canonical 'Claude' name, so ALTPACA_CLAUDE_DIR can never promote a
    'Claude-<suffix>' sibling to be the default or redefine the naming scheme.
    """
    pb = base_dir()
    stem = DEFAULT_BASE.name  # always "Claude" — never derived from the env-pointed dir
    tenants = [Tenant("", pb)]
    try:
        siblings = sorted(pb.parent.iterdir())
    except Exception:
        siblings = []
    for d in siblings:
        if d == pb:
            continue  # the default tenant is never also a named one
        if d.is_dir() and d.name.startswith(stem + "-"):
            tenants.append(Tenant(d.name[len(stem) + 1 :], d))
    return tenants


def _short_ref(ref: str) -> str:
    if "/" in ref:
        t, u = ref.split("/", 1)
        return f"{t}/{u[:8]}"
    return ref[:8]


# --------------------------------------------------------------------------- #
# native groups (READ-ONLY). The desktop app keeps custom groups in its
# Local Storage leveldb, under key "dframe-store", as JSON:
#   {"customGroups":[{"id","name"}],
#    "customGroupAssignments":{"code:local_<uuid>":"<group-id>"}}
# We read it with a tiny pure-Python leveldb (SSTable + WAL + Snappy) reader.
# Nothing is ever written back.
# --------------------------------------------------------------------------- #
def local_storage_dir() -> Path:
    return base_dir() / "Local Storage" / "leveldb"


def _uvarint(b, p):
    shift = result = 0
    while True:
        c = b[p]
        p += 1
        result |= (c & 0x7F) << shift
        if not c & 0x80:
            return result, p
        shift += 7


def _snappy_decompress(data: bytes) -> bytes:
    _, p = _uvarint(data, 0)  # uncompressed-length preamble
    out = bytearray()
    n = len(data)
    while p < n:
        tag = data[p]
        p += 1
        t = tag & 3
        if t == 0:
            ln = tag >> 2
            if ln >= 60:
                nb = ln - 59
                ln = int.from_bytes(data[p : p + nb], "little")
                p += nb
            ln += 1
            out += data[p : p + ln]
            p += ln
        else:
            if t == 1:
                length = ((tag >> 2) & 7) + 4
                offset = ((tag >> 5) << 8) | data[p]
                p += 1
            elif t == 2:
                length = (tag >> 2) + 1
                offset = int.from_bytes(data[p : p + 2], "little")
                p += 2
            else:
                length = (tag >> 2) + 1
                offset = int.from_bytes(data[p : p + 4], "little")
                p += 4
            start = len(out) - offset
            for i in range(length):
                out.append(out[start + i])
    return bytes(out)


def _ldb_block(f, offset, size):
    f.seek(offset)
    raw = f.read(size)
    ctype = f.read(5)[:1]  # 1 byte compression type + 4 byte crc
    return _snappy_decompress(raw) if ctype == b"\x01" else raw


def _ldb_block_kvs(block):
    n = len(block)
    num_restarts = int.from_bytes(block[n - 4 : n], "little")
    end = n - 4 * (num_restarts + 1)
    p = 0
    prev = b""
    out = []
    while p < end:
        shared, p = _uvarint(block, p)
        nonshared, p = _uvarint(block, p)
        vlen, p = _uvarint(block, p)
        key = prev[:shared] + block[p : p + nonshared]
        p += nonshared
        out.append((key, block[p : p + vlen]))
        p += vlen
        prev = key
    return out


def _sstable_entries(path):
    with open(path, "rb") as f:
        f.seek(0, 2)
        fsize = f.tell()
        f.seek(fsize - 48)  # footer
        footer = f.read(48)
        p = 0
        _, p = _uvarint(footer, p)  # metaindex handle
        _, p = _uvarint(footer, p)
        idx_off, p = _uvarint(footer, p)
        idx_size, p = _uvarint(footer, p)
        for _k, val in _ldb_block_kvs(_ldb_block(f, idx_off, idx_size)):
            q = 0
            boff, q = _uvarint(val, q)
            bsize, q = _uvarint(val, q)
            try:
                block = _ldb_block(f, boff, bsize)
            except Exception:
                continue
            yield from _ldb_block_kvs(block)


def _log_record_payloads(data: bytes):
    """Reassemble physical log records (FULL/FIRST/MIDDLE/LAST) into payloads.

    The leveldb log format (shared by the WAL *and* the MANIFEST) frames data into
    32 KiB blocks of [crc(4) | len(2) | type(1) | payload] records; this yields the
    logical payloads with multi-block fragments stitched back together.
    """
    recs = []
    frag = b""
    off = 0
    while off < len(data):
        block = data[off : off + 32768]
        off += 32768
        bp = 0
        while bp + 7 <= len(block):
            length = int.from_bytes(block[bp + 4 : bp + 6], "little")
            rtype = block[bp + 6]
            if length == 0 and rtype == 0:
                break
            payload = block[bp + 7 : bp + 7 + length]
            bp += 7 + length
            if rtype == 1:
                recs.append(payload)
            elif rtype == 2:
                frag = payload
            elif rtype == 3:
                frag += payload
            elif rtype == 4:
                recs.append(frag + payload)
                frag = b""
    return recs


def _wal_entries(path):
    for rec in _log_record_payloads(Path(path).read_bytes()):
        if len(rec) < 12:
            continue
        seq = int.from_bytes(rec[0:8], "little")
        count = int.from_bytes(rec[8:12], "little")
        p = 12
        for i in range(count):
            if p >= len(rec):
                break
            tag = rec[p]
            p += 1
            klen, p = _uvarint(rec, p)
            key = rec[p : p + klen]
            p += klen
            if tag == 1:
                vlen, p = _uvarint(rec, p)
                yield key, seq + i, 1, rec[p : p + vlen]
                p += vlen
            elif tag == 0:
                yield key, seq + i, 0, None
            else:
                break


def _split_internal(key):
    if len(key) < 8:
        return key, 0, 1
    trailer = int.from_bytes(key[-8:], "little")
    return key[:-8], trailer >> 8, trailer & 0xFF


def _decode_ls_value(v: bytes) -> str:
    if not v:
        return ""
    if v[0] == 0:
        return v[1:].decode("utf-16-le", "replace")
    if v[0] == 1:
        return v[1:].decode("latin-1", "replace")
    return v.decode("utf-8", "replace")


def _merge_ls(src: Path):
    """Snapshot a Local Storage leveldb dir and merge it to the current view.

    Returns (merged, max_seq) where merged maps each internal user-key -> (seq,
    value-bytes or None for a deletion) keeping the highest-sequence record per
    key (leveldb's last-write-wins), and max_seq is the highest sequence number
    seen anywhere (across .ldb + .log) — the floor the write path bumps past.
    The live DB may be locked/mid-write, so we read a copy, never the originals.
    """
    tmp = Path(tempfile.mkdtemp(prefix="altpaca-ls-"))
    try:
        for f in src.glob("*"):
            try:
                shutil.copy(f, tmp)
            except Exception:
                pass
        merged = {}
        max_seq = 0
        for ldb in sorted(tmp.glob("*.ldb")):
            try:
                for k, v in _sstable_entries(ldb):
                    uk, seq, typ = _split_internal(k)
                    if seq > max_seq:
                        max_seq = seq
                    cur = merged.get(uk)
                    if cur is None or seq > cur[0]:
                        merged[uk] = (seq, None if typ == 0 else v)
            except Exception:
                pass
        for log in sorted(tmp.glob("*.log")):
            try:
                for uk, seq, typ, v in _wal_entries(log):
                    if seq > max_seq:
                        max_seq = seq
                    cur = merged.get(uk)
                    if cur is None or seq > cur[0]:
                        merged[uk] = (seq, None if typ == 0 else v)
            except Exception:
                pass
        return merged, max_seq
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _read_localstorage_blob(needle: str, src: Path = None):
    """Newest decoded localStorage value containing `needle`, or None."""
    src = src if src is not None else local_storage_dir()
    if not src.exists():
        return None
    merged, _ = _merge_ls(src)
    best = None
    for _uk, (seq, v) in merged.items():
        if v is not None:
            text = _decode_ls_value(v)
            if needle in text and (best is None or seq > best[0]):
                best = (seq, text)
    return best[1] if best else None


def _parse_dframe_groups(text: str):
    store = json.loads(text)
    state = store.get("state", store)
    id2name = {g["id"]: g["name"] for g in state.get("customGroups", [])}
    names = [g["name"] for g in state.get("customGroups", [])]
    uuid2group = {}
    for key, gid in state.get("customGroupAssignments", {}).items():
        uuid = key.split("local_", 1)[-1] if "local_" in key else key.rsplit(":", 1)[-1]
        name = id2name.get(gid)
        if name:
            uuid2group[uuid] = name
    return uuid2group, names


_NATIVE_CACHE = {}


def native_groups(tenant: Tenant = None):
    """(uuid -> group name, ordered group names) from a tenant's Local Storage."""
    if tenant is None:
        tenant = default_tenant()
    key = str(tenant.base)
    if key not in _NATIVE_CACHE:
        text = _read_localstorage_blob('"customGroups"', tenant.local_storage)
        try:
            _NATIVE_CACHE[key] = _parse_dframe_groups(text) if text else ({}, [])
        except Exception:
            _NATIVE_CACHE[key] = ({}, [])
    return _NATIVE_CACHE[key]


def match_group_name(query, names):
    for n in names:
        if n.lower() == query.lower():
            return n
    if not names:
        die("could not read the app's groups (Local Storage not found or unreadable)")
    die(f"no such group '{query}'. groups: " + ", ".join(names))


# --------------------------------------------------------------------------- #
# native groups (WRITE path). Used only by `regroup`. We never rewrite existing
# bytes: we APPEND one record to the destination tenant's Local Storage leveldb
# write-ahead log, carrying an updated `dframe-store` value. leveldb replays the
# log on next open and last-write-wins makes our value the live one.
#
# The on-disk framing is verified against google/leveldb (db/log_format.h,
# db/log_writer.cc, util/crc32c.h, db/write_batch.cc) and Chromium's DOM Storage
# value encoding. Safety rests on append-only: a malformed/truncated trailing
# record is dropped by the reader (resync on the 32 KiB block boundary / EOF),
# so the worst case is "our record is ignored", never loss of existing data.
# --------------------------------------------------------------------------- #
_LDB_BLOCK = 32768
_LDB_HEADER = 7
_CRC32C_TABLE = None


def _crc32c(data: bytes) -> int:
    """CRC-32C (Castagnoli, reflected poly 0x82F63B78) — NOT zlib's IEEE CRC32."""
    global _CRC32C_TABLE
    if _CRC32C_TABLE is None:
        tbl = []
        for i in range(256):
            c = i
            for _ in range(8):
                c = (c >> 1) ^ (0x82F63B78 if c & 1 else 0)
            tbl.append(c)
        _CRC32C_TABLE = tbl
    crc = 0xFFFFFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC32C_TABLE[(crc ^ b) & 0xFF]
    return crc ^ 0xFFFFFFFF


def _mask_crc(c: int) -> int:
    """leveldb's util/crc32c.h Mask(): rotate then add kMaskDelta."""
    return (((c >> 15) | (c << 17)) + 0xA282EAD8) & 0xFFFFFFFF


def _ldb_varint(n: int) -> bytes:
    """leveldb base-128 varint (low 7-bit group first; high bit = continuation)."""
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _encode_ls_value(s: str) -> bytes:
    """Chromium DOM-Storage value blob: 0x01+Latin-1 if every code unit <= 0xFF,
    else 0x00+UTF-16LE (matches Blink WTF::String's 8-bit/16-bit choice)."""
    if all(ord(c) <= 0xFF for c in s):
        return b"\x01" + s.encode("latin-1")
    return b"\x00" + s.encode("utf-16-le")


def _write_batch(seq: int, key: bytes, value: bytes) -> bytes:
    """A single-Put WriteBatch payload: fixed64 seq + fixed32 count(=1) + op."""
    if seq > (1 << 56) - 1:
        die("computed leveldb sequence is implausibly large; refusing to write")
    return (
        struct.pack("<Q", seq)
        + struct.pack("<I", 1)
        + b"\x01"  # kTypeValue
        + _ldb_varint(len(key))
        + key
        + _ldb_varint(len(value))
        + value
    )


def _frame_log_append(payload: bytes, file_size: int) -> bytes:
    """Frame `payload` into physical log record(s) to append at `file_size`.

    Replicates leveldb log::Writer.AddRecord exactly: it seeds block_offset from
    the current file length, zero-pads a sub-7-byte block tail, and splits across
    FULL/FIRST/MIDDLE/LAST fragments on 32 KiB boundaries. A localStorage value is
    far under one block, so this is virtually always a single FULL record — but the
    fragmentation path is here so a write that happens to straddle a boundary stays
    valid rather than being silently rejected by the reader.
    """
    out = bytearray()
    block_offset = file_size % _LDB_BLOCK
    ptr = 0
    left = len(payload)
    begin = True
    while True:
        if _LDB_BLOCK - block_offset < _LDB_HEADER:
            out += b"\x00" * (_LDB_BLOCK - block_offset)  # zero trailer; reader skips it
            block_offset = 0
        # When exactly _LDB_HEADER bytes remain, avail==0 and leveldb emits a
        # zero-length FIRST record to consume the tail, spilling the payload into
        # the next block (doc/log_format.md). We match that on purpose — it is NOT
        # a bug to "pad away"; diverging here would stop matching leveldb's writer.
        avail = _LDB_BLOCK - block_offset - _LDB_HEADER
        frag = min(left, avail)
        end = frag == left
        rtype = 1 if (begin and end) else 2 if begin else 4 if end else 3
        chunk = payload[ptr : ptr + frag]
        crc = _mask_crc(_crc32c(bytes([rtype]) + chunk))
        out += struct.pack("<IHB", crc, frag, rtype) + chunk
        block_offset += _LDB_HEADER + frag
        ptr += frag
        left -= frag
        begin = False
        if left == 0:
            return bytes(out)


def _append_log_record(log_path: Path, payload: bytes):
    """Strictly append one WriteBatch to the end of an existing .log (fsync'd)."""
    with open(log_path, "r+b") as f:
        f.seek(0, 2)
        size = f.tell()
        f.write(_frame_log_append(payload, size))
        f.flush()
        os.fsync(f.fileno())


def _parse_version_edit(rec: bytes) -> dict:
    """Decode the tags we need from a leveldb MANIFEST VersionEdit record.

    Returns {tag: int} for log_number (2), next_file (3), last_sequence (4),
    prev_log_number (9). Other tags are skipped by walking their payloads.
    """
    out = {}
    p = 0
    n = len(rec)
    try:
        while p < n:
            tag, p = _uvarint(rec, p)
            if tag in (2, 3, 4, 9):  # varint scalars we care about
                val, p = _uvarint(rec, p)
                out[tag] = val
            elif tag == 1:  # comparator: length-prefixed string
                ln, p = _uvarint(rec, p)
                p += ln
            elif tag == 5:  # compact pointer: level + length-prefixed key
                _, p = _uvarint(rec, p)
                ln, p = _uvarint(rec, p)
                p += ln
            elif tag == 6:  # deleted file: level + file number
                _, p = _uvarint(rec, p)
                _, p = _uvarint(rec, p)
            elif tag == 7:  # new file: level + number + size + smallest + largest
                _, p = _uvarint(rec, p)
                _, p = _uvarint(rec, p)
                _, p = _uvarint(rec, p)
                ln, p = _uvarint(rec, p)
                p += ln
                ln, p = _uvarint(rec, p)
                p += ln
            else:  # unknown tag — can't safely keep walking
                break
    except Exception:
        pass
    return out


def _ldb_active_log(ls_dir: Path):
    """(active .log path, last-sequence floor) from CURRENT -> MANIFEST.

    The active log is the one referenced by the manifest's log_number; appending
    to any other .log is harmless but ineffective (leveldb won't replay it). Falls
    back to (None, 0) if the manifest can't be read so the caller can use the
    highest-numbered .log instead.
    """
    current = ls_dir / "CURRENT"
    if not current.exists():
        return None, 0
    try:
        man_name = current.read_text().strip()
    except Exception:
        return None, 0
    manifest = ls_dir / man_name
    if not manifest.exists():
        return None, 0
    log_number = None
    last_seq = 0
    try:
        for rec in _log_record_payloads(manifest.read_bytes()):
            edit = _parse_version_edit(rec)
            if 2 in edit:
                log_number = edit[2]
            if 4 in edit:
                last_seq = max(last_seq, edit[4])
    except Exception:
        return None, 0
    log_path = None
    if log_number is not None:
        cand = ls_dir / f"{log_number:06d}.log"
        if cand.exists():
            log_path = cand
    return log_path, last_seq


def _dframe_record(merged):
    """(internal key bytes, parsed store dict) for the newest dframe-store record.

    Returns the VERBATIM on-disk key bytes (reused unchanged on write so we never
    have to reconstruct Chromium's key framing) and the whole `{state, version}`
    store object. (None, None) if there is no group store.
    """
    best = None
    for uk, (seq, v) in merged.items():
        if v is not None:
            text = _decode_ls_value(v)
            if '"customGroups"' in text and (best is None or seq > best[0]):
                best = (seq, uk, text)
    if not best:
        return None, None
    try:
        return best[1], json.loads(best[2])
    except Exception:
        return None, None


def _store_state(store: dict) -> dict:
    return store.get("state", store)


def recover_source_group_names(src_ls: Path) -> dict:
    """uuid -> source group NAME, from the source tenant's dframe-store.

    A cross-tenant move removes the session *file* from the source but leaves its
    `customGroupAssignments` row intact, so this recovers the group each moved
    session belonged to even after the move.
    """
    if not src_ls.exists():
        return {}
    merged, _ = _merge_ls(src_ls)
    _key, store = _dframe_record(merged)
    if not store:
        return {}
    st = _store_state(store)
    id2name = {g["id"]: g["name"] for g in st.get("customGroups", [])}
    out = {}
    for k, gid in st.get("customGroupAssignments", {}).items():
        u = k.split("local_", 1)[-1] if "local_" in k else k.rsplit(":", 1)[-1]
        if gid in id2name:
            out[u] = id2name[gid]
    return out


def die(msg: str, code: int = 1):
    print(f"altpaca: error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def warn(msg: str):
    print(f"altpaca: warning: {msg}", file=sys.stderr)


def fmt_ts(ms) -> str:
    if not ms:
        return "?"
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


class Progress:
    """Minimal in-place progress bar on a TTY; a no-op when piped/redirected."""

    def __init__(self, total, label="", stream=None, width=24):
        self.total = total
        self.label = label
        self.width = width
        self.stream = stream if stream is not None else sys.stderr
        self.enabled = total > 0 and hasattr(self.stream, "isatty") and self.stream.isatty()

    def render(self, n, suffix=""):
        if not self.enabled:
            return
        frac = max(0.0, min(1.0, n / self.total))
        filled = int(self.width * frac)
        bar = "#" * filled + "-" * (self.width - filled)
        if len(suffix) > 32:
            suffix = suffix[:31] + "…"
        self.stream.write(f"\r{self.label}[{bar}] {n}/{self.total}  {suffix}\033[K")
        self.stream.flush()

    def finish(self):
        if self.enabled:
            self.stream.write("\r\033[K")  # clear the bar line
            self.stream.flush()


# --------------------------------------------------------------------------- #
# model
# --------------------------------------------------------------------------- #
class Session:
    def __init__(self, path: Path, tenant: Tenant = None):
        self.path = Path(path)
        self.tenant = tenant
        self.workspace = self.path.parent.name
        self.account = self.path.parent.parent.name
        try:
            self.meta = json.loads(self.path.read_text())
        except Exception as e:  # keep going; surface a warning
            self.meta = {}
            warn(f"could not parse {self.path.name}: {e}")

    @property
    def tenant_name(self) -> str:
        return self.tenant.name if self.tenant else ""

    @property
    def account_ref(self) -> str:
        if self.tenant and self.tenant.name:
            return f"{self.tenant.name}/{self.account}"
        return self.account

    @property
    def account_obj(self) -> Account:
        return Account(self.tenant or default_tenant(), self.account)

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


def all_account_objs() -> list:
    out = []
    for t in discover_tenants():
        root = t.sessions_root
        if not root.exists():
            continue
        for p in sorted(root.iterdir()):
            if p.is_dir():
                out.append(Account(t, p.name))
    out.sort(key=lambda a: a.ref)
    return out


def all_accounts() -> list:
    return [a.ref for a in all_account_objs()]


def workspaces_of(account: Account) -> list:
    accdir = account.sessions_dir
    if not accdir.exists():
        return []
    return sorted(p.name for p in accdir.iterdir() if p.is_dir())


def discover() -> list:
    tenants = [t for t in discover_tenants() if t.sessions_root.exists()]
    if not tenants:
        die(
            f"sessions dir not found: {sessions_root()}\n"
            "is the Claude desktop app installed? set ALTPACA_CLAUDE_DIR to override."
        )
    out = []
    for t in tenants:
        for acc in sorted(p for p in t.sessions_root.iterdir() if p.is_dir()):
            for ws in sorted(p for p in acc.iterdir() if p.is_dir()):
                for f in sorted(ws.glob("local_*.json")):
                    out.append(Session(f, tenant=t))
    return out


def by_account(sessions: list) -> dict:
    d = {}
    for s in sessions:
        d.setdefault(s.account_ref, []).append(s)
    return d


def current_account(sessions: list):
    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if env_sid:
        for s in sessions:
            if s.cli_id == env_sid or s.uuid == env_sid:
                return s.account_obj
    best, acc = -1, None
    for s in sessions:
        if s.last_activity > best:
            best, acc = s.last_activity, s.account_obj
    return acc


def resolve_account(ref: str) -> Account:
    """Resolve '<tenant>/<uuid>' (or bare '<uuid>' for the default tenant) to an Account.

    Tenant name and uuid both accept a prefix; bare refs only ever match the
    default tenant, so the same uuid living in two tenants stays unambiguous.
    """
    if "/" in ref:
        tname, uref = ref.split("/", 1)
    else:
        tname, uref = "", ref
    tenants = discover_tenants()
    if tname == "":
        cand_t = [t for t in tenants if t.name == ""]
    else:
        exact = [t for t in tenants if t.name == tname]
        cand_t = exact or [t for t in tenants if t.name.startswith(tname)]
    matches = []
    for t in cand_t:
        root = t.sessions_root
        if not root.exists():
            continue
        for p in sorted(root.iterdir()):
            if p.is_dir() and (p.name == uref or p.name.startswith(uref)):
                matches.append(Account(t, p.name))
    if not matches:
        known = all_accounts()
        if not known:
            die("no account partitions found")
        die(f"no account matches '{ref}'. known: " + ", ".join(_short_ref(a) for a in known))
    if len(matches) > 1:
        die(f"'{ref}' is ambiguous: " + ", ".join(a.short for a in matches))
    return matches[0]


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
    if getattr(args, "group", None) and out:
        # callers pass single-account (so single-tenant) slices; resolve the
        # group name within that tenant's own Local Storage.
        uuid2group, names = native_groups(out[0].tenant)
        target = match_group_name(args.group, names)
        out = [s for s in out if uuid2group.get(s.uuid) == target]
    if getattr(args, "skip_archived", False):
        out = [s for s in out if not s.archived]
    return out


def has_positive_selector(args) -> bool:
    return bool(
        getattr(args, "session", None)
        or getattr(args, "project", None)
        or getattr(args, "title", None)
        or getattr(args, "group", None)
    )


# --------------------------------------------------------------------------- #
# printing
# --------------------------------------------------------------------------- #
def print_session_rows(sessions: list, uuid2group: dict = None):
    show_group = uuid2group is not None
    for s in sorted(sessions, key=lambda x: x.last_activity, reverse=True):
        base = os.path.basename(s.cwd.rstrip("/")) or s.cwd or "?"
        flag = "A" if s.archived else " "
        miss = "" if (s.transcript() or not s.cli_id) else "  [no transcript!]"
        title = s.title.replace("\n", " ")
        if len(title) > 46:
            title = title[:45] + "…"
        cells = [f"{s.uuid[:8]:8}", f"{fmt_ts(s.created):16}", f"{fmt_ts(s.last_activity):16}", flag]
        if show_group:
            cells.append(f"{(uuid2group.get(s.uuid) or '')[:12]:12}")
        cells.append(f"{base[:18]:18}")
        cells.append(title)
        print("  " + "  ".join(cells) + miss)


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def cmd_accounts(args):
    sessions = discover()
    groups = by_account(sessions)
    cur = current_account(sessions)
    accts = all_account_objs()
    if not accts:
        print("no account partitions found.")
        return
    tenants = discover_tenants()
    print(f"Claude session partitions across {len(tenants)} tenant(s):")
    for t in tenants:
        print(f"  {t.name or '(default)':12}  {t.base}")
    print()
    for acc in accts:
        ss = groups.get(acc.ref, [])
        arch = sum(1 for s in ss if s.archived)
        projs = len({s.cwd for s in ss})
        wss = workspaces_of(acc)
        mark = "  <- current login (guess)" if (cur is not None and acc == cur) else ""
        print(f"{acc.ref}")
        print(f"  sessions={len(ss)}  archived={arch}  projects={projs}  workspaces={len(wss)}{mark}")
        newest = max(ss, key=lambda s: s.last_activity, default=None)
        if newest:
            print(f"  newest: {fmt_ts(newest.last_activity)}  {newest.title[:54]}")
        print()
    print("Pick source/destination by the account ref ('<uuid>', or '<tenant>/<uuid>'; prefixes ok):")
    print("  altpaca list <account>")
    print("  altpaca move <src> <dst> --all          # dry-run by default")


def cmd_list(args):
    sessions = discover()
    accounts = [resolve_account(args.account)] if args.account else all_account_objs()
    if not accounts:
        print("no accounts found.")
        return
    cur = current_account(sessions)
    total = 0
    for i, acc in enumerate(accounts):
        ss = [s for s in sessions if s.account_ref == acc.ref]
        if has_positive_selector(args) or args.skip_archived:
            ss = select(ss, args)
        uuid2group, _names = native_groups(acc.tenant)
        show_group = bool(uuid2group)
        mark = "  <- current login (guess)" if (not args.account and acc == cur) else ""
        if i:
            print()
        print(f"account {acc.ref}  ({len(ss)} session(s)){mark}")
        hdr = [f"{'uuid':8}", f"{'first activity':16}", f"{'last activity':16}", " "]
        if show_group:
            hdr.append(f"{'group':12}")
        hdr.append(f"{'project':18}")
        hdr.append("title")
        print("  " + "  ".join(hdr))
        print_session_rows(ss, uuid2group=uuid2group if show_group else None)
        total += len(ss)
    if not args.account and len(accounts) > 1:
        print(f"\ntotal: {total} session(s) across {len(accounts)} account(s)")


def _slug(text: str, n: int = 40) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text or "").strip("-").lower()
    return s[:n] or "session"


def _entry_name(s: Session) -> str:
    created = datetime.fromtimestamp(s.created / 1000).strftime("%Y%m%d-%H%M%S") if s.created else "unknown"
    return f"{created}_{s.uuid[:8]}_{_slug(s.title)}.altpaca.json"


def _unique_path(p: Path) -> Path:
    """Never overwrite: if p exists, append -2, -3, … before the suffix."""
    if not p.exists():
        return p
    i = 2
    while True:
        cand = p.with_name(f"{p.stem}-{i}{p.suffix}")
        if not cand.exists():
            return cand
        i += 1


def _read_transcript(path: Path) -> list:
    out = []
    for line in Path(path).read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            out.append({"_raw": line})
    return out


def cmd_dump(args):
    acc = resolve_account(args.account)
    ss = [s for s in discover() if s.account_ref == acc.ref]
    if not ss:
        die("no sessions in that account")

    run_stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    default_name = f"altpaca-dump_{acc.uuid[:8]}_{run_stamp}.zip"
    if args.out:
        o = Path(args.out).expanduser()
        archive = o if o.suffix == ".zip" else o / default_name
    else:
        archive = HOME / ".altpaca" / "dumps" / default_name
    archive = _unique_path(archive)
    print(f"dumping {len(ss)} session(s) from {acc.short} -> {archive}")
    if args.dry_run:
        for s in ss:
            print(f"  would add {_entry_name(s)}")
        print(f"\n(dry-run) would write 1 archive: {archive.name}")
        return

    archive.parent.mkdir(parents=True, exist_ok=True)
    added = failed = 0
    seen = set()
    progress = Progress(len(ss), label="archiving ")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, s in enumerate(ss):
            progress.render(i, s.title)
            tpath = s.transcript()
            bundle = {
                "altpaca_dump": 1,
                "exported_at": datetime.now().isoformat(timespec="seconds"),
                "source_account": acc.ref,
                "source_tenant": acc.tenant.name or "(default)",
                "source_workspace": s.workspace,
                "session_file": s.path.name,
                "metadata": s.meta,
                "cwd": s.cwd,
                "cli_session_id": s.cli_id,
                "transcript_file": str(tpath) if tpath else None,
                "transcript": _read_transcript(tpath) if tpath else None,
            }
            name = _entry_name(s)
            while name in seen:  # guard against rare uuid8+title collision
                name = name[: -len(".altpaca.json")] + "_.altpaca.json"
            seen.add(name)
            try:
                # tolerate lone surrogates (broken emoji halves) in transcripts
                data = json.dumps(bundle, indent=2, ensure_ascii=False).encode("utf-8", "replace")
                zf.writestr(name, data)
            except Exception as e:
                failed += 1
                warn(f"could not add {s.uuid[:8]}: {e}")
                continue
            added += 1
    progress.finish()
    suffix = f" ({failed} failed)" if failed else ""
    print(f"wrote {archive} — {added} session(s), {archive.stat().st_size} bytes{suffix}")


def make_backup(originals: list, dests: list) -> Path:
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    archive = _unique_path(backup_root() / f"altpaca-backup_{ts}.zip")
    manifest = {
        "version": 2,
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
    entries = []
    for i, p in enumerate(sorted(to_backup)):
        arc = f"files/{i:04d}_{p.name}"
        entries.append((arc, p))
        manifest["originals"].append({"path": str(p), "backup": arc})
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.json", json.dumps(manifest, indent=2))
        for arc, p in entries:
            zf.write(p, arc)
    return archive


def regroup(src_tenant, dst_tenant, candidates, *, apply, force, no_backup, yes, header=True):
    """Re-file `candidates` (sessions now in dst_tenant) into dst_tenant's groups
    by matching each session's source-tenant group NAME, writing the result by
    appending one record to the destination's Local Storage leveldb.

    Returns the number of sessions regrouped (0 on a dry-run). The Claude desktop
    app must be quit — it owns the destination's Local Storage.
    """
    if str(src_tenant.base) == str(dst_tenant.base):
        die("regroup needs two different tenants (within-tenant moves keep grouping)")
    dst_ls = dst_tenant.local_storage
    if not dst_ls.exists():
        die(f"destination tenant has no Local Storage at {dst_ls}")

    uuid2name = recover_source_group_names(src_tenant.local_storage)
    if not uuid2name:
        die("no group assignments are recoverable from the source tenant's Local Storage")

    dst_merged, dst_max = _merge_ls(dst_ls)
    ver = next(
        (_decode_ls_value(v) for uk, (sq, v) in dst_merged.items() if v is not None and uk == b"VERSION"),
        None,
    )
    if ver is not None and ver != "1":
        die(
            f"destination Local Storage schema version is {ver!r}, expected '1' — refusing to write "
            "(the layout may have changed)"
        )
    dst_key, dst_store = _dframe_record(dst_merged)
    if dst_store is None or dst_key is None:
        die("destination tenant has no group store yet — open the app once on that tenant first")
    st = _store_state(dst_store)
    assign = st.setdefault("customGroupAssignments", {})
    groups = st.setdefault("customGroups", [])
    order = st.setdefault("customGroupOrder", [])  # so a freshly minted group is also ordered
    name2id = {g["name"]: g["id"] for g in groups}
    id2name = {g["id"]: g["name"] for g in groups}

    plan, skipped = [], []
    for s in candidates:
        name = uuid2name.get(s.uuid)
        if not name:
            skipped.append((s, "no group at source"))
            continue
        akey = f"code:local_{s.uuid}"
        if akey in assign and not force:
            skipped.append((s, f"already in '{id2name.get(assign[akey], '?')}' (use --force to override)"))
            continue
        plan.append((s, name))

    if header:
        print(f"REGROUP  {src_tenant.name or 'default'}  ->  {dst_tenant.name or 'default'}")
    print(f"{len(plan)} session(s) to regroup by group name:")
    bygroup = {}
    for s, name in plan:
        bygroup.setdefault(name, []).append(s)
    for name in sorted(bygroup):
        tag = "" if name in name2id else "  [new group — will be created]"
        print(f"  {name}  ({len(bygroup[name])}){tag}")
        for s in sorted(bygroup[name], key=lambda x: x.last_activity, reverse=True):
            base = os.path.basename(s.cwd.rstrip("/")) or s.cwd or "?"
            print(f"      {s.uuid[:8]}  {base[:18]:18}  {s.title[:42]}")
    if skipped:
        print(f"\nskipping {len(skipped)}:")
        for s, why in skipped[:40]:
            print(f"  {s.uuid[:8]}  {why}")
        if len(skipped) > 40:
            print(f"  ... and {len(skipped) - 40} more")

    if not plan:
        die("nothing to regroup")

    if not apply:
        print(
            f"\nDRY-RUN — nothing changed. Re-run with --apply to write group membership into "
            f"{dst_tenant.name or 'default'}'s Local Storage. (add --yes to skip the prompt)"
        )
        return 0

    if claude_running() and not force:
        die(
            "the Claude desktop app is running — quit it first (it owns this Local Storage and can "
            "clobber the write), then retry. use --force to override."
        )

    if not yes:
        if not sys.stdin.isatty():
            die("refusing to apply without confirmation; pass --yes")
        prompt = f"\nProceed to regroup {len(plan)} session(s) in {dst_tenant.name or 'default'}? [y/N] "
        if input(prompt).strip().lower() not in ("y", "yes"):
            die("aborted")
        if claude_running() and not force:  # re-check: the app may have started during the prompt
            die("the Claude desktop app started while waiting — quit it and retry (it can clobber the write).")

    active_log, man_seq = _ldb_active_log(dst_ls)
    if active_log is None:
        logs = sorted(dst_ls.glob("*.log"))
        if not logs:
            die("no .log file in the destination's Local Storage; cannot append a write")
        active_log = max(logs, key=lambda p: p.name)
        warn(f"could not read MANIFEST; appending to highest-numbered log {active_log.name}")
    new_seq = max(dst_max, man_seq) + 1

    backup_dir = None
    if not no_backup:
        backup_dir = make_backup([active_log], [])
        print(f"backup: {backup_dir}")

    for s, name in plan:
        gid = name2id.get(name)
        if gid is None:  # name absent in destination — mint a fresh group
            gid = "cg-" + str(_uuidlib.uuid4())
            groups.append({"id": gid, "name": name})
            if isinstance(order, list):
                order.append(gid)
            name2id[name] = gid
        assign[f"code:local_{s.uuid}"] = gid

    new_text = json.dumps(dst_store, ensure_ascii=False, separators=(",", ":"))
    payload = _write_batch(new_seq, dst_key, _encode_ls_value(new_text))
    _append_log_record(active_log, payload)

    print(f"\nregrouped {len(plan)} session(s) in {dst_tenant.name or 'default'}.")
    print("restart the Claude desktop app to see them filed under their groups.")
    if backup_dir:
        print(f"undo the grouping with:  altpaca restore {backup_dir.name}")
    return len(plan)


def cmd_regroup(args):
    src = resolve_account(args.src)
    dst = resolve_account(args.dst)
    if str(src.tenant.base) == str(dst.tenant.base):
        die("source and destination are in the same tenant — within-tenant grouping is preserved automatically")
    dst_base = str(dst.tenant.base)
    candidates = [s for s in discover() if str((s.tenant or default_tenant()).base) == dst_base]
    if not candidates:
        die(f"no sessions found in the destination tenant {dst.tenant.name or 'default'}")
    regroup(
        src.tenant,
        dst.tenant,
        candidates,
        apply=args.apply,
        force=args.force,
        no_backup=args.no_backup,
        yes=args.yes,
    )


def transfer(args, remove_source: bool):
    verb = "move" if remove_source else "copy"
    src = resolve_account(args.src)
    dst = resolve_account(args.dst)
    if src == dst:
        die("source and destination are the same account")

    cross_tenant = str(src.tenant.base) != str(dst.tenant.base)
    regrouping = cross_tenant and getattr(args, "regroup", False)
    if cross_tenant:
        tlabel = f"{src.tenant.name or 'default'} -> {dst.tenant.name or 'default'}"
        if regrouping:
            warn(
                f"cross-tenant {verb} ({tlabel}): after the {verb}, group membership will be re-filed "
                f"by group NAME in the destination's Local Storage (--regroup)."
            )
        else:
            warn(
                f"cross-tenant {verb} ({tlabel}): session files move, but sidebar GROUP membership will "
                "NOT carry — groups live in each tenant's own Local Storage. Pass --regroup to re-file it "
                "by group name (or run `altpaca regroup <src> <dst>` afterward); otherwise sessions land "
                "ungrouped."
            )

    sessions = discover()
    src_ss = [s for s in sessions if s.account_ref == src.ref]

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
            f"destination account {dst.short} has no workspace yet.\n"
            "open the Claude app once while logged into that account to initialize it, then retry."
        )
    if args.workspace:
        cand = [w for w in wss if w == args.workspace or w.startswith(args.workspace)]
        if not cand:
            die(f"workspace '{args.workspace}' not in {dst.short}: " + ", ".join(w[:8] for w in wss))
        target_ws = cand[0]
    elif len(wss) == 1:
        target_ws = wss[0]
    else:
        recent = {}
        for s in (x for x in sessions if x.account_ref == dst.ref):
            recent[s.workspace] = max(recent.get(s.workspace, 0), s.last_activity)
        target_ws = max(wss, key=lambda w: recent.get(w, 0))
        warn(f"destination has {len(wss)} workspaces; using most-recent {target_ws[:8]} (override with --workspace)")

    dst_dir = dst.sessions_dir / target_ws
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

    print(f"{verb.upper()}  {src.short}  ->  {dst.short} / {target_ws[:8]}")
    print(f"{len(plan)} session(s) to {verb}:")
    print_session_rows([s for s, _ in plan])
    if skipped:
        print(f"\nskipping {len(skipped)}:")
        for s, why in skipped:
            print(f"  {s.uuid[:8]}  {why}  ({s.title[:40]})")

    if not plan:
        die("nothing to do")

    if not args.apply:
        if regrouping:
            print(
                f"(--regroup) would then re-file up to {len(plan)} moved session(s) "
                f"by group name in {dst.tenant.name or 'default'}."
            )
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

    print(f"\n{verb}d {done} session(s) into {dst.short} / {target_ws[:8]}.")
    print("restart the Claude desktop app to see them under that account.")
    if backup_dir:
        print(f"undo with:  altpaca restore {backup_dir.name}")

    if regrouping:
        print()
        regroup(
            src.tenant,
            dst.tenant,
            [s for s, _ in plan],
            apply=True,
            force=args.force,
            no_backup=args.no_backup,
            yes=True,  # the move itself was already confirmed above
            header=True,
        )


def cmd_move(args):
    transfer(args, remove_source=True)


def cmd_copy(args):
    transfer(args, remove_source=False)


def cmd_restore(args):
    ref = args.backup
    candidates = [
        Path(ref),
        backup_root() / ref,
        backup_root() / f"{ref}.zip",
        backup_root() / f"altpaca-backup_{ref}.zip",
    ]
    archive = next((c for c in candidates if c.exists() and c.is_file()), None)
    if archive is None:
        die(f"backup not found: {ref}")
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile:
        die(f"not a valid backup archive: {archive}")
    with zf:
        try:
            man = json.loads(zf.read("manifest.json"))
        except KeyError:
            die(f"not an altpaca backup (no manifest): {archive}")
        created = [Path(p) for p in man.get("created_destinations", [])]
        originals = man.get("originals", [])
        print(f"restore from {archive}")
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
            if claude_running() and not args.force:  # re-check after the prompt (it owns Local Storage)
                die("the Claude desktop app started while waiting — quit it and retry.")

        for p in created:
            if p.exists():
                p.unlink()
        for o in originals:
            dst = Path(o["path"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(zf.read(o["backup"]))
    print("restored. restart the Claude desktop app to see the result.")


def cmd_doctor(args):
    print(f"base dir         : {base_dir()}  ({'ok' if base_dir().exists() else 'MISSING'})")
    tenants = discover_tenants()
    print(f"tenants          : {len(tenants)}")
    for t in tenants:
        sr = t.sessions_root
        print(f"  {t.name or '(default)':12}  {t.base}  (sessions-root {'ok' if sr.exists() else 'MISSING'})")
    pj = projects_dir()
    print(f"projects dir     : {pj}  ({'ok' if pj.exists() else 'MISSING'})")
    print(f"backup root      : {backup_root()}")
    print(f"Claude running   : {'yes (quit before moving)' if claude_running() else 'no'}")
    print(f"env session id   : {os.environ.get('CLAUDE_CODE_SESSION_ID', '(unset)')}")
    accts = all_account_objs()
    print(f"accounts         : {len(accts)}")
    for a in accts:
        print(f"  {a.ref}  workspaces={len(workspaces_of(a))}")


def cmd_groups(args):
    sessions = discover()
    any_groups = False
    for t in discover_tenants():
        uuid2group, names = native_groups(t)
        if not names:
            continue
        if any_groups:
            print()
        any_groups = True
        if t.name:  # header only for named tenants; default stays bare
            print(f"=== tenant {t.name} ===")
        index = {s.uuid: s for s in sessions if str((s.tenant or default_tenant()).base) == str(t.base)}
        members = {n: [] for n in names}
        for uuid, gname in uuid2group.items():
            if uuid in index:
                members.setdefault(gname, []).append(index[uuid])
        for name in names:
            present = members.get(name, [])
            print(f"{name}  ({len(present)} session(s) present)")
            for s in sorted(present, key=lambda x: x.last_activity, reverse=True):
                base = os.path.basename(s.cwd.rstrip("/")) or s.cwd or "?"
                print(f"  {s.uuid[:8]}  {base[:18]:18}  {s.title[:50]}")
        ungrouped = [s for s in index.values() if s.uuid not in uuid2group]
        if ungrouped:
            print(f"\nUngrouped: {len(ungrouped)} session(s) present")
    if not any_groups:
        print("no app groups found (could not read Local Storage, or none defined).")


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def add_selectors(sp):
    sp.add_argument("--all", action="store_true", help="select every session in the source account")
    sp.add_argument("--session", nargs="+", metavar="ID", help="select by session/cli uuid (prefix ok)")
    sp.add_argument("--project", metavar="PATH", help="select sessions whose cwd contains PATH")
    sp.add_argument("--title", metavar="SUBSTR", help="select sessions whose title contains SUBSTR (case-insensitive)")
    sp.add_argument("--group", metavar="NAME", help="select sessions in an app group (see: altpaca groups)")
    sp.add_argument("--skip-archived", action="store_true", help="exclude archived sessions")


def build_parser():
    p = argparse.ArgumentParser(
        prog="altpaca",
        description="Move Claude Desktop sessions between account partitions.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("accounts", help="list account partitions and their session counts")
    sp.set_defaults(func=cmd_accounts)

    sp = sub.add_parser("list", help="list sessions (all accounts if none given)")
    sp.add_argument("account", nargs="?", help="account ref '<uuid>' or '<tenant>/<uuid>' (prefix ok; omit for all)")
    add_selectors(sp)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser("dump", help="archive a whole account's sessions into one .zip (metadata + transcripts)")
    sp.add_argument("account", help="account ref '<uuid>' or '<tenant>/<uuid>' (prefix ok) — whole account archived")
    sp.add_argument("--out", metavar="PATH", help="output dir, or a .zip path (default ~/.altpaca/dumps)")
    sp.add_argument("-n", "--dry-run", action="store_true", help="show the archive contents without writing")
    sp.set_defaults(func=cmd_dump)

    sp = sub.add_parser("groups", help="list the app's custom groups and their members (read-only)")
    sp.set_defaults(func=cmd_groups)

    for name, helptext, remove in (
        ("move", "move sessions to another account (removes from source)", True),
        ("copy", "copy sessions to another account (keeps source)", False),
    ):
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("src", help="source account ref '<uuid>' or '<tenant>/<uuid>' (prefix ok)")
        sp.add_argument("dst", help="destination account ref '<uuid>' or '<tenant>/<uuid>' (prefix ok)")
        add_selectors(sp)
        sp.add_argument("--workspace", help="destination workspace uuid (if the account has several)")
        sp.add_argument("--apply", action="store_true", help="actually perform it (default: dry-run)")
        sp.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
        sp.add_argument("--no-backup", action="store_true", help="do not back up before mutating")
        sp.add_argument("--force", action="store_true", help="proceed despite warnings")
        sp.add_argument(
            "--regroup",
            action="store_true",
            help="on a cross-tenant move/copy, also re-file sidebar group membership by group name "
            "in the destination (writes its Local Storage; see the `regroup` command)",
        )
        sp.set_defaults(func=cmd_move if remove else cmd_copy)

    sp = sub.add_parser(
        "regroup",
        help="re-file sidebar group membership across tenants by group name (writes the destination's Local Storage)",
    )
    sp.add_argument("src", help="source account ref — the tenant whose grouping to recover from (prefix ok)")
    sp.add_argument("dst", help="destination account ref — its tenant's Local Storage gets the assignments (prefix ok)")
    sp.add_argument("--apply", action="store_true", help="actually write it (default: dry-run)")
    sp.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    sp.add_argument("--no-backup", action="store_true", help="do not back up the touched .log first")
    sp.add_argument(
        "--force",
        action="store_true",
        help="proceed despite warnings; also override sessions already grouped in the destination",
    )
    sp.set_defaults(func=cmd_regroup)

    sp = sub.add_parser("restore", help="undo a previous move/copy from its backup")
    sp.add_argument("backup", help="backup archive name/path (or its timestamp) under ~/.altpaca/backups")
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
