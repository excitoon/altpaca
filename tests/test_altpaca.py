"""Hermetic tests for altpaca.

Each test builds a fake Claude app-support tree under a tmp dir and points
altpaca at it via ALTPACA_* env vars, so nothing touches a real install.
"""

import argparse
import io
import json
import zipfile

import pytest

import altpaca

A = "aaaaaaaa-0000-0000-0000-000000000000"
WA = "wa000000-0000-0000-0000-0000000000aa"
B = "bbbbbbbb-0000-0000-0000-000000000000"
WB = "wb000000-0000-0000-0000-0000000000bb"

SESSIONS = [
    # (desktop uuid, cliSessionId, cwd, title, archived)
    ("11111111-1111-1111-1111-111111111111", "c1111111-1111-1111-1111-111111111111", "/Users/x/life", "Alpha", False),
    (
        "22222222-2222-2222-2222-222222222222",
        "c2222222-2222-2222-2222-222222222222",
        "/Users/x/proj",
        "Beta beta",
        False,
    ),
    ("33333333-3333-3333-3333-333333333333", "c3333333-3333-3333-3333-333333333333", "/Users/x/life", "Gamma", True),
]


def _meta(uuid, cli, cwd, title, archived):
    return {
        "sessionId": f"local_{uuid}",
        "cliSessionId": cli,
        "cwd": cwd,
        "createdAt": 1700000000000,
        "lastActivityAt": 1700000000000,
        "model": "claude-test",
        "isArchived": archived,
        "title": title,
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    base = tmp_path / "Claude"
    ccs = base / "claude-code-sessions"
    projects = tmp_path / "projects"
    backups = tmp_path / "backups"
    (ccs / A / WA).mkdir(parents=True)
    (ccs / B / WB).mkdir(parents=True)  # destination account, no sessions yet
    (projects / "encoded").mkdir(parents=True)
    for uuid, cli, cwd, title, archived in SESSIONS:
        (ccs / A / WA / f"local_{uuid}.json").write_text(json.dumps(_meta(uuid, cli, cwd, title, archived)))
        (projects / "encoded" / f"{cli}.jsonl").write_text("{}\n")  # fake transcript

    monkeypatch.setenv("ALTPACA_CLAUDE_DIR", str(base))
    monkeypatch.setenv("ALTPACA_PROJECTS_DIR", str(projects))
    monkeypatch.setenv("ALTPACA_BACKUP_DIR", str(backups))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setattr(altpaca, "_NATIVE_CACHE", {}, raising=False)
    # stay hermetic: don't let a real running Claude.app trip the apply-guard
    monkeypatch.setattr(altpaca, "claude_running", lambda: False)
    return argparse.Namespace(base=base, ccs=ccs, projects=projects, backups=backups)


def _src_files(env):
    return list((env.ccs / A / WA).glob("local_*.json"))


def _dst_files(env):
    return list((env.ccs / B / WB).glob("local_*.json"))


def test_discover_groups_by_account(env):
    sessions = altpaca.discover()
    assert len(sessions) == 3
    groups = altpaca.by_account(sessions)
    assert set(groups) == {A}  # B has an (empty) workspace but no sessions
    assert altpaca.all_accounts() == sorted([A, B])


def test_uuid_and_transcript(env):
    s = next(x for x in altpaca.discover() if x.title == "Alpha")
    assert s.uuid == "11111111-1111-1111-1111-111111111111"
    assert s.session_id.startswith("local_")
    assert s.transcript() is not None


def test_resolve_account_prefix_and_errors(env):
    acc = altpaca.resolve_account("aaaa")
    assert acc.ref == A
    assert acc.uuid == A
    assert acc.tenant.name == ""  # default tenant -> bare uuid ref
    with pytest.raises(SystemExit):
        altpaca.resolve_account("zzzz")  # no match


def test_select_filters(env):
    ss = [s for s in altpaca.discover() if s.account == A]
    ns = argparse.Namespace(session=None, project="proj", title=None, skip_archived=False)
    assert [s.title for s in altpaca.select(ss, ns)] == ["Beta beta"]
    ns = argparse.Namespace(session=None, project=None, title="alpha", skip_archived=False)
    assert [s.title for s in altpaca.select(ss, ns)] == ["Alpha"]
    ns = argparse.Namespace(session=None, project=None, title=None, skip_archived=True)
    assert sorted(s.title for s in altpaca.select(ss, ns)) == ["Alpha", "Beta beta"]  # Gamma archived


def test_projects_dir_respects_claude_config_dir(monkeypatch, tmp_path):
    monkeypatch.delenv("ALTPACA_PROJECTS_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert altpaca.projects_dir() == tmp_path / "cfg" / "projects"


def test_dry_run_move_does_not_mutate(env, capsys):
    altpaca.main(["move", A[:8], B[:8], "--all"])
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert len(_src_files(env)) == 3
    assert _dst_files(env) == []


def test_apply_move_then_restore_roundtrip(env):
    altpaca.main(["move", A[:8], B[:8], "--all", "--apply", "--yes"])
    assert _src_files(env) == []
    assert len(_dst_files(env)) == 3

    backups = sorted(env.backups.glob("*.zip"))
    assert backups, "a backup archive should have been written"
    altpaca.main(["restore", backups[-1].name, "--apply", "--yes"])
    assert len(_src_files(env)) == 3
    assert _dst_files(env) == []


def test_copy_keeps_source(env):
    altpaca.main(["copy", A[:8], B[:8], "--project", "proj", "--apply", "--yes"])
    assert len(_src_files(env)) == 3  # nothing removed
    assert len(_dst_files(env)) == 1  # only the /proj session copied


def test_move_requires_selection(env):
    with pytest.raises(SystemExit):
        altpaca.main(["move", A[:8], B[:8]])  # no --all / selector


def test_dump_writes_archive(env, tmp_path):
    out = tmp_path / "dumps"
    altpaca.main(["dump", A[:8], "--out", str(out)])  # whole account
    zips = list(out.glob("*.zip"))
    assert len(zips) == 1  # one archive per run
    with zipfile.ZipFile(zips[0]) as zf:
        names = zf.namelist()
        assert len(names) == 3  # one entry per session
        bundle = json.loads(zf.read(names[0]))
        assert bundle["altpaca_dump"] == 1
        assert bundle["metadata"]["cliSessionId"]
        assert bundle["transcript"] is not None


def test_dump_dry_run_writes_nothing(env, tmp_path, capsys):
    out = tmp_path / "dumps2"
    altpaca.main(["dump", A[:8], "--out", str(out), "-n"])
    assert not out.exists() or not list(out.glob("*.zip"))
    assert "dry-run" in capsys.readouterr().out


def test_dump_handles_lone_surrogate(env, tmp_path):
    # corrupt Alpha's transcript with a lone surrogate (half an emoji)
    cli = "c1111111-1111-1111-1111-111111111111"
    (env.projects / "encoded" / f"{cli}.jsonl").write_text('{"text": "\\ud83d"}\n')
    out = tmp_path / "dumps3"
    altpaca.main(["dump", A[:8], "--out", str(out)])  # whole account incl. the corrupt one
    zips = list(out.glob("*.zip"))
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0]) as zf:
        texts = [zf.read(n).decode("utf-8") for n in zf.namelist()]  # no crash = valid UTF-8
    assert len(texts) == 3
    assert all("altpaca_dump" in t for t in texts)


def test_dump_never_overwrites(env, tmp_path):
    out = tmp_path / "d"
    altpaca.main(["dump", A[:8], "--out", str(out)])
    altpaca.main(["dump", A[:8], "--out", str(out)])  # second run
    assert len(list(out.glob("*.zip"))) == 2  # one archive per run, no overwrite


def test_progress_noop_when_not_tty():
    s = io.StringIO()  # isatty() -> False
    p = altpaca.Progress(5, stream=s)
    p.render(2, "x")
    p.finish()
    assert s.getvalue() == ""  # silent when not a terminal


def test_progress_renders_on_tty():
    class _TTY(io.StringIO):
        def isatty(self):
            return True

    s = _TTY()
    p = altpaca.Progress(4, label="go ", stream=s)
    p.render(2, "hello")
    assert "2/4" in s.getvalue()
    assert "go " in s.getvalue()


def test_list_all_accounts(env, capsys):
    altpaca.main(["list"])  # no account -> every account
    out = capsys.readouterr().out
    assert A in out and B in out  # both partitions shown
    assert "total:" in out


def test_list_single_account(env, capsys):
    altpaca.main(["list", A[:8]])
    out = capsys.readouterr().out
    assert A in out and B not in out  # only the requested account
    assert "total:" not in out


def test_snappy_decompress_literal_and_copy():
    assert altpaca._snappy_decompress(bytes([5, 0x10]) + b"hello") == b"hello"
    # literal "abcd" then copy(offset=4, len=4) -> "abcdabcd"
    stream = bytes([8, 0x0C]) + b"abcd" + bytes([0x01, 0x04])
    assert altpaca._snappy_decompress(stream) == b"abcdabcd"


def test_parse_dframe_groups():
    text = json.dumps(
        {
            "state": {
                "customGroups": [{"id": "cg-1", "name": "Work"}, {"id": "cg-2", "name": "Home"}],
                "customGroupAssignments": {
                    "code:local_aaaa": "cg-1",
                    "code:local_bbbb": "cg-2",
                    "code:local_cccc": "cg-missing",  # unknown group id -> dropped
                },
            },
            "version": 1,
        }
    )
    uuid2group, names = altpaca._parse_dframe_groups(text)
    assert names == ["Work", "Home"]
    assert uuid2group == {"aaaa": "Work", "bbbb": "Home"}


def _fake_native(monkeypatch):
    u_alpha = "11111111-1111-1111-1111-111111111111"
    monkeypatch.setattr(altpaca, "native_groups", lambda tenant=None: ({u_alpha: "Work"}, ["Work", "Home"]))
    return u_alpha


def test_native_group_tag_filter_and_move(env, monkeypatch, capsys):
    _fake_native(monkeypatch)
    altpaca.main(["list", A[:8]])
    assert "Work" in capsys.readouterr().out  # shown in the group column

    altpaca.main(["list", A[:8], "--group", "work"])  # case-insensitive
    out = capsys.readouterr().out
    assert "Alpha" in out and "Beta" not in out

    altpaca.main(["move", A[:8], B[:8], "--group", "Work"])
    assert "1 session(s) to move" in capsys.readouterr().out


def test_groups_command(env, monkeypatch, capsys):
    _fake_native(monkeypatch)
    altpaca.main(["groups"])
    out = capsys.readouterr().out
    assert "Work  (1 session(s) present)" in out
    assert "Home  (0 session(s) present)" in out
    assert "Ungrouped: 2 session(s) present" in out


def test_unknown_group_errors(env, monkeypatch):
    monkeypatch.setattr(altpaca, "native_groups", lambda tenant=None: ({}, ["Work"]))
    with pytest.raises(SystemExit):
        altpaca.main(["list", A[:8], "--group", "nope"])


# --------------------------------------------------------------------------- #
# tenants: a sibling "Claude-<suffix>" dir is a second tenant; its accounts are
# addressed "<suffix>/<uuid>" while the default tenant stays bare "<uuid>".
# --------------------------------------------------------------------------- #
@pytest.fixture
def two_tenants(env):
    """Add a 'Claude-excitoon' tenant that shares account uuid A with the default."""
    alt = env.base.parent / "Claude-excitoon"
    accs = alt / "claude-code-sessions"
    wsx = "wx000000-0000-0000-0000-0000000000xx"
    (accs / A / wsx).mkdir(parents=True)
    uuid = "44444444-4444-4444-4444-444444444444"
    cli = "c4444444-4444-4444-4444-444444444444"
    (accs / A / wsx / f"local_{uuid}.json").write_text(json.dumps(_meta(uuid, cli, "/Users/x/alt", "Delta", False)))
    (env.projects / "encoded" / f"{cli}.jsonl").write_text("{}\n")
    return argparse.Namespace(alt=alt, uuid=uuid, ws=wsx)


def test_tenant_discovery_and_refs(two_tenants):
    tenants = altpaca.discover_tenants()
    assert sorted(t.name for t in tenants) == ["", "excitoon"]
    # account uuid A exists in BOTH tenants -> two distinct refs
    assert set(altpaca.all_accounts()) == {A, B, f"excitoon/{A}"}


def test_bare_ref_is_default_tenant(two_tenants):
    acc = altpaca.resolve_account(A[:8])  # bare -> default tenant only
    assert acc.tenant.name == ""
    assert acc.ref == A


def test_tenant_qualified_ref_resolves(two_tenants):
    acc = altpaca.resolve_account(f"excitoon/{A[:8]}")
    assert acc.tenant.name == "excitoon"
    assert acc.ref == f"excitoon/{A}"
    # the session living only in the excitoon tenant is discoverable there
    ss = [s for s in altpaca.discover() if s.account_ref == acc.ref]
    assert [s.title for s in ss] == ["Delta"]


def test_cross_tenant_move_warns_about_groups(two_tenants, capsys):
    altpaca.main(["move", f"excitoon/{A[:8]}", A[:8], "--all"])
    err = capsys.readouterr().err
    assert "cross-tenant" in err and "GROUP membership will NOT carry" in err
    assert "--regroup" in err  # the warning now points at the fix


# --------------------------------------------------------------------------- #
# leveldb WRITE path + regroup. We build a minimal-but-real Chromium Local
# Storage leveldb (CURRENT + MANIFEST + .log) using altpaca's own writer
# primitives, then read it back through altpaca's reader — so a regroup write is
# verified end-to-end through the same code the desktop app's leveldb uses.
# --------------------------------------------------------------------------- #
LS_KEY = b"_https://claude.ai\x00\x01dframe-store"


def test_leveldb_write_primitives_match_spec():
    # CRC32C (Castagnoli) test vectors verified against google/leveldb, NOT IEEE CRC32.
    assert altpaca._crc32c(b"") == 0x00000000
    assert altpaca._crc32c(b"123456789") == 0xE3069283
    assert altpaca._mask_crc(0xE3069283) == 0xC78AB0E5
    # base-128 varint
    assert altpaca._ldb_varint(0) == b"\x00"
    assert altpaca._ldb_varint(128) == b"\x80\x01"
    assert altpaca._ldb_varint(300) == b"\xac\x02"
    # DOM-Storage value framing: 0x01+Latin-1 vs 0x00+UTF-16LE by content
    assert altpaca._encode_ls_value('"') == b"\x01\x22"
    assert altpaca._encode_ls_value("да") == b"\x00" + "да".encode("utf-16-le")


def test_write_batch_frame_roundtrips_through_reader():
    payload = altpaca._write_batch(4242, LS_KEY, altpaca._encode_ls_value('{"x":1}'))
    framed = altpaca._frame_log_append(payload, 0)  # append into an empty file
    recs = altpaca._log_record_payloads(framed)
    assert len(recs) == 1 and recs[0] == payload


def test_frame_append_spans_block_boundary():
    # append a >1-block payload AFTER a real first record: it must fragment
    # (FIRST/MIDDLE/LAST) across 32 KiB boundaries and still reassemble exactly.
    p1 = altpaca._write_batch(1, LS_KEY, altpaca._encode_ls_value("x"))
    framed1 = altpaca._frame_log_append(p1, 0)
    p2 = altpaca._write_batch(7, LS_KEY, altpaca._encode_ls_value("v" * 40000))
    framed2 = altpaca._frame_log_append(p2, len(framed1))  # block_offset seeded from file size
    assert altpaca._log_record_payloads(framed1 + framed2) == [p1, p2]


def test_frame_append_pads_sub_header_block_tail():
    # with fewer than 7 bytes left in the block, the tail is zero-padded first
    framed = altpaca._frame_log_append(altpaca._write_batch(1, b"k", b"\x01v"), 32768 - 3)
    assert framed[:3] == b"\x00\x00\x00"


def test_frame_append_empty_first_at_seven_byte_tail_roundtrips():
    # exactly 7 bytes left -> leveldb emits a 0-byte FIRST record then spills into
    # the next block. Appending after a real record at that offset must still read back.
    p1 = altpaca._write_batch(1, LS_KEY, altpaca._encode_ls_value("x"))
    framed1 = altpaca._frame_log_append(p1, 0)
    # pick a payload size so the file lands exactly 7 bytes from a block boundary,
    # then append a second record there.
    pad_target = 32768 - 7 - len(framed1)
    p2 = altpaca._write_batch(2, b"k", b"\x01" + b"y" * pad_target)
    framed2 = altpaca._frame_log_append(p2, len(framed1))
    assert len(framed1 + framed2) > 32768  # genuinely crossed a block
    p3 = altpaca._write_batch(3, LS_KEY, altpaca._encode_ls_value("z"))
    framed3 = altpaca._frame_log_append(p3, len(framed1 + framed2))
    assert altpaca._log_record_payloads(framed1 + framed2 + framed3) == [p1, p2, p3]


def _version_edit(log_number, last_seq):
    return bytes([2]) + altpaca._ldb_varint(log_number) + bytes([4]) + altpaca._ldb_varint(last_seq)


def test_version_edit_parse_roundtrip():
    edit = altpaca._parse_version_edit(_version_edit(7, 1234))
    assert edit[2] == 7 and edit[4] == 1234


def _build_ls(base, store, *, seq, log_number=5, last_seq=None):
    """Write a minimal valid leveldb for `store` under <base>/Local Storage/leveldb."""
    last_seq = seq if last_seq is None else last_seq
    ls = base / "Local Storage" / "leveldb"
    ls.mkdir(parents=True, exist_ok=True)
    # the dframe-store value, plus a VERSION='1' row so the schema guard is happy
    text = json.dumps(store, separators=(",", ":"))
    batch = (
        altpaca._write_batch(seq, LS_KEY, altpaca._encode_ls_value(text))
        + b""  # (one record; VERSION written as its own record below)
    )
    log_bytes = altpaca._frame_log_append(batch, 0)
    ver_batch = altpaca._write_batch(seq - 1, b"VERSION", altpaca._encode_ls_value("1"))
    log_bytes = log_bytes + altpaca._frame_log_append(ver_batch, len(log_bytes))
    (ls / f"{log_number:06d}.log").write_bytes(log_bytes)
    (ls / "CURRENT").write_text("MANIFEST-000001\n")
    (ls / "MANIFEST-000001").write_bytes(altpaca._frame_log_append(_version_edit(log_number, last_seq), 0))
    return ls


def _read_store(ls):
    merged, _ = altpaca._merge_ls(ls)
    _key, store = altpaca._dframe_record(merged)
    return altpaca._store_state(store)


WORK = "cg-work0000-0000-0000-0000-000000000000"
HOME = "cg-home0000-0000-0000-0000-000000000000"
WORK_DST = "cg-workdst0-0000-0000-0000-000000000000"


def _src_store():
    # source tenant: Alpha(11111111) in Work, Beta(22222222) in Home
    return {
        "state": {
            "customGroups": [{"id": WORK, "name": "Work"}, {"id": HOME, "name": "Home"}],
            "customGroupAssignments": {
                "code:local_11111111-1111-1111-1111-111111111111": WORK,
                "code:local_22222222-2222-2222-2222-222222222222": HOME,
            },
            "customGroupOrder": [WORK, HOME],
        },
        "version": 0,
    }


def _dst_store(assignments=None):
    # destination tenant already has a "Work" group (different id), no "Home"
    return {
        "state": {
            "customGroups": [{"id": WORK_DST, "name": "Work"}],
            "customGroupAssignments": assignments or {},
            "customGroupOrder": [WORK_DST],
        },
        "version": 0,
    }


@pytest.fixture
def regroup_env(env):
    """Default tenant = source (has groups). excitoon tenant = destination.
    Move Alpha+Beta into excitoon so they physically live there, ungrouped."""
    alt = env.base.parent / "Claude-excitoon"
    (alt / "claude-code-sessions" / B / WB).mkdir(parents=True)
    _build_ls(env.base, _src_store(), seq=100, log_number=3)
    _build_ls(alt, _dst_store(), seq=50, log_number=7)
    # physically relocate Alpha + Beta into the excitoon tenant (as a move would)
    import shutil as _sh

    for uuid in ("11111111-1111-1111-1111-111111111111", "22222222-2222-2222-2222-222222222222"):
        srcf = env.ccs / A / WA / f"local_{uuid}.json"
        _sh.copy(srcf, alt / "claude-code-sessions" / B / WB / f"local_{uuid}.json")
        srcf.unlink()
    return argparse.Namespace(alt=alt, dst_ls=alt / "Local Storage" / "leveldb")


def test_recover_source_group_names(regroup_env, env):
    got = altpaca.recover_source_group_names(env.base / "Local Storage" / "leveldb")
    assert got["11111111-1111-1111-1111-111111111111"] == "Work"
    assert got["22222222-2222-2222-2222-222222222222"] == "Home"


def test_ldb_active_log_uses_manifest(regroup_env):
    log_path, last_seq = altpaca._ldb_active_log(regroup_env.dst_ls)
    assert log_path.name == "000007.log"  # the manifest's log_number, not "highest by chance"
    assert last_seq == 50


def test_regroup_dry_run_writes_nothing(regroup_env, capsys):
    before = (regroup_env.dst_ls / "000007.log").read_bytes()
    altpaca.main(["regroup", A[:8], f"excitoon/{B[:8]}"])  # no --apply
    out = capsys.readouterr().out
    assert "DRY-RUN" in out
    assert (regroup_env.dst_ls / "000007.log").read_bytes() == before  # untouched
    assert "11111111" not in _read_store(regroup_env.dst_ls).get("customGroupAssignments", {})


def test_regroup_apply_assigns_by_name_and_creates_missing(regroup_env):
    altpaca.main(["regroup", A[:8], f"excitoon/{B[:8]}", "--apply", "--yes"])
    st = _read_store(regroup_env.dst_ls)
    asg = st["customGroupAssignments"]
    name_by_id = {g["id"]: g["name"] for g in st["customGroups"]}
    # Alpha -> the destination's existing "Work" id (matched by NAME, not source id)
    a_id = asg["code:local_11111111-1111-1111-1111-111111111111"]
    assert name_by_id[a_id] == "Work" and a_id == WORK_DST
    # Beta -> a freshly minted "Home" group (absent in destination before)
    b_id = asg["code:local_22222222-2222-2222-2222-222222222222"]
    assert name_by_id[b_id] == "Home" and b_id != HOME
    assert b_id in st["customGroupOrder"]  # new group ordered in the sidebar


def test_regroup_skips_already_grouped_unless_force(regroup_env):
    # pre-assign Alpha to the destination's Work; regroup must leave it alone...
    pre = {"code:local_11111111-1111-1111-1111-111111111111": WORK_DST}
    _build_ls(regroup_env.alt, _dst_store(pre), seq=50, log_number=7)
    altpaca.main(["regroup", A[:8], f"excitoon/{B[:8]}", "--apply", "--yes"])
    st = _read_store(regroup_env.dst_ls)
    assert st["customGroupAssignments"]["code:local_11111111-1111-1111-1111-111111111111"] == WORK_DST
    # Beta still gets grouped (it was unassigned)
    assert "code:local_22222222-2222-2222-2222-222222222222" in st["customGroupAssignments"]


def test_regroup_backup_then_restore_reverts_write(regroup_env, env):
    akey = "code:local_11111111-1111-1111-1111-111111111111"
    altpaca.main(["regroup", A[:8], f"excitoon/{B[:8]}", "--apply", "--yes"])
    assert akey in _read_store(regroup_env.dst_ls)["customGroupAssignments"]
    backups = sorted(env.backups.glob("*.zip"))
    assert backups, "regroup should have written a backup"
    altpaca.main(["restore", backups[-1].name, "--apply", "--yes"])
    assert akey not in _read_store(regroup_env.dst_ls)["customGroupAssignments"]


def test_regroup_creates_order_when_missing(regroup_env):
    # destination store lacking customGroupOrder entirely: a minted group must
    # still be ordered (regression for the setdefault fix).
    store = {"state": {"customGroups": [{"id": WORK_DST, "name": "Work"}], "customGroupAssignments": {}}, "version": 0}
    _build_ls(regroup_env.alt, store, seq=50, log_number=7)
    altpaca.main(["regroup", A[:8], f"excitoon/{B[:8]}", "--apply", "--yes"])
    st = _read_store(regroup_env.dst_ls)
    home_id = st["customGroupAssignments"]["code:local_22222222-2222-2222-2222-222222222222"]
    assert home_id in st["customGroupOrder"]  # created list, new group ordered


def test_regroup_refuses_same_tenant(regroup_env):
    with pytest.raises(SystemExit):
        altpaca.main(["regroup", A[:8], B[:8], "--apply", "--yes"])  # both in default tenant


def test_regroup_refuses_bad_schema_version(regroup_env):
    ls = regroup_env.dst_ls
    # poison the VERSION row with an unexpected value
    bad = altpaca._write_batch(999, b"VERSION", altpaca._encode_ls_value("9"))
    altpaca._append_log_record(ls / "000007.log", bad)
    with pytest.raises(SystemExit):
        altpaca.main(["regroup", A[:8], f"excitoon/{B[:8]}", "--apply", "--yes"])


def test_move_with_regroup_end_to_end(env, capsys):
    """A fresh cross-tenant move --regroup files the moved sessions in one shot."""
    alt = env.base.parent / "Claude-excitoon"
    (alt / "claude-code-sessions" / B / WB).mkdir(parents=True)
    _build_ls(env.base, _src_store(), seq=100, log_number=3)
    _build_ls(alt, _dst_store(), seq=50, log_number=7)
    dst_ls = alt / "Local Storage" / "leveldb"

    altpaca.main(["move", A[:8], f"excitoon/{B[:8]}", "--all", "--regroup", "--apply", "--yes", "--force"])
    # the two grouped sessions are now filed in the destination store
    asg = _read_store(dst_ls)["customGroupAssignments"]
    assert "code:local_11111111-1111-1111-1111-111111111111" in asg
    assert "code:local_22222222-2222-2222-2222-222222222222" in asg
