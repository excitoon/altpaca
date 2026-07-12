"""Hermetic tests for altpaca.

Each test builds a fake Claude app-support tree under a tmp dir and points
altpaca at it via ALTPACA_* env vars, so nothing touches a real install.
"""

import argparse
import io
import json
import re
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
    # (accept the optional tenant arg the scoped guard now passes)
    monkeypatch.setattr(altpaca, "claude_running", lambda *a, **k: False)
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


# --------------------------------------------------------------------------- #
# drop: delete sessions from an account. Metadata-only by default; --with-transcript
# also removes the .jsonl, but never one a surviving session still references.
# --------------------------------------------------------------------------- #
def _transcript(env, cli):
    return env.projects / "encoded" / f"{cli}.jsonl"


def test_dry_run_drop_does_not_mutate(env, capsys):
    altpaca.main(["drop", A[:8], "--all"])
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and "DROP" in out
    assert len(_src_files(env)) == 3  # nothing deleted


def test_drop_requires_selection(env):
    with pytest.raises(SystemExit):
        altpaca.main(["drop", A[:8]])  # no --all / selector


def test_apply_drop_removes_metadata_keeps_transcript(env):
    cli = "c2222222-2222-2222-2222-222222222222"  # Beta
    altpaca.main(["drop", A[:8], "--all", "--apply", "--yes"])
    assert _src_files(env) == []  # all metadata removed
    assert _transcript(env, cli).exists()  # transcript untouched by default
    assert sorted(env.backups.glob("*.zip")), "a backup should have been written"


def test_drop_selective_leaves_others(env):
    altpaca.main(["drop", A[:8], "--project", "proj", "--apply", "--yes"])
    assert len(_src_files(env)) == 2  # only the /proj session (Beta) removed


def test_drop_then_restore_roundtrip(env):
    altpaca.main(["drop", A[:8], "--all", "--apply", "--yes"])
    assert _src_files(env) == []
    backups = sorted(env.backups.glob("*.zip"))
    altpaca.main(["restore", backups[-1].name, "--apply", "--yes"])
    assert len(_src_files(env)) == 3  # every dropped session is back


def test_drop_with_transcript_deletes_unreferenced(env):
    cli = "c2222222-2222-2222-2222-222222222222"  # Beta, referenced only in A
    assert _transcript(env, cli).exists()
    altpaca.main(["drop", A[:8], "--title", "beta", "--with-transcript", "--apply", "--yes"])
    assert not _transcript(env, cli).exists()  # its only session is gone -> deleted


def test_drop_with_transcript_keeps_shared(env):
    # copy Alpha to B first, so its transcript is referenced by two sessions
    altpaca.main(["copy", A[:8], B[:8], "--title", "alpha", "--apply", "--yes"])
    cli = "c1111111-1111-1111-1111-111111111111"
    altpaca.main(["drop", A[:8], "--title", "alpha", "--with-transcript", "--apply", "--yes"])
    assert _transcript(env, cli).exists()  # kept: B's copy still references it
    assert len(_dst_files(env)) == 1  # the copy survives


def test_drop_with_transcript_restore_roundtrip(env):
    cli = "c2222222-2222-2222-2222-222222222222"
    altpaca.main(["drop", A[:8], "--title", "beta", "--with-transcript", "--apply", "--yes"])
    assert not _transcript(env, cli).exists() and len(_src_files(env)) == 2
    backups = sorted(env.backups.glob("*.zip"))
    altpaca.main(["restore", backups[-1].name, "--apply", "--yes"])
    assert _transcript(env, cli).exists()  # transcript restored from backup
    assert len(_src_files(env)) == 3


def test_drop_shows_skipped_before_dying_when_plan_empties(env, monkeypatch, capsys):
    # the only selected session is the currently-running one -> the plan empties.
    # drop must print the skip reason (like move/copy) before erroring, not die mute.
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "c1111111-1111-1111-1111-111111111111")
    with pytest.raises(SystemExit):
        altpaca.main(["drop", A[:8], "--title", "alpha", "--apply", "--yes"])
    out = capsys.readouterr().out
    assert "DROP" in out and "currently running session" in out
    assert len(_src_files(env)) == 3  # nothing deleted


def test_drop_kept_count_dedupes_by_transcript(env, capsys):
    # two A-account sessions sharing ONE transcript, kept alive by a copy in B:
    # the "kept" line must count the file once, not once per dropped session.
    shared_cli = "cshared0-0000-0000-0000-000000000000"
    (env.projects / "encoded" / f"{shared_cli}.jsonl").write_text("{}\n")
    for u in ("a1a1a1a1-0000-0000-0000-000000000000", "a2a2a2a2-0000-0000-0000-000000000000"):
        (env.ccs / A / WA / f"local_{u}.json").write_text(
            json.dumps(_meta(u, shared_cli, "/Users/x/shared", "S", False))
        )
    bu = "b9b9b9b9-0000-0000-0000-000000000000"  # surviving referrer in account B
    (env.ccs / B / WB / f"local_{bu}.json").write_text(
        json.dumps(_meta(bu, shared_cli, "/Users/x/shared", "S2", False))
    )
    altpaca.main(["drop", A[:8], "--project", "shared", "--with-transcript"])  # dry-run
    out = capsys.readouterr().out
    assert "1 transcript(s) kept" in out and "2 transcript(s) kept" not in out


# --------------------------------------------------------------------------- #
# accounts: the logged-in email is read from each tenant's claude.ai IndexedDB
# (data.account = {uuid, email_address, full_name, ...}), framed as
# 0x22 <len-byte> <utf-8 bytes>. We synthesize a minimal blob in that framing.
# --------------------------------------------------------------------------- #
def _idb_str(s):
    b = s.encode("latin-1")  # V8 one-byte string: 0x22 + varint byte-length + Latin-1 bytes
    return b'"' + altpaca._ldb_varint(len(b)) + b


def _idb_str_2byte(s):
    b = s.encode("utf-16-le")  # V8 two-byte string: 'c' + varint byte-length + UTF-16LE bytes
    return b"c" + altpaca._ldb_varint(len(b)) + b


def _write_idb_account(base, uuid, email, name, name_frame=_idb_str):
    rec = (
        b"\xe5\x04datao"
        + _idb_str("account")
        + b"o"
        + _idb_str("tagged_id")
        + _idb_str("user_test")
        + _idb_str("uuid")
        + _idb_str(uuid)
        + _idb_str("email_address")
        + _idb_str(email)
        + _idb_str("full_name")
        + name_frame(name)  # one-byte by default; pass _idb_str_2byte for non-ASCII
    )
    d = base / "IndexedDB" / "https_claude.ai_0.indexeddb.blob" / "1" / "00"
    d.mkdir(parents=True, exist_ok=True)
    (d / "0001").write_bytes(rec)


def _write_idb_account_2byte(base, uuid, email, name):
    _write_idb_account(base, uuid, email, name, name_frame=_idb_str_2byte)


def test_accounts_parser_extracts_uuid_email_name():
    blob = (
        b"\xe5\x04datao"
        + _idb_str("account")
        + b"o"
        + _idb_str("uuid")
        + _idb_str(A)
        + _idb_str("email_address")
        + _idb_str("alice@example.com")
        + _idb_str("full_name")
        + _idb_str("Alice")
    )
    m = re.search(rb'"\x04uuid"\$([0-9a-fA-F-]{36})', blob)
    assert m.group(1).decode() == A
    assert altpaca._idb_string_after(blob, m.end(), "email_address") == "alice@example.com"
    assert altpaca._idb_string_after(blob, m.end(), "full_name") == "Alice"


def test_accounts_shows_logged_in_email(env, capsys):
    _write_idb_account(env.base, A, "alice@example.com", "Alice")
    altpaca.main(["accounts"])
    out = capsys.readouterr().out
    assert "email: alice@example.com  (Alice)" in out
    # the account whose IndexedDB names it is marked precisely (not the activity guess)
    a_line = next(line for line in out.splitlines() if line.startswith(A))
    assert a_line.endswith("<- logged in")
    assert "current login (guess)" not in out  # IndexedDB present -> precise marker, no guess
    # account B has no IndexedDB record -> no email line, not marked logged in
    assert not any(line.startswith(B) and "logged in" in line for line in out.splitlines())


def test_accounts_without_idb_falls_back_to_guess(env, capsys):
    altpaca.main(["accounts"])  # no IndexedDB written
    out = capsys.readouterr().out
    assert A in out and B in out
    assert "email:" not in out
    assert "current login (guess)" in out  # graceful fallback, no crash


def test_idb_parser_handles_varint_length_over_127():
    # a 200-byte one-byte string -> V8 frames its length as a 2-byte varint (0xc8 0x01).
    # A single-byte length reader would truncate it.
    long_name = "a" * 200
    assert _idb_str(long_name)[1:3] == b"\xc8\x01"  # the boundary is genuinely exercised
    blob = (
        b"\xe5\x04datao"
        + _idb_str("uuid")
        + _idb_str(A)
        + _idb_str("email_address")
        + _idb_str("alice@example.com")
        + _idb_str("full_name")
        + _idb_str(long_name)
    )
    m = re.search(rb'"\x04uuid"\$([0-9a-fA-F-]{36})', blob)
    assert altpaca._idb_string_after(blob, m.end(), "email_address") == "alice@example.com"
    assert altpaca._idb_string_after(blob, m.end(), "full_name") == long_name  # full round-trip


def test_idb_parser_decodes_two_byte_string_name():
    # a non-ASCII (Cyrillic) name is stored by V8 as a TWO-byte string ('c' tag,
    # UTF-16LE), NOT a one-byte string — the parser must decode it, not drop it.
    name = "Владимир Чеботарёв"
    blob = (
        b"\xe5\x04datao"
        + _idb_str("uuid")
        + _idb_str(A)
        + _idb_str("email_address")
        + _idb_str("vova@example.com")
        + _idb_str("full_name")
        + _idb_str_2byte(name)
    )
    m = re.search(rb'"\x04uuid"\$([0-9a-fA-F-]{36})', blob)
    assert altpaca._idb_string_after(blob, m.end(), "email_address") == "vova@example.com"
    assert altpaca._idb_string_after(blob, m.end(), "full_name") == name


def test_idb_scan_does_not_borrow_next_records_email(env):
    # uuid A carries NO own email; a later record carries one. A must NOT inherit it.
    other = "ffffffff-0000-0000-0000-000000000000"
    blob = (
        b"\xe5\x04datao"
        + _idb_str("uuid")
        + _idb_str(A)
        + _idb_str("full_name")
        + _idb_str("NoEmail")
        + _idb_str("uuid")
        + _idb_str(other)
        + _idb_str("email_address")
        + _idb_str("victim@other.com")
    )
    d = env.base / "IndexedDB" / "https_claude.ai_0.indexeddb.blob" / "1" / "00"
    d.mkdir(parents=True, exist_ok=True)
    (d / "0001").write_bytes(blob)
    emails, _current = altpaca.account_identities()
    assert A not in emails  # not poisoned with the next record's email
    assert emails.get(other) == ("victim@other.com", None)


def test_accounts_survives_corrupt_idb_blob(env, capsys):
    # the uuid anchor + 'email_address' token are present but the value framing is a
    # truncated/runaway varint — the parser must yield no email, never crash accounts.
    d = env.base / "IndexedDB" / "https_claude.ai_0.indexeddb.blob" / "1" / "00"
    d.mkdir(parents=True, exist_ok=True)
    (d / "0001").write_bytes(b"\xe5\x04datao" + _idb_str("uuid") + _idb_str(A) + b'"\remail_address"\xff\xff\xff')
    altpaca.main(["accounts"])  # must not raise
    out = capsys.readouterr().out
    assert A in out and "email:" not in out


def test_accounts_email_unions_across_tenants(env):
    # account uuid A lives in the default tenant (no IndexedDB email there) but a
    # sibling tenant is signed into the SAME uuid -> its email fills in for A,
    # while only the sibling is reported as actually signed in.
    alt = env.base.parent / "Claude-excitoon"
    (alt / "claude-code-sessions" / A / "wx00").mkdir(parents=True)
    _write_idb_account(alt, A, "shared@acct.com", "Shared")
    emails, current = altpaca.account_identities()
    assert emails.get(A) == ("shared@acct.com", "Shared")
    assert (str(alt), A) in current and (str(env.base), A) not in current


def test_accounts_partial_readability_keeps_guess_for_unreadable_tenant(env, capsys):
    # default tenant has no IndexedDB; a sibling does. The default tenant's accounts
    # must still fall back to the activity guess, not be silently left unmarked.
    alt = env.base.parent / "Claude-excitoon"
    (alt / "claude-code-sessions" / B / "wx00").mkdir(parents=True)
    _write_idb_account(alt, B, "ex@acct.com", "Ex")
    altpaca.main(["accounts"])
    out = capsys.readouterr().out
    assert "current login (guess)" in out  # default tenant (unreadable) still guessed
    assert "<- logged in" in out  # sibling tenant precisely marked


# --------------------------------------------------------------------------- #
# --json: the read commands emit a machine-readable structure instead of text.
# --------------------------------------------------------------------------- #
def test_accounts_json_structure(env, capsys):
    _write_idb_account(env.base, A, "alice@example.com", "Alice")
    altpaca.main(["accounts", "--json"])
    data = json.loads(capsys.readouterr().out)  # pure JSON, no human text mixed in
    assert {t["name"] for t in data["tenants"]} == {""}
    accts = {a["uuid"]: a for a in data["accounts"]}
    assert accts[A]["email"] == "alice@example.com" and accts[A]["name"] == "Alice"
    assert accts[A]["logged_in"] is True and accts[A]["sessions"] == 3
    assert accts[A]["archived"] == 1  # Gamma
    assert accts[A]["newest"]["title"] in {"Alpha", "Beta beta", "Gamma"}  # populated when sessions exist
    assert accts[B]["email"] is None and accts[B]["logged_in"] is False
    assert accts[B]["newest"] is None  # no sessions -> null, not omitted


def test_list_json_structure(env, capsys):
    altpaca.main(["list", A[:8], "--json"])
    data = json.loads(capsys.readouterr().out)
    assert len(data["accounts"]) == 1
    acc = data["accounts"][0]
    assert acc["ref"] == A
    assert {s["title"] for s in acc["sessions"]} == {"Alpha", "Beta beta", "Gamma"}
    s0 = acc["sessions"][0]
    assert {"uuid", "cli_id", "cwd", "title", "archived", "created", "last_activity", "group", "has_transcript"} <= set(
        s0
    )
    assert all(s["has_transcript"] for s in acc["sessions"])  # fixtures have transcripts
    assert any(s["archived"] for s in acc["sessions"])  # Gamma archived


def test_list_json_respects_selectors(env, capsys):
    altpaca.main(["list", A[:8], "--project", "proj", "--json"])
    data = json.loads(capsys.readouterr().out)
    titles = [s["title"] for s in data["accounts"][0]["sessions"]]
    assert titles == ["Beta beta"]  # only the /proj session


def test_list_json_all_accounts(env, capsys):
    altpaca.main(["list", "--json"])  # no account -> every account (list's primary mode)
    data = json.loads(capsys.readouterr().out)
    byref = {a["ref"]: a for a in data["accounts"]}
    assert set(byref) == {A, B}
    assert byref[B]["sessions"] == []  # empty destination account still emitted, not dropped
    assert {s["title"] for s in byref[A]["sessions"]} == {"Alpha", "Beta beta", "Gamma"}


def test_groups_json_structure(env, monkeypatch, capsys):
    _fake_native(monkeypatch)  # Alpha in "Work"; groups Work, Home
    altpaca.main(["groups", "--json"])
    data = json.loads(capsys.readouterr().out)
    t0 = data["tenants"][0]
    gmap = {g["name"]: g for g in t0["groups"]}
    assert {"Work", "Home"} <= set(gmap)
    assert [s["title"] for s in gmap["Work"]["sessions"]] == ["Alpha"]
    assert gmap["Work"]["sessions"][0]["group"] == "Work"
    assert gmap["Home"]["sessions"] == []
    assert {s["title"] for s in t0["ungrouped"]} == {"Beta beta", "Gamma"}


def test_doctor_json_structure(env, capsys):
    altpaca.main(["doctor", "--json"])
    data = json.loads(capsys.readouterr().out)
    assert data["base_exists"] is True and data["claude_running"] is False
    assert data["env_session_id"] is None
    assert {a["ref"] for a in data["accounts"]} == {A, B}


def test_running_desktops_parses_only_the_executable(monkeypatch):
    """_running_claude_desktops matches argv[0] (not any arg that mentions the app
    path), reads --user-data-dir even past later flags / spaces, treats a flagless
    desktop as the default tenant, and decodes ps output leniently (never fails open
    on a stray non-UTF-8 byte in some unrelated process)."""
    AS = "/tmp/AS"
    stdout = "\n".join(
        [
            f"/Applications/Claude.app/Contents/MacOS/Claude --user-data-dir={AS}/Claude-excitoon",
            "/Applications/Claude.app/Contents/MacOS/Claude",  # default tenant: no flag
            # lowercase CLI + helper: argv[0] doesn't end in ".../MacOS/Claude" → ignored
            f"{AS}/Claude-x/claude-code/2/claude.app/Contents/MacOS/claude --output-format stream-json",
            "/Applications/Claude.app/Contents/Helpers/disclaimer /x/claude --model opus",
            # benign process whose ARGUMENT merely contains the marker string → ignored
            "grep -r Claude.app/Contents/MacOS/Claude /Users/x/life",
            # a replaced byte (what errors='replace' yields) in an unrelated command
            "/usr/bin/weird �� job",
            # flags after --user-data-dir, and a space inside the path
            f"/Applications/Claude.app/Contents/MacOS/Claude --user-data-dir={AS}/App Support/Claude-foo --enable-logging",
        ]
    )
    captured = {}

    def fake_run(argv, **kw):
        captured.update(kw)
        return argparse.Namespace(stdout=stdout, returncode=0)

    monkeypatch.setattr(altpaca.subprocess, "run", fake_run)
    dirs = [str(d) for d in altpaca._running_claude_desktops()]
    assert dirs == [
        f"{AS}/Claude-excitoon",
        str(altpaca.DEFAULT_BASE),
        f"{AS}/App Support/Claude-foo",
    ]
    # ps -A dumps every process's argv; a stray byte must be decoded, not raised
    assert captured.get("errors") == "replace" and captured.get("text") is True


def test_running_guard_ignores_unrelated_and_scopes(monkeypatch):
    """A process that merely mentions the app path never blocks; the guard fires
    only for the tenant whose desktop app is actually running."""
    AS = "/tmp/AS2"
    monkeypatch.setattr(
        altpaca.subprocess,
        "run",
        lambda argv, **kw: argparse.Namespace(
            stdout="grep -r Claude.app/Contents/MacOS/Claude /Users/x/life\n", returncode=0
        ),
    )
    assert altpaca._running_claude_desktops() == []
    assert altpaca.claude_running(altpaca.default_tenant()) is False
    assert altpaca.claude_running() is False

    monkeypatch.setattr(
        altpaca.subprocess,
        "run",
        lambda argv, **kw: argparse.Namespace(
            stdout=f"/Applications/Claude.app/Contents/MacOS/Claude --user-data-dir={AS}/Claude-excitoon\n",
            returncode=0,
        ),
    )
    excitoon = altpaca.Tenant("excitoon", f"{AS}/Claude-excitoon")
    other = altpaca.Tenant("mikhail", f"{AS}/Claude-mikhail")
    assert altpaca.claude_running(excitoon) is True  # its app is up → blocks
    assert altpaca.claude_running(other) is False  # unrelated tenant → clear
    assert altpaca.claude_running([other, excitoon]) is True  # any match blocks
    assert altpaca.claude_running([]) is False  # empty watch list never blocks


def test_count_transcript_tokens_dedup_and_iterations(tmp_path):
    """input+output are summed per DISTINCT assistant message (duplicate copies
    counted once), take max(top-level, sum(iterations)), and skip synthetic /
    non-assistant / malformed lines."""
    lines = [
        # message with iterations (top-level zeroed), persisted 3× with same id → once
        {"type": "assistant", "message": {"id": "msg_X", "model": "claude-opus-4-8", "usage": {
            "input_tokens": 0, "output_tokens": 0,
            "iterations": [{"input_tokens": 2, "output_tokens": 1000}, {"input_tokens": 0, "output_tokens": 500}]}}},
        {"type": "assistant", "message": {"id": "msg_X", "model": "claude-opus-4-8", "usage": {
            "input_tokens": 0, "output_tokens": 0,
            "iterations": [{"input_tokens": 2, "output_tokens": 1000}, {"input_tokens": 0, "output_tokens": 500}]}}},
        {"type": "assistant", "message": {"id": "msg_X", "model": "claude-opus-4-8", "usage": {
            "input_tokens": 0, "output_tokens": 0,
            "iterations": [{"input_tokens": 2, "output_tokens": 1000}, {"input_tokens": 0, "output_tokens": 500}]}}},
        # plain message, no iterations → top-level values
        {"type": "assistant", "message": {"id": "msg_Y", "model": "claude-opus-4-8", "usage": {
            "input_tokens": 10, "output_tokens": 20}}},
        # synthetic → excluded
        {"type": "assistant", "message": {"id": "msg_S", "model": "<synthetic>", "usage": {"output_tokens": 999}}},
        # non-assistant line that still carries "usage" → skipped by the type guard
        {"type": "user", "message": {"usage": {"output_tokens": 777}}},
    ]
    p = tmp_path / "t.jsonl"
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\nnot valid json\n")
    inp, out = altpaca._count_transcript_tokens(p)
    assert (inp, out) == (2 + 10, 1500 + 20)  # X counted once (max iter-sum), Y added, S/user/junk ignored
    assert altpaca.fmt_tokens(1500) == "1.5k" and altpaca.fmt_tokens(2_500_000) == "2.5M"


def test_list_shows_and_caches_tokens(env, capsys, monkeypatch):
    """list surfaces an in+out column (text + JSON) and reuses the stat-keyed cache."""
    cli = SESSIONS[0][1]  # Alpha's cliSessionId
    (env.projects / "encoded" / f"{cli}.jsonl").write_text(
        json.dumps({"type": "assistant", "message": {"id": "m1", "model": "claude-opus-4-8",
                                                      "usage": {"input_tokens": 100, "output_tokens": 400}}}) + "\n"
    )
    altpaca.main(["list", "--json"])
    data = json.loads(capsys.readouterr().out)
    alpha = next(s for a in data["accounts"] for s in a["sessions"] if s["title"] == "Alpha")
    assert alpha["tokens"] == {"input": 100, "output": 400, "total": 500}
    # a session whose transcript has no usage records reads as zero, not missing
    other = next(s for a in data["accounts"] for s in a["sessions"] if s["title"] != "Alpha")
    assert other["tokens"] == {"input": 0, "output": 0, "total": 0}
    assert altpaca._token_cache_path().exists()  # cache was written

    # second run must NOT re-parse the unchanged transcript
    calls = []
    real = altpaca._transcript_messages
    monkeypatch.setattr(altpaca, "_transcript_messages", lambda p: calls.append(p) or real(p))
    altpaca.main(["list"])
    out_text = capsys.readouterr().out
    assert "in+out" in out_text and "500" in out_text
    assert calls == []  # every transcript served from cache


def _asst(mid, ts, i, o):
    return {"type": "assistant", "timestamp": ts,
            "message": {"id": mid, "model": "claude-opus-4-8",
                        "usage": {"input_tokens": i, "output_tokens": o}}}


def test_usage_persists_per_session_after_deletion(env, capsys):
    """`usage` folds usage into a persistent ledger on every run and reports per
    day/session, deduping replayed messages — and the data SURVIVES the transcript's
    deletion (a later run still shows it)."""
    ts1, ts2 = "2026-01-01T10:00:00.000Z", "2026-01-02T10:00:00.000Z"
    d1, d2 = altpaca._local_day(ts1), altpaca._local_day(ts2)  # tz-robust: derive expected day

    orphan = env.projects / "encoded" / "deadbeef-0000-0000-0000-000000000000.jsonl"
    orphan.write_text(
        "\n".join(json.dumps(x) for x in [
            _asst("mA", ts1, 100, 200), _asst("mA", ts1, 100, 200), _asst("mB", ts2, 10, 20)])
        + "\n"
    )
    altpaca.main(["usage", "--json"])  # updates the ledger on every run
    data = json.loads(capsys.readouterr().out)
    assert data["totals"]["total"] == 330  # 110 in + 220 out, mA deduped
    assert data["totals"]["in_live_session"] == {"input": 0, "output": 0}  # not backed by a live session
    assert data["totals"]["orphaned"] == {"input": 110, "output": 220}
    assert data["sessions"]["with_usage"] == 1
    days = {d["date"]: d for d in data["days"]}
    assert days[d1]["input"] == 100 and days[d1]["messages"] == 1 and days[d2]["output"] == 20

    # per-session-per-day CSV artifact (refreshed every run)
    sess_csv = env.backups.parent / "usage-by-session.csv"
    assert sess_csv.exists()
    head, *rows = sess_csv.read_text().splitlines()
    assert head == "cli_id,uuid,account,project,title,present,date,input,output,messages,total"
    assert any(r.endswith(f"1,{d1},100,200,1,300") for r in rows)  # present=1, day1 row

    # DELETE the transcript entirely, re-run: the ledger keeps the history
    orphan.unlink()
    altpaca.main(["usage", "--json"])
    data2 = json.loads(capsys.readouterr().out)
    assert data2["totals"]["total"] == 330  # still recorded though the .jsonl is gone
    days2 = {d["date"]: d for d in data2["days"]}
    assert days2[d1]["input"] == 100 and days2[d2]["output"] == 20
    assert data2["sessions"]["gone"] == 1  # now flagged as transcript-gone, retained


def test_ledger_synced_on_every_command(env, capsys):
    """The persistent ledger is kept current on EVERY altpaca run — not just usage."""
    ledger = altpaca._usage_ledger_path()
    assert not ledger.exists()

    orphan = env.projects / "encoded" / "cafe0000-0000-0000-0000-000000000000.jsonl"
    orphan.write_text(json.dumps(_asst("z1", "2026-01-01T10:00:00.000Z", 7, 9)) + "\n")
    altpaca.main(["accounts"])  # a NON-usage command still syncs the ledger
    capsys.readouterr()
    assert ledger.exists() and "z1" in json.loads(ledger.read_text())["messages"]

    # a newly-active session is folded in on the next run (here: list)
    live_tx = env.projects / "encoded" / f"{SESSIONS[0][1]}.jsonl"
    live_tx.write_text(json.dumps(_asst("z2", "2026-01-03T10:00:00.000Z", 1, 2)) + "\n")
    altpaca.main(["list"])
    capsys.readouterr()
    assert "z2" in json.loads(ledger.read_text())["messages"]


def test_drop_captures_usage_before_deleting(env, capsys):
    """Because every run syncs first, a `drop` records a session's usage in the
    ledger before it removes the session — even if `usage` was never run."""
    ledger = altpaca._usage_ledger_path()
    (env.projects / "encoded" / f"{SESSIONS[0][1]}.jsonl").write_text(
        json.dumps(_asst("z3", "2026-01-05T10:00:00.000Z", 4, 5)) + "\n"
    )
    assert not ledger.exists()
    altpaca.main(["drop", A[:8], "--title", "alpha", "--apply", "--yes"])
    capsys.readouterr()
    assert ledger.exists() and "z3" in json.loads(ledger.read_text())["messages"]


def test_usage_dedupes_forked_sessions(env, capsys):
    """A forked/resumed session replays the parent's messages VERBATIM (same message
    id) into its own transcript. Usage must count each generation ONCE (owned by the
    older parent), never once per transcript it was copied into."""
    ts_old, ts_new = "2026-02-01T10:00:00.000Z", "2026-02-02T10:00:00.000Z"
    # parent transcript: the shared turn, alphabetically/temporally first
    parent = env.projects / "encoded" / "aaaa1111-0000-0000-0000-000000000000.jsonl"
    parent.write_text(json.dumps(_asst("shared", ts_old, 100, 200)) + "\n")
    # fork transcript: replays "shared" (same id) + adds its own new turn
    fork = env.projects / "encoded" / "bbbb2222-0000-0000-0000-000000000000.jsonl"
    fork.write_text("\n".join(json.dumps(x) for x in [
        _asst("shared", ts_old, 100, 200), _asst("forked", ts_new, 5, 9)]) + "\n")

    altpaca.main(["usage", "--json"])
    data = json.loads(capsys.readouterr().out)
    # shared (300) counted ONCE + forked (14) = 314, NOT 300 + 300 + 14
    assert data["totals"]["total"] == 314
    assert data["sessions"]["with_usage"] == 2  # parent owns "shared", fork owns "forked"
    d_old = altpaca._local_day(ts_old)
    days = {d["date"]: d for d in data["days"]}
    assert days[d_old]["total"] == 300 and days[d_old]["messages"] == 1  # shared booked once


def test_failed_save_does_not_advance_sync_state(env, capsys, monkeypatch):
    """If the ledger save fails (or a run is skipped), the sync-state marker must NOT
    advance — the next run re-attempts, so un-persisted usage is never marked done."""
    orphan = env.projects / "encoded" / "beef0000-0000-0000-0000-000000000000.jsonl"
    orphan.write_text(json.dumps(_asst("m1", "2026-01-01T10:00:00.000Z", 5, 7)) + "\n")
    state = env.backups.parent / "usage-sync-state"

    fail = {"on": True}
    real = altpaca.save_usage_ledger
    monkeypatch.setattr(altpaca, "save_usage_ledger", lambda led: False if fail["on"] else real(led))

    altpaca.main(["accounts"])  # save fails -> nothing persisted
    capsys.readouterr()
    assert not state.exists()  # marker NOT advanced

    fail["on"] = False  # save works again
    altpaca.main(["accounts"])
    capsys.readouterr()
    assert state.exists()
    assert "m1" in json.loads(altpaca._usage_ledger_path().read_text())["messages"]  # re-captured


def test_sync_sig_covers_session_metadata(env):
    """A session-metadata change (e.g. a rename) alters the sync fingerprint, so it's
    folded in without waiting for the transcript to change."""
    paths = list(env.projects.glob("*/*.jsonl"))
    s0 = altpaca._sync_sig(paths)
    meta_file = env.ccs / A / WA / f"local_{SESSIONS[0][0]}.json"
    d = json.loads(meta_file.read_text())
    d["title"] = "Renamed Session"
    meta_file.write_text(json.dumps(d))
    assert altpaca._sync_sig(paths) != s0


def test_usage_footer_shown_when_no_usage(env, capsys):
    """Even with zero recorded usage, the run persists and prints the ledger/CSV
    footer — the footer must not be skipped in the empty case."""
    altpaca.main(["usage"])  # env transcripts are empty "{}" → no usage
    assert "ledger updated" in capsys.readouterr().out
    assert altpaca._usage_ledger_path().exists()


def test_cache_prunes_only_deleted_transcripts(env):
    """A dirty write drops cache keys whose .jsonl is gone, but keeps ones that
    still exist even when the current call didn't ask about them (list vs usage)."""
    def usage_line(mid, i, o):
        return json.dumps({"type": "assistant",
                           "message": {"id": mid, "model": "claude-opus-4-8",
                                       "usage": {"input_tokens": i, "output_tokens": o}}}) + "\n"

    live = env.projects / "encoded" / f"{SESSIONS[0][1]}.jsonl"
    ghost = env.projects / "encoded" / "ghost-0000-0000-0000-000000000000.jsonl"
    other = env.projects / "encoded" / f"{SESSIONS[1][1]}.jsonl"
    live.write_text(usage_line("m1", 1, 2))
    ghost.write_text(usage_line("g1", 5, 6))
    altpaca.cached_messages([live, ghost, other])  # all three cached
    cache = json.loads(altpaca._token_cache_path().read_text())
    assert {str(live), str(ghost), str(other)} <= set(cache)

    ghost.unlink()  # session's transcript deleted
    live.write_text(live.read_text() + usage_line("m2", 3, 4))  # grow live → dirty write
    altpaca.cached_messages([live])  # only asks about live
    cache = json.loads(altpaca._token_cache_path().read_text())
    assert str(ghost) not in cache  # pruned: file no longer exists
    assert str(other) in cache  # kept: still exists, though this call didn't ask for it


def test_json_preserves_non_ascii(env, capsys):
    # a two-byte (Cyrillic) name must survive JSON as real UTF-8, not \uXXXX-mangled
    _write_idb_account_2byte(env.base, A, "vova@example.com", "Влади́мир")
    altpaca.main(["accounts", "--json"])
    raw = capsys.readouterr().out
    assert "Влади́мир" in raw  # ensure_ascii=False keeps it human-readable
    assert json.loads(raw)["accounts"][0]["name"] == "Влади́мир"


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
    # a cross-tenant move always auto-carries group membership
    altpaca.main(["move", f"excitoon/{A[:8]}", A[:8], "--all"])
    err = capsys.readouterr().err
    assert "cross-tenant" in err
    assert "carried over automatically" in err


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


def test_move_auto_regroups_cross_tenant(env):
    """A cross-tenant move files the moved sessions automatically (no flag needed)."""
    alt = env.base.parent / "Claude-excitoon"
    (alt / "claude-code-sessions" / B / WB).mkdir(parents=True)
    _build_ls(env.base, _src_store(), seq=100, log_number=3)
    _build_ls(alt, _dst_store(), seq=50, log_number=7)
    dst_ls = alt / "Local Storage" / "leveldb"

    altpaca.main(["move", A[:8], f"excitoon/{B[:8]}", "--all", "--apply", "--yes", "--force"])
    asg = _read_store(dst_ls)["customGroupAssignments"]
    assert "code:local_11111111-1111-1111-1111-111111111111" in asg
    assert "code:local_22222222-2222-2222-2222-222222222222" in asg


def test_move_auto_regroup_best_effort_when_no_dst_store(env, capsys):
    """If the destination has no group store, the move still succeeds and regroup just warns."""
    alt = env.base.parent / "Claude-excitoon"
    (alt / "claude-code-sessions" / B / WB).mkdir(parents=True)
    _build_ls(env.base, _src_store(), seq=100, log_number=3)
    # deliberately NO Local Storage for the excitoon tenant

    altpaca.main(["move", A[:8], f"excitoon/{B[:8]}", "--all", "--apply", "--yes", "--force"])
    moved = alt / "claude-code-sessions" / B / WB / "local_11111111-1111-1111-1111-111111111111.json"
    assert moved.exists()  # the move itself completed
    assert "group membership not carried" in capsys.readouterr().err  # best-effort warning, not a failure
