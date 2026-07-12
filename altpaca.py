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
#   The `regroup` command (run automatically as the tail of a cross-tenant
#   move/copy, or standalone) re-files membership by group
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
import csv
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

try:
    import fcntl  # POSIX advisory locking (macOS/Linux); serializes concurrent ledger writes
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None

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
# account identity (READ-ONLY). The desktop app records the account currently
# signed in for a tenant in its claude.ai IndexedDB, as a serialized object:
#   data.account = {tagged_id, uuid, email_address, full_name, display_name}
# Big values spill to external blob files under
#   <tenant>/IndexedDB/https_claude.ai_*.indexeddb.blob/**.
# That record's `uuid` IS the account's claude-code-sessions/<uuid> folder, so it
# ties an account partition to the email signed into it. Strings are framed as
#   0x22 <len-byte> <utf-8 bytes>. Best-effort and never written: any parse miss
# just yields no email, and the listing falls back to the uuid alone.
# --------------------------------------------------------------------------- #
_IDB_MAX_BLOB = 64 * 1024 * 1024  # don't slurp giant attachment blobs hunting for a record


def _idb_string_after(data: bytes, start: int, key: str, window: int = 4000):
    """The V8-framed string value following the first `key` at/after `start`, or None.

    V8 ValueSerializer frames a string as <tag><varint byte-length><bytes>, the tag
    choosing the encoding: 0x22 (") one-byte/Latin-1, 'c' two-byte/UTF-16LE, 'S' UTF-8.
    (`key` is itself a one-byte string, matched by its 0x22 + length prefix.)
    """
    kb = key.encode()
    i = data.find(b'"' + bytes([len(kb)]) + kb, start)
    if i < 0 or i - start > window:
        return None
    j = i + 2 + len(kb)  # past 0x22, the key-length byte, and the key itself
    tag = data[j : j + 1]
    try:
        length, k = _uvarint(data, j + 1)  # value byte-length is a varint, not a single byte
        raw = data[k : k + length]
    except Exception:
        return None
    if tag == b'"':  # kOneByteString: Latin-1, one byte per char
        return raw.decode("latin-1", "replace")
    if tag == b"c":  # kTwoByteString: UTF-16LE (non-ASCII names land here)
        return raw.decode("utf-16-le", "replace")
    if tag == b"S":  # kUtf8String
        return raw.decode("utf-8", "replace")
    return None


def _scan_idb_accounts(tenant: Tenant) -> dict:
    """uuid -> (email, full_name) for any account login record in this tenant's
    claude.ai IndexedDB. Returns {} when nothing is present or readable."""
    out = {}
    idb = tenant.base / "IndexedDB"
    try:
        blobs = list(idb.glob("https_claude.ai_*.indexeddb.blob/**/*"))
    except Exception:
        return out
    for f in blobs:
        try:
            if not f.is_file() or f.stat().st_size > _IDB_MAX_BLOB:
                continue
            d = f.read_bytes()
            if b"email_address" not in d:
                continue
            # Bound each account's email/name search to the bytes BEFORE the next uuid
            # record, so one account never borrows the next record's email_address.
            matches = list(re.finditer(rb'"\x04uuid"\$([0-9a-fA-F-]{36})', d))
            for idx, m in enumerate(matches):
                u = m.group(1).decode()
                if u in out:
                    continue
                stop = matches[idx + 1].start() if idx + 1 < len(matches) else len(d)
                seg = d[m.end() : stop]
                email = _idb_string_after(seg, 0, "email_address")
                if email:
                    out[u] = (email, _idb_string_after(seg, 0, "full_name"))
        except Exception:
            continue  # best-effort: a malformed/foreign blob just yields no email, never a crash
    return out


def account_identities():
    """(uuid -> (email, full_name), {(tenant_base, uuid)} signed-in per tenant).

    Account uuids are global, so the union across every tenant's IndexedDB maps as
    many account partitions to an email as the app has on disk; the second set
    marks which account each tenant is *currently* signed into.
    """
    emails, current = {}, set()
    for t in discover_tenants():
        for u, ident in _scan_idb_accounts(t).items():
            current.add((str(t.base), u))
            emails.setdefault(u, ident)
    return emails, current


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


def _running_claude_desktops() -> list:
    """Data-dir (tenant base) of every running Claude *desktop* app.

    The desktop main process is launched as
        /Applications/Claude.app/Contents/MacOS/Claude --user-data-dir=<BASE>
    and <BASE> is exactly a tenant's base dir. A desktop process started without
    --user-data-dir falls back to Electron's default, the bare "Claude" base.
    The match is case-sensitive on ".../MacOS/Claude", so the lowercase claude
    CLI (.../claude.app/Contents/MacOS/claude) and helper processes are excluded.
    Returns [] when none run or the process list can't be read.
    """
    marker = "Claude.app/Contents/MacOS/Claude"  # capital C: the desktop main only
    dirs = []
    try:
        # -ww: don't truncate to terminal width, so --user-data-dir is never cut off.
        # errors="replace": `ps -A` dumps EVERY process's argv; a stray non-UTF-8 byte
        # in some unrelated command line must not raise (that would fail the guard OPEN).
        out = subprocess.run(
            ["ps", "-ww", "-Axo", "command="], capture_output=True, text=True, errors="replace"
        )
    except Exception:
        return dirs
    for line in out.stdout.splitlines():
        # Match the EXECUTABLE (argv[0]), not any argument that merely contains the
        # path — otherwise a `grep`/editor touching that path would look like the app.
        exe = line.strip().split(" ", 1)[0]
        if not exe.endswith(marker):
            continue
        # The value runs to the next " --flag" or the end of the line, so a path
        # with spaces ("Application Support") survives intact. No flag ⇒ default.
        m = re.search(r"--user-data-dir=(.+?)(?= --|\s*$)", line)
        dirs.append(Path(m.group(1)) if m else DEFAULT_BASE)
    return dirs


def _same_dir(a, b) -> bool:
    try:
        return os.path.realpath(a) == os.path.realpath(b)
    except Exception:
        return str(a) == str(b)


def claude_running(tenants=None) -> bool:
    """Is a Claude desktop app running that owns data we're about to touch?

    With no argument, True iff *any* Claude desktop app is running (used for the
    doctor status line). Given a Tenant or an iterable of them, True only iff a
    running desktop app is bound to one of those tenants' data dirs — so an
    unrelated instance on a different --user-data-dir never blocks the write.
    """
    running = _running_claude_desktops()
    if not running:
        return False
    if tenants is None:
        return True
    if isinstance(tenants, Tenant):
        tenants = [tenants]
    bases = [t.base for t in tenants if t is not None]
    return any(_same_dir(d, b) for d in running for b in bases)


def tenants_touching(paths) -> list:
    """The discovered tenants whose base dir is an ancestor of any given path.

    Used to scope the running-app guard for `restore`, whose backup manifest can
    span tenants. Returns [] if no path maps to a known tenant (the caller then
    falls back to the global guard rather than assume the write is safe).
    """
    by_base = {os.path.realpath(t.base): t for t in discover_tenants()}
    hit = {}
    for p in paths:
        rp = os.path.realpath(p)
        for b, t in by_base.items():
            if rp == b or rp.startswith(b + os.sep):
                hit[b] = t
    return list(hit.values())


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
# token accounting
#
# "Tokens spent" = input + output tokens (cache reads/creation excluded — cheap
# and dominated by per-turn context re-reads). We parse each transcript down to
# unique assistant messages, keyed by API message id. Two layers of duplication:
#   * WITHIN a file the desktop log persists each message several times with the
#     same usage → deduped here (last copy wins);
#   * ACROSS files Claude Code replays a message VERBATIM (same id, same usage,
#     same timestamp) into every resumed/forked/compacted transcript → so any
#     aggregate over multiple files MUST dedupe by id globally, or it inflates
#     badly (measured 2.8x on a real store). `usage` does that; per-session `list`
#     numbers are a single file so they're already correct.
# The multi-iteration usage shape carries the real numbers inside `iterations`
# while top-level fields can be 0, so per record we take max(top, sum(iters));
# `<synthetic>` messages don't count. Each message stores its UTC epoch (not a
# pre-bucketed local day), so the day split is computed at query time and never
# goes stale if the machine's timezone changes. Parsing is stat-cached per file
# (append-only ⇒ unchanged size+mtime ⇒ counts still hold).
# --------------------------------------------------------------------------- #
def fmt_tokens(n) -> str:
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1e3:.1f}k"
    if n < 1_000_000_000:
        return f"{n / 1e6:.1f}M"
    return f"{n / 1e9:.2f}B"


def _iso_to_epoch(ts):
    """UTC epoch seconds (int) for an ISO timestamp, or None if absent/unparseable."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return int(dt.timestamp())
    except Exception:
        return None


def _day_from_epoch(epoch) -> str:
    """Local-time calendar day ('YYYY-MM-DD') of a UTC epoch; 'unknown' if none.
    A human's 'per day' means their own timezone, so convert at query time."""
    if epoch is None or epoch < 0:
        return "unknown"
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d")
    except Exception:
        return "unknown"


def _local_day(ts) -> str:
    """Local calendar day of an ISO timestamp (convenience over the two above)."""
    return _day_from_epoch(_iso_to_epoch(ts))


def _transcript_messages(path) -> dict:
    """{ message_id: [epoch, input, output] } for one transcript's assistant turns.

    Deduped by id within this file (copies are identical). epoch is UTC seconds,
    or -1 if the timestamp is missing. Cross-file dedup (resume/fork replays) is
    the caller's responsibility, since a replayed message keeps its original id.
    """

    def g(d, k):
        try:
            return int(d.get(k) or 0)
        except Exception:
            return 0

    msgs = {}
    try:
        fh = open(path, errors="replace")  # transcripts can hold stray bytes; never raise
    except OSError:
        return msgs
    with fh:
        for line in fh:
            if '"usage"' not in line:  # cheap pre-filter: skip title/mode/user lines
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("type") != "assistant":
                continue
            m = o.get("message") or {}
            u = m.get("usage")
            if not isinstance(u, dict) or m.get("model") == "<synthetic>":
                continue
            its = u.get("iterations")
            if isinstance(its, list) and its:
                inp = max(g(u, "input_tokens"), sum(g(i, "input_tokens") for i in its))
                out = max(g(u, "output_tokens"), sum(g(i, "output_tokens") for i in its))
            else:
                inp, out = g(u, "input_tokens"), g(u, "output_tokens")
            mid = m.get("id") or o.get("uuid")
            if not mid:
                continue  # can't dedupe a message with no stable id; real ones always have one
            ep = _iso_to_epoch(o.get("timestamp"))
            msgs[mid] = [ep if ep is not None else -1, inp, out]
    return msgs


def _transcript_cwd(path) -> str:
    """The working dir recorded in a transcript's lines (for orphaned sessions with
    no live metadata, this at least recovers the project). '' if not found."""
    try:
        with open(path, errors="replace") as fh:
            for line in fh:
                if '"cwd"' not in line:
                    continue
                try:
                    cwd = json.loads(line).get("cwd")
                except Exception:
                    continue
                if cwd:
                    return cwd
    except OSError:
        pass
    return ""


def _token_cache_path() -> Path:
    # colocated with backups (honors ALTPACA_BACKUP_DIR, so tests stay isolated)
    return backup_root().parent / "token-cache.json"


def cached_messages(paths, label="") -> dict:
    """{ transcript_path_str: {message_id: [epoch, input, output]} } for each path.

    Stat-keyed on-disk cache; only transcripts whose (size, mtime) changed are
    re-parsed. Shared by `list` (per-session totals) and `usage` (global dedup +
    per-day report), so a transcript is parsed at most once.
    """
    try:
        cache = json.loads(_token_cache_path().read_text())
    except Exception:
        cache = {}
    todo, out, dirty = [], {}, False
    for p in paths:
        try:
            st = os.stat(p)
        except OSError:
            continue
        key = str(p)
        sig = [st.st_size, st.st_mtime_ns]
        ent = cache.get(key)
        if ent and ent.get("sig") == sig and "msgs" in ent:
            out[key] = ent["msgs"]
        else:
            todo.append((key, p, sig))
    # only show a progress bar for a non-trivial batch — a 1-2 file refresh (the
    # common per-run case) shouldn't flash a bar on unrelated commands.
    prog = Progress(len(todo), label=label) if len(todo) >= 8 else None
    for i, (key, p, sig) in enumerate(todo, 1):
        msgs = _transcript_messages(p)
        out[key] = msgs
        cache[key] = {"sig": sig, "msgs": msgs}
        dirty = True
        if prog:
            prog.render(i)
    if prog:
        prog.finish()
    if dirty:
        # Drop entries whose transcript no longer exists (deleted session), so the
        # cache can't grow forever. Existence-based — NOT "absent from this call" —
        # so a live-only `list` never evicts the orphan entries `usage` still needs.
        for k in [k for k in cache if not os.path.exists(k)]:
            del cache[k]
        try:
            cp = _token_cache_path()
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps(cache))
        except Exception:
            pass
    return out


def _count_transcript_tokens(path) -> tuple:
    """(input, output) totals for one transcript (its own messages, in-file deduped)."""
    msgs = _transcript_messages(path)
    return (sum(v[1] for v in msgs.values()), sum(v[2] for v in msgs.values()))


def compute_session_tokens(sessions: list) -> dict:
    """uuid -> (input, output) for each session with a transcript (stat-cached).

    Per-session = that one transcript's own tokens (correct in isolation). NOTE:
    resume/fork chains share replayed messages, so DON'T sum these across sessions
    to get a grand total — use `usage`, which dedupes by message id globally.
    """
    paths = {}  # path str -> [sessions sharing it]
    for s in sessions:
        tp = s.transcript()
        if tp is not None:
            paths.setdefault(str(tp), []).append(s)
    permsg = cached_messages([Path(p) for p in paths], label="counting tokens ")
    out = {}
    for pstr, shared in paths.items():
        d = permsg.get(pstr, {})
        totals = (sum(v[1] for v in d.values()), sum(v[2] for v in d.values()))
        for s in shared:
            out[s.uuid] = totals
    return out


# --------------------------------------------------------------------------- #
# persistent usage ledger
#
# The token-cache above is a *cache* — it is pruned when a transcript is deleted,
# and `usage` recomputes from whatever .jsonl files still exist. The ledger is the
# opposite: a durable, append-only record of every assistant message ever seen,
# keyed by message id → [owner_session_cli, utc_epoch, input, output]. It is
# updated on each `usage` run and NEVER auto-pruned, so a session's per-day usage
# survives even after its transcript (and its desktop session) disappear. Keying by
# message id makes it dedupe-correct across resume/fork replays; storing epoch (not
# a baked local day) keeps the per-day rollup timezone-correct forever.
# --------------------------------------------------------------------------- #
def _usage_ledger_path() -> Path:
    return backup_root().parent / "usage-ledger.json"


def load_usage_ledger() -> dict:
    try:
        d = json.loads(_usage_ledger_path().read_text())
        if isinstance(d, dict) and isinstance(d.get("messages"), dict):
            d.setdefault("sessions", {})
            return d
    except Exception:
        pass
    return {"version": 1, "messages": {}, "sessions": {}}


def save_usage_ledger(ledger) -> bool:
    try:
        p = _usage_ledger_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        # Write to a PER-CALL unique temp, then atomically replace — so a crash
        # mid-write can't corrupt the ledger AND two concurrent writers can't
        # interleave into a shared temp (they'd install garbage). Last replace wins.
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                json.dump(ledger, fh)
            os.replace(tmp, p)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return True
    except Exception as e:
        warn(f"could not save usage ledger: {e}")
        return False


def merge_scan_into_ledger(ledger, all_paths, permsg, sessions, recover_projects=False):
    """Fold the current scan into `ledger` IN MEMORY — no disk write. Returns whether
    anything actually changed (so the caller can skip rewriting an unchanged ledger).

    Each new message is attributed once, to the OLDEST transcript that holds it
    (so a replay is booked to the session that first produced it), and that owner is
    fixed forever after. Identities of currently-live sessions are captured so a
    later deletion keeps the title/project. Nothing is ever removed. `recover_projects`
    (used only on the persist path) reads an orphan owner's transcript once to fill
    its project — skipped on reads so a report never touches extra files needlessly.
    """
    messages, meta = ledger["messages"], ledger["sessions"]
    present_paths = {p.stem: p for p in all_paths}
    present = set(present_paths)
    dirty = False

    def setfields(e, fields):  # assign only changed fields; report if any changed
        nonlocal dirty
        for k, v in fields:
            if e.get(k) != v:
                e[k] = v
                dirty = True

    for s in sessions:  # capture live identity, so a future deletion keeps it
        if not s.cli_id:
            continue
        e = meta.setdefault(s.cli_id, {})
        setfields(e, [("uuid", s.uuid), ("title", s.title),
                      ("project", os.path.basename(s.cwd.rstrip("/")) or s.cwd or ""),
                      ("account", s.account_ref)])

    def earliest(p):
        d = permsg.get(str(p), {})
        eps = [v[0] for v in d.values() if v[0] >= 0]
        return (min(eps) if eps else 1 << 62, str(p))

    for p in sorted(all_paths, key=earliest):  # oldest transcript owns a shared message
        cli = p.stem
        for mid, (ep, i, o) in permsg.get(str(p), {}).items():
            if mid not in messages:
                messages[mid] = [cli, ep, i, o]
                dirty = True

    owners = {v[0] for v in messages.values()}  # only sessions with usage get an entry
    for cli in owners:
        if cli not in meta:
            dirty = True
        e = meta.setdefault(cli, {})
        if recover_projects and not e.get("title") and not e.get("project") and cli in present_paths:
            cwd = _transcript_cwd(present_paths[cli])
            if cwd:
                setfields(e, [("project", os.path.basename(cwd.rstrip("/")) or cwd)])
        setfields(e, [("present", cli in present)])
    return dirty


def update_usage_ledger(all_paths, permsg, sessions) -> bool:
    """Persist the current scan: load, merge, and SAVE the ledger — but only rewrite
    the file when the merge actually changed something. Serialized by an advisory
    lock so two concurrent altpaca runs can't clobber each other.

    Returns True iff the on-disk ledger now reflects this scan (so the caller may
    advance its sync-state marker). Returns False if we skipped on lock contention
    or the save failed — the marker must NOT advance then, or a genuinely-needed
    re-sync would be skipped and usage could be lost before a `drop`.
    """
    lockf = None
    if fcntl is not None:
        try:
            lockf = open(str(_usage_ledger_path()) + ".lock", "w")
            fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            if lockf is not None:
                lockf.close()
            return False  # another run holds the lock; we did NOT persist this scan
    try:
        ledger = load_usage_ledger()
        if merge_scan_into_ledger(ledger, all_paths, permsg, sessions, recover_projects=True):
            return save_usage_ledger(ledger)  # True only if the write actually succeeded
        return True  # nothing changed — the ledger already reflects this scan
    finally:
        if lockf is not None:
            try:
                fcntl.flock(lockf, fcntl.LOCK_UN)
            except Exception:
                pass
            lockf.close()


def _sync_state_path() -> Path:
    return backup_root().parent / "usage-sync-state"


def _sync_sig(transcript_paths) -> str:
    """A cheap fingerprint of everything a sync reads — the transcript files AND the
    session-metadata files (local_*.json), by (path, size, mtime). If it's unchanged
    since the last sync, a re-sync would produce an identical ledger, so it can be
    skipped entirely — no ledger/cache load, no re-parse — keeping every command fast.
    Covering metadata too means a session rename/move is folded in without waiting for
    the transcript to change."""
    paths = [str(x) for x in transcript_paths]
    for t in discover_tenants():
        paths += glob.glob(str(t.sessions_root / "*" / "*" / "local_*.json"))
    h = hashlib.sha256()
    for p in sorted(paths):
        try:
            st = os.stat(p)
        except OSError:
            continue
        h.update(f"{p}\0{st.st_size}\0{st.st_mtime_ns}\0".encode())
    return h.hexdigest()


def sync_usage_ledger():
    """Fold the current transcripts into the persistent ledger. Run once per altpaca
    invocation (from main), so the ledger stays current no matter which command runs
    — a session's usage is captured before it can ever disappear. Best-effort and
    silent: a sync failure (or an unconfigured environment) never breaks the command.

    Fast-path: if no transcript changed since the last sync, skip everything.
    """
    try:
        all_paths = [Path(p) for p in glob.glob(str(projects_dir() / "*" / "*.jsonl"))]
        if not all_paths:
            return
        sig = _sync_sig(all_paths)
        state = _sync_state_path()
        try:
            # skip only if nothing changed AND the ledger is actually there (a deleted
            # ledger with a surviving sidecar must NOT fast-path into reporting empty)
            if state.read_text() == sig and _usage_ledger_path().exists():
                return
        except Exception:
            pass
        sessions = discover() if any(t.sessions_root.exists() for t in discover_tenants()) else []
        # Advance the sync-state marker ONLY when the scan was actually persisted —
        # never after a lock-contention skip or a failed save, or the fast-path would
        # later skip re-capturing these transcripts (and a drop could lose them).
        if update_usage_ledger(all_paths, cached_messages(all_paths, label="syncing usage ledger "), sessions):
            try:
                state.parent.mkdir(parents=True, exist_ok=True)
                state.write_text(sig)
            except Exception:
                pass
    except (Exception, SystemExit):  # incl. discover() die() on an unconfigured env — never abort the command
        pass


def ledger_rollups(ledger) -> tuple:
    """(per_session, per_day) from a ledger.

    per_session: cli -> {day: [input, output, messages]}
    per_day:            {day: [input, output, messages]}
    Days are derived from each message's epoch at call time (timezone-correct).
    """
    per_session, per_day = {}, {}
    for cli, ep, i, o in ledger["messages"].values():
        day = _day_from_epoch(ep if ep is not None and ep >= 0 else None)
        d = per_session.setdefault(cli, {}).setdefault(day, [0, 0, 0])
        d[0] += i
        d[1] += o
        d[2] += 1
        g = per_day.setdefault(day, [0, 0, 0])
        g[0] += i
        g[1] += o
        g[2] += 1
    return per_session, per_day


def print_session_rows(sessions: list, uuid2group: dict = None, tokens: dict = None):
    show_group = uuid2group is not None
    show_tok = tokens is not None
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
        if show_tok:
            io = tokens.get(s.uuid)
            cells.append(f"{(fmt_tokens(io[0] + io[1]) if io else '-'):>8}")
        cells.append(f"{base[:18]:18}")
        cells.append(title)
        print("  " + "  ".join(cells) + miss)


# --------------------------------------------------------------------------- #
# commands
#
# The read commands (accounts/list/groups/doctor) each build a plain data
# structure once and render EITHER JSON (--json) or the human text from it, so
# the two views can never drift.
# --------------------------------------------------------------------------- #
def emit_json(obj):
    """Print a JSON document — the --json rendering for the read commands."""
    print(json.dumps(obj, indent=2, ensure_ascii=False))


_NO_TOKENS = object()  # sentinel: caller didn't ask for a tokens field (vs. asked, got None)


def _session_dict(s: Session, group=None, tokens=_NO_TOKENS) -> dict:
    """A session as JSON-serializable fields (timestamps are epoch-ms ints).

    `tokens`, when supplied, is an (input, output) pair (or None for a session
    with no transcript); it adds a "tokens" object. Omit it and no such key
    appears, so callers that don't count tokens are unaffected.
    """
    d = {
        "uuid": s.uuid,
        "session_id": s.session_id,
        "cli_id": s.cli_id,
        "account": s.account_ref,
        "tenant": s.tenant_name,
        "workspace": s.workspace,
        "cwd": s.cwd,
        "title": s.title,
        "archived": s.archived,
        "created": s.created,
        "last_activity": s.last_activity,
        "group": group,
        "has_transcript": s.transcript() is not None,
    }
    if tokens is not _NO_TOKENS:
        d["tokens"] = (
            {"input": tokens[0], "output": tokens[1], "total": tokens[0] + tokens[1]} if tokens else None
        )
    return d


def cmd_accounts(args):
    sessions = discover()
    groups = by_account(sessions)
    cur = current_account(sessions)
    emails, current_logins = account_identities()
    login_tenants = {tb for tb, _ in current_logins}  # tenants whose IndexedDB named their login
    tenants = discover_tenants()
    accts = all_account_objs()

    rows = []
    for acc in accts:
        ss = groups.get(acc.ref, [])
        if str(acc.tenant.base) in login_tenants:  # this tenant's IndexedDB named its login
            logged_in = (str(acc.tenant.base), acc.uuid) in current_logins
            guess = False
        else:  # this tenant's IndexedDB was unreadable/absent — fall back to the activity guess
            logged_in = False
            guess = cur is not None and acc == cur
        ident = emails.get(acc.uuid)
        newest = max(ss, key=lambda s: s.last_activity, default=None)
        rows.append(
            {
                "ref": acc.ref,
                "tenant": acc.tenant.name,
                "uuid": acc.uuid,
                "email": ident[0] if ident else None,
                "name": ident[1] if ident else None,
                "logged_in": logged_in,
                "current_login_guess": guess,
                "sessions": len(ss),
                "archived": sum(1 for s in ss if s.archived),
                "projects": len({s.cwd for s in ss}),
                "workspaces": len(workspaces_of(acc)),
                "newest": {"last_activity": newest.last_activity, "title": newest.title} if newest else None,
            }
        )

    if getattr(args, "json", False):
        emit_json({"tenants": [{"name": t.name, "base": str(t.base)} for t in tenants], "accounts": rows})
        return

    if not accts:
        print("no account partitions found.")
        return
    print(f"Claude session partitions across {len(tenants)} tenant(s):")
    for t in tenants:
        print(f"  {t.name or '(default)':12}  {t.base}")
    print()
    for a in rows:
        mark = "  <- logged in" if a["logged_in"] else "  <- current login (guess)" if a["current_login_guess"] else ""
        print(f"{a['ref']}{mark}")
        if a["email"]:
            print(f"  email: {a['email']}" + (f"  ({a['name']})" if a["name"] else ""))
        se, ar, pr, ws = a["sessions"], a["archived"], a["projects"], a["workspaces"]
        print(f"  sessions={se}  archived={ar}  projects={pr}  workspaces={ws}")
        if a["newest"]:
            print(f"  newest: {fmt_ts(a['newest']['last_activity'])}  {a['newest']['title'][:54]}")
        print()
    print("Pick source/destination by the account ref ('<uuid>', or '<tenant>/<uuid>'; prefixes ok):")
    print("  altpaca list <account>")
    print("  altpaca move <src> <dst> --all          # dry-run by default")


def cmd_list(args):
    sessions = discover()
    accounts = [resolve_account(args.account)] if args.account else all_account_objs()

    blocks = []  # (account, selected sessions, uuid->group map)
    for acc in accounts:
        ss = [s for s in sessions if s.account_ref == acc.ref]
        if has_positive_selector(args) or args.skip_archived:
            ss = select(ss, args)
        uuid2group, _names = native_groups(acc.tenant)
        blocks.append((acc, ss, uuid2group))

    # input+output tokens per session (stat-cached; only changed transcripts recount).
    # These are per-transcript; resume/fork chains share replayed messages, so we do
    # NOT sum them into an account/grand total here — `altpaca usage` does that with a
    # global message-id dedup. Summing the column would double-count shared history.
    tok = compute_session_tokens([s for _, ss, _ in blocks for s in ss])

    if getattr(args, "json", False):
        emit_json(
            {
                "accounts": [
                    {
                        "ref": acc.ref,
                        "tenant": acc.tenant.name,
                        "uuid": acc.uuid,
                        "sessions": [
                            _session_dict(s, uuid2group.get(s.uuid), tokens=tok.get(s.uuid))
                            for s in sorted(ss, key=lambda x: x.last_activity, reverse=True)
                        ],
                    }
                    for acc, ss, uuid2group in blocks
                ]
            }
        )
        return

    if not accounts:
        print("no accounts found.")
        return
    cur = current_account(sessions)
    total = 0
    for i, (acc, ss, uuid2group) in enumerate(blocks):
        show_group = bool(uuid2group)
        mark = "  <- current login (guess)" if (not args.account and acc == cur) else ""
        if i:
            print()
        print(f"account {acc.ref}  ({len(ss)} session(s)){mark}")
        hdr = [f"{'uuid':8}", f"{'first activity':16}", f"{'last activity':16}", " "]
        if show_group:
            hdr.append(f"{'group':12}")
        hdr.append(f"{'in+out':>8}")
        hdr.append(f"{'project':18}")
        hdr.append("title")
        print("  " + "  ".join(hdr))
        print_session_rows(ss, uuid2group=uuid2group if show_group else None, tokens=tok)
        total += len(ss)
    if not args.account and len(accounts) > 1:
        print(f"\ntotal: {total} session(s) across {len(accounts)} account(s)")
    print("(in+out is per-session; for a deduplicated grand total & per-day usage: altpaca usage)")


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


def regroup(src_tenant, dst_tenant, candidates, *, apply, force, no_backup, yes, header=True, best_effort=False):
    """Re-file `candidates` (sessions now in dst_tenant) into dst_tenant's groups
    by matching each session's source-tenant group NAME, writing the result by
    appending one record to the destination's Local Storage leveldb.

    Returns the number of sessions regrouped (0 on a dry-run). The Claude desktop
    app must be quit — it owns the destination's Local Storage.

    `best_effort` (used when regroup runs automatically as the tail of a move): a
    condition that would otherwise abort instead just warns and skips, so a failed
    regroup never fails the move that already succeeded.
    """

    def stop(msg):  # abort (strict) or warn-and-skip (best-effort)
        if best_effort:
            warn(f"group membership not carried — {msg} (run `altpaca regroup` later to fix)")
            return 0
        die(msg)

    if str(src_tenant.base) == str(dst_tenant.base):
        return stop("regroup needs two different tenants (within-tenant moves keep grouping)")
    dst_ls = dst_tenant.local_storage
    if not dst_ls.exists():
        return stop(f"destination tenant has no Local Storage at {dst_ls}")

    uuid2name = recover_source_group_names(src_tenant.local_storage)
    if not uuid2name:
        return stop("no group assignments are recoverable from the source tenant's Local Storage")

    dst_merged, dst_max = _merge_ls(dst_ls)
    ver = next(
        (_decode_ls_value(v) for uk, (sq, v) in dst_merged.items() if v is not None and uk == b"VERSION"),
        None,
    )
    if ver is not None and ver != "1":
        return stop(
            f"destination Local Storage schema version is {ver!r}, expected '1' — refusing to write "
            "(the layout may have changed)"
        )
    dst_key, dst_store = _dframe_record(dst_merged)
    if dst_store is None or dst_key is None:
        return stop("destination tenant has no group store yet — open the app once on that tenant first")
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
        if best_effort:
            return 0  # nothing recoverable to carry — not an error for an automatic pass
        die("nothing to regroup")

    if not apply:
        print(
            f"\nDRY-RUN — nothing changed. Re-run with --apply to write group membership into "
            f"{dst_tenant.name or 'default'}'s Local Storage. (add --yes to skip the prompt)"
        )
        return 0

    if claude_running(dst_tenant) and not force:
        return stop(
            f"the Claude desktop app for tenant '{dst_tenant.name or 'default'}' is running — quit that "
            "instance first (it owns this Local Storage and can clobber the write), then retry. use "
            "--force to override."
        )

    if not yes:
        if not sys.stdin.isatty():
            die("refusing to apply without confirmation; pass --yes")
        prompt = f"\nProceed to regroup {len(plan)} session(s) in {dst_tenant.name or 'default'}? [y/N] "
        if input(prompt).strip().lower() not in ("y", "yes"):
            die("aborted")
        if claude_running(dst_tenant) and not force:  # re-check: the app may have started during the prompt
            die(
                f"the Claude desktop app for tenant '{dst_tenant.name or 'default'}' started while waiting "
                "— quit it and retry (it can clobber the write)."
            )

    active_log, man_seq = _ldb_active_log(dst_ls)
    if active_log is None:
        logs = sorted(dst_ls.glob("*.log"))
        if not logs:
            return stop("no .log file in the destination's Local Storage; cannot append a write")
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
    regrouping = cross_tenant
    if cross_tenant:
        tlabel = f"{src.tenant.name or 'default'} -> {dst.tenant.name or 'default'}"
        warn(
            f"cross-tenant {verb} ({tlabel}): sidebar group membership will be carried over "
            "automatically — re-filed by group NAME in the destination's Local Storage."
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
                f"then auto-regroup: would re-file up to {len(plan)} moved session(s) "
                f"by group name in {dst.tenant.name or 'default'}."
            )
        print(f"\nDRY-RUN — nothing changed. Re-run with --apply to {verb}. (add --yes to skip the prompt)")
        return

    # Only the tenants this op writes to matter: the destination always, plus the
    # source when a move removes files from it. An unrelated instance on another
    # data dir is irrelevant and must not block the write.
    watch = [dst.tenant] + ([src.tenant] if remove_source else [])
    if claude_running(watch) and not args.force:
        names = " / ".join(sorted({t.name or "default" for t in watch if claude_running(t)}))
        die(
            f"the Claude desktop app for tenant '{names}' is running — quit that instance first "
            "(it can overwrite changes on exit), then retry. use --force to override."
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
            best_effort=True,  # never let a regroup hiccup fail a completed move
        )


def cmd_move(args):
    transfer(args, remove_source=True)


def cmd_copy(args):
    transfer(args, remove_source=False)


def cmd_drop(args):
    """Delete selected sessions from an account.

    Removes each session's local_*.json metadata file, so it disappears from the
    app's history list. Transcripts are account-agnostic and may be shared, so they
    are left in place by default (matching move/copy, which never touch them); pass
    --with-transcript to also delete a transcript — but only when no *surviving*
    session still references it, so a copy in another account is never orphaned.
    Dry-run by default; backs up before deleting (undo with `restore`).
    """
    acc = resolve_account(args.account)
    sessions = discover()
    acc_ss = [s for s in sessions if s.account_ref == acc.ref]

    if has_positive_selector(args):
        chosen = select(acc_ss, args)
    elif args.all:
        chosen = select(acc_ss, args)  # still honors --skip-archived
    else:
        die("refusing to act without a selection — pass --all or a selector (--session/--project/--title)")

    if not chosen:
        die("no matching sessions in that account")

    env_sid = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    plan, skipped = [], []
    for s in chosen:
        if env_sid and (s.cli_id == env_sid or s.uuid == env_sid):
            skipped.append((s, "currently running session"))
            continue
        plan.append(s)

    # transcript deletion (opt-in): only drop a .jsonl when nothing that survives
    # this run still points at it — a copy in another account shares the same
    # transcript and deleting it would orphan that session.
    transcripts, kept_shared = [], set()
    if args.with_transcript:
        drop_paths = {s.path for s in plan}
        surviving = {s.cli_id for s in sessions if s.cli_id and s.path not in drop_paths}
        seen_t = set()
        for s in plan:
            tpath = s.transcript()
            if not tpath:
                continue
            if s.cli_id in surviving:
                kept_shared.add(tpath)  # distinct files, so the "kept" count below is by file
            elif tpath not in seen_t:
                seen_t.add(tpath)
                transcripts.append(tpath)

    print(f"DROP  {acc.short}")
    print(f"{len(plan)} session(s) to delete:")
    print_session_rows(plan)
    if args.with_transcript:
        print(f"\n  + {len(transcripts)} transcript file(s) will also be deleted")
        if kept_shared:
            print(f"  ! {len(kept_shared)} transcript(s) kept — still referenced by a surviving session")
    if skipped:
        print(f"\nskipping {len(skipped)}:")
        for s, why in skipped:
            print(f"  {s.uuid[:8]}  {why}  ({s.title[:40]})")

    if not plan:
        die("nothing to do")

    if not args.apply:
        print("\nDRY-RUN — nothing changed. Re-run with --apply to delete. (add --yes to skip the prompt)")
        return

    if claude_running(acc.tenant) and not args.force:
        die(
            f"the Claude desktop app for tenant '{acc.tenant.name or 'default'}' is running — quit that "
            "instance first (it can overwrite changes on exit), then retry. use --force to override."
        )

    if not args.yes:
        if not sys.stdin.isatty():
            die("refusing to apply without confirmation; pass --yes")
        extra = f" + {len(transcripts)} transcript(s)" if transcripts else ""
        if input(f"\nProceed to DELETE {len(plan)} session(s){extra}? [y/N] ").strip().lower() not in ("y", "yes"):
            die("aborted")

    backup_dir = None
    if not args.no_backup:
        backup_dir = make_backup([s.path for s in plan] + transcripts, [])
        print(f"backup: {backup_dir}")

    done = 0
    for s in plan:
        if s.path.exists():
            s.path.unlink()
        done += 1
    for tp in transcripts:
        try:
            Path(tp).unlink()
        except FileNotFoundError:
            pass

    tnote = f" and {len(transcripts)} transcript(s)" if transcripts else ""
    print(f"\ndeleted {done} session(s){tnote} from {acc.short}.")
    print("restart the Claude desktop app to see them gone.")
    if backup_dir:
        print(f"undo with:  altpaca restore {backup_dir.name}")


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
        # Scope the guard to the tenants the manifest actually writes into; if no
        # path maps to a known tenant, fall back to the global guard (None).
        watch = tenants_touching(created + [Path(o["path"]) for o in originals]) or None
        if claude_running(watch) and not args.force:
            die("quit the Claude desktop app for the affected tenant first, then retry (or use --force).")
        if not args.yes:
            if not sys.stdin.isatty():
                die("refusing to apply without confirmation; pass --yes")
            if input("Proceed to restore? [y/N] ").strip().lower() not in ("y", "yes"):
                die("aborted")
            if claude_running(watch) and not args.force:  # re-check after the prompt (it owns Local Storage)
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
    tenants = discover_tenants()
    accts = all_account_objs()
    if getattr(args, "json", False):
        emit_json(
            {
                "base_dir": str(base_dir()),
                "base_exists": base_dir().exists(),
                "tenants": [
                    {
                        "name": t.name,
                        "base": str(t.base),
                        "sessions_root_exists": t.sessions_root.exists(),
                        "app_running": claude_running(t),
                    }
                    for t in tenants
                ],
                "projects_dir": str(projects_dir()),
                "projects_dir_exists": projects_dir().exists(),
                "backup_root": str(backup_root()),
                "claude_running": claude_running(),
                "env_session_id": os.environ.get("CLAUDE_CODE_SESSION_ID") or None,
                "accounts": [{"ref": a.ref, "workspaces": len(workspaces_of(a))} for a in accts],
            }
        )
        return
    print(f"base dir         : {base_dir()}  ({'ok' if base_dir().exists() else 'MISSING'})")
    print(f"tenants          : {len(tenants)}")
    for t in tenants:
        sr = t.sessions_root
        run = "  [app running — blocks writes to THIS tenant]" if claude_running(t) else ""
        print(f"  {t.name or '(default)':12}  {t.base}  (sessions-root {'ok' if sr.exists() else 'MISSING'}){run}")
    pj = projects_dir()
    print(f"projects dir     : {pj}  ({'ok' if pj.exists() else 'MISSING'})")
    print(f"backup root      : {backup_root()}")
    print(f"Claude running   : {'yes (only quit the tenant you write to)' if claude_running() else 'no'}")
    print(f"env session id   : {os.environ.get('CLAUDE_CODE_SESSION_ID', '(unset)')}")
    print(f"accounts         : {len(accts)}")
    for a in accts:
        print(f"  {a.ref}  workspaces={len(workspaces_of(a))}")


def cmd_groups(args):
    sessions = discover()
    blocks = []  # (tenant, group names, name->members, ungrouped)
    for t in discover_tenants():
        uuid2group, names = native_groups(t)
        if not names:
            continue
        index = {s.uuid: s for s in sessions if str((s.tenant or default_tenant()).base) == str(t.base)}
        members = {n: [] for n in names}
        for uuid, gname in uuid2group.items():
            if uuid in index:
                members.setdefault(gname, []).append(index[uuid])
        ungrouped = [s for s in index.values() if s.uuid not in uuid2group]
        blocks.append((t, names, members, ungrouped))

    def _by_recent(ss):
        return sorted(ss, key=lambda x: x.last_activity, reverse=True)

    if getattr(args, "json", False):
        emit_json(
            {
                "tenants": [
                    {
                        "tenant": t.name,
                        "groups": [
                            {
                                "name": name,
                                "sessions": [_session_dict(s, name) for s in _by_recent(members.get(name, []))],
                            }
                            for name in names
                        ],
                        "ungrouped": [_session_dict(s) for s in _by_recent(ungrouped)],
                    }
                    for t, names, members, ungrouped in blocks
                ]
            }
        )
        return

    any_groups = False
    for t, names, members, ungrouped in blocks:
        if any_groups:
            print()
        any_groups = True
        if t.name:  # header only for named tenants; default stays bare
            print(f"=== tenant {t.name} ===")
        for name in names:
            present = members.get(name, [])
            print(f"{name}  ({len(present)} session(s) present)")
            for s in _by_recent(present):
                base = os.path.basename(s.cwd.rstrip("/")) or s.cwd or "?"
                print(f"  {s.uuid[:8]}  {base[:18]:18}  {s.title[:50]}")
        if ungrouped:
            print(f"\nUngrouped: {len(ungrouped)} session(s) present")
    if not any_groups:
        print("no app groups found (could not read Local Storage, or none defined).")


def cmd_usage(args):
    """Report input+output token usage per day and per session, keeping a PERSISTENT
    ledger current so a session's history survives even after its transcript (and
    desktop session) are deleted.

    Every run scans every transcript in the projects dir — live and orphaned — folds
    any new messages into ~/.altpaca/usage-ledger.json (deduped by message id, never
    pruned) and refreshes the CSV artifacts, then reports. `--by-session` prints a
    per-session breakdown. `drop` also snapshots into the ledger before it deletes.
    """
    proj = projects_dir()
    all_paths = [Path(p) for p in glob.glob(str(proj / "*" / "*.jsonl"))]
    live_cli = set()
    live_sessions = []
    if any(t.sessions_root.exists() for t in discover_tenants()):
        live_sessions = discover()
        live_cli = {s.cli_id for s in live_sessions if s.cli_id}

    permsg = cached_messages(all_paths, label="scanning transcripts ")
    # main() already synced the ledger for this run; just read it back and report.
    ledger = load_usage_ledger()
    per_session, per_day = ledger_rollups(ledger)
    meta = ledger["sessions"]
    messages = ledger["messages"]

    # "still live" = usage present in a transcript that a live session still owns;
    # everything else (orphaned transcript, or transcript purged) is "deleted".
    live_ids = set()
    for p in all_paths:
        if p.stem in live_cli:
            live_ids.update(permsg.get(str(p), {}))
    tin = tout = live_in = live_out = 0
    for mid, (cli, ep, i, o) in messages.items():
        tin += i
        tout += o
        if mid in live_ids:
            live_in += i
            live_out += o
    del_in, del_out = tin - live_in, tout - live_out

    n_present = sum(1 for c in per_session if meta.get(c, {}).get("present"))
    n_gone = len(per_session) - n_present
    order = sorted(per_day, key=lambda day: (day == "unknown", day))

    def sess_row(cli):  # (uuid, account, project, title, present) with fallbacks
        m = meta.get(cli, {})
        return (m.get("uuid", ""), m.get("account", ""), m.get("project", ""), m.get("title", ""), bool(m.get("present")))

    _write_usage_csvs(order, per_day, per_session, meta, sess_row)  # refreshed every run

    if getattr(args, "json", False):
        emit_json(
            {
                "ledger": str(_usage_ledger_path()),
                "sessions": {"with_usage": len(per_session), "transcript_on_disk": n_present, "gone": n_gone},
                "totals": {
                    "input": tin,
                    "output": tout,
                    "total": tin + tout,
                    "in_live_session": {"input": live_in, "output": live_out},
                    "orphaned": {"input": del_in, "output": del_out},
                },
                "days": [
                    {
                        "date": day,
                        "sessions": sum(1 for c in per_session if day in per_session[c]),
                        "messages": per_day[day][2],
                        "input": per_day[day][0],
                        "output": per_day[day][1],
                        "total": per_day[day][0] + per_day[day][1],
                    }
                    for day in order
                ],
            }
        )
        return

    if args.by_session:
        rows = sorted(
            per_session.items(),
            key=lambda kv: sum(d[0] + d[1] for d in kv[1].values()),
            reverse=True,
        )
        print(f"{len(per_session)} session(s) with usage  ({n_present} on disk, {n_gone} gone — retained in ledger)")
        print(f"\n  {'in+out':>8}  {'days':>4}  {'account':8}  {'project':14}  title")
        for cli, days in rows:
            tot = sum(d[0] + d[1] for d in days.values())
            uuid, acct, project, title, present = sess_row(cli)
            tag = "" if present else "  [transcript gone]"
            label = title or f"(cli {cli[:8]})"
            print(f"  {fmt_tokens(tot):>8}  {len(days):>4}  {(acct or '?')[:8]:8}  {project[:14]:14}  {label[:40]}{tag}")
        _print_usage_footer(args)
        return

    print(
        f"sessions with usage: {len(per_session)}  "
        f"({n_present} transcript on disk, {n_gone} gone — retained in ledger)   [deduped by message id]"
    )
    print(
        f"tokens: {fmt_tokens(tin)} in + {fmt_tokens(tout)} out = {fmt_tokens(tin + tout)} total"
        f"   [in a live session {fmt_tokens(live_in + live_out)}, "
        f"in orphaned/deleted {fmt_tokens(del_in + del_out)}]"
    )
    if not order:
        print("\nno assistant token usage recorded yet.")
        _print_usage_footer(args)
        return
    print()
    print(f"  {'date':10}  {'sess':>5}  {'msgs':>6}  {'in':>8}  {'out':>8}  {'total':>9}")
    for day in order:
        a = per_day[day]
        nsess = sum(1 for c in per_session if day in per_session[c])
        print(
            f"  {day:10}  {nsess:>5}  {fmt_tokens(a[2]):>6}  "
            f"{fmt_tokens(a[0]):>8}  {fmt_tokens(a[1]):>8}  {fmt_tokens(a[0] + a[1]):>9}"
        )
    tmsg = sum(a[2] for a in per_day.values())
    print(
        f"  {'TOTAL':10}  {len(per_session):>5}  {fmt_tokens(tmsg):>6}  "
        f"{fmt_tokens(tin):>8}  {fmt_tokens(tout):>8}  {fmt_tokens(tin + tout):>9}"
    )
    print(f"  (TOTAL sess = {len(per_session)} distinct sessions; a session spans multiple days)")
    _print_usage_footer(args)


def _print_usage_footer(args):
    base = backup_root().parent
    print(f"\nsaved per-day CSV     : {base / 'usage-daily.csv'}")
    print(f"saved per-session CSV : {base / 'usage-by-session.csv'}   (per session × day, includes deleted)")
    print(f"ledger updated        : {_usage_ledger_path()}   (persists even after a session is deleted)")


def _write_usage_csvs(order, per_day, per_session, meta, sess_row):
    """Write usage-daily.csv (per day) and usage-by-session.csv (per session × day)."""
    base = backup_root().parent
    daily_csv = base / "usage-daily.csv"
    sess_csv = base / "usage-by-session.csv"
    try:
        base.mkdir(parents=True, exist_ok=True)
        with open(daily_csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "sessions", "messages", "input", "output", "total"])
            for day in order:
                a = per_day[day]
                nsess = sum(1 for c in per_session if day in per_session[c])
                w.writerow([day, nsess, a[2], a[0], a[1], a[0] + a[1]])
        with open(sess_csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["cli_id", "uuid", "account", "project", "title", "present", "date",
                        "input", "output", "messages", "total"])
            for cli in sorted(per_session):
                uuid, acct, project, title, present = sess_row(cli)
                for day in sorted(per_session[cli], key=lambda d: (d == "unknown", d)):
                    a = per_session[cli][day]
                    w.writerow([cli, uuid, acct, project, title, int(present), day,
                                a[0], a[1], a[2], a[0] + a[1]])
    except Exception as e:
        warn(f"could not write usage CSVs: {e}")
    return daily_csv, sess_csv


# --------------------------------------------------------------------------- #
# cli
# --------------------------------------------------------------------------- #
def add_json(sp):
    sp.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of the text output")


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

    sp = sub.add_parser("accounts", help="list account partitions, their signed-in email, and session counts")
    add_json(sp)
    sp.set_defaults(func=cmd_accounts)

    sp = sub.add_parser("list", help="list sessions (all accounts if none given)")
    sp.add_argument("account", nargs="?", help="account ref '<uuid>' or '<tenant>/<uuid>' (prefix ok; omit for all)")
    add_selectors(sp)
    add_json(sp)
    sp.set_defaults(func=cmd_list)

    sp = sub.add_parser(
        "usage",
        help="per-day & per-session token usage; updates a persistent ledger that survives session deletion",
    )
    sp.add_argument(
        "--by-session", action="store_true", help="show a per-session breakdown instead of the per-day table"
    )
    add_json(sp)
    sp.set_defaults(func=cmd_usage)

    sp = sub.add_parser("dump", help="archive a whole account's sessions into one .zip (metadata + transcripts)")
    sp.add_argument("account", help="account ref '<uuid>' or '<tenant>/<uuid>' (prefix ok) — whole account archived")
    sp.add_argument("--out", metavar="PATH", help="output dir, or a .zip path (default ~/.altpaca/dumps)")
    sp.add_argument("-n", "--dry-run", action="store_true", help="show the archive contents without writing")
    sp.set_defaults(func=cmd_dump)

    sp = sub.add_parser("groups", help="list the app's custom groups and their members (read-only)")
    add_json(sp)
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
        sp.set_defaults(func=cmd_move if remove else cmd_copy)

    sp = sub.add_parser("drop", help="delete sessions from an account (removes them from the app)")
    sp.add_argument("account", help="account ref '<uuid>' or '<tenant>/<uuid>' (prefix ok)")
    add_selectors(sp)
    sp.add_argument(
        "--with-transcript",
        action="store_true",
        help="also delete the .jsonl transcript (kept if a surviving session still references it)",
    )
    sp.add_argument("--apply", action="store_true", help="actually delete (default: dry-run)")
    sp.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    sp.add_argument("--no-backup", action="store_true", help="do not back up before deleting")
    sp.add_argument("--force", action="store_true", help="proceed despite warnings")
    sp.set_defaults(func=cmd_drop)

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
    add_json(sp)
    sp.set_defaults(func=cmd_doctor)

    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    sync_usage_ledger()  # keep the persistent usage ledger current on every run
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main() or 0)
