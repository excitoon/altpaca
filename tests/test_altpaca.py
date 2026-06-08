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
    monkeypatch.setattr(altpaca, "_NATIVE_CACHE", None, raising=False)
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
    assert altpaca.resolve_account("aaaa") == A
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

    backups = sorted(p for p in env.backups.iterdir() if p.is_dir())
    assert backups, "a backup should have been written"
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
    altpaca.main(["dump", A[:8], "--all", "--out", str(out)])
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
    altpaca.main(["dump", A[:8], "--project", "proj", "--out", str(out), "-n"])
    assert not out.exists() or not list(out.glob("*.zip"))
    assert "dry-run" in capsys.readouterr().out


def test_dump_handles_lone_surrogate(env, tmp_path):
    # corrupt Alpha's transcript with a lone surrogate (half an emoji)
    cli = "c1111111-1111-1111-1111-111111111111"
    (env.projects / "encoded" / f"{cli}.jsonl").write_text('{"text": "\\ud83d"}\n')
    out = tmp_path / "dumps3"
    altpaca.main(["dump", A[:8], "--session", "11111111", "--out", str(out)])
    zips = list(out.glob("*.zip"))
    assert len(zips) == 1
    with zipfile.ZipFile(zips[0]) as zf:
        data = zf.read(zf.namelist()[0])
    assert "altpaca_dump" in data.decode("utf-8")  # valid UTF-8, no crash


def test_dump_never_overwrites(env, tmp_path):
    out = tmp_path / "d"
    altpaca.main(["dump", A[:8], "--all", "--out", str(out)])
    altpaca.main(["dump", A[:8], "--all", "--out", str(out)])  # second run
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
    monkeypatch.setattr(altpaca, "native_groups", lambda: ({u_alpha: "Work"}, ["Work", "Home"]))
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
    monkeypatch.setattr(altpaca, "native_groups", lambda: ({}, ["Work"]))
    with pytest.raises(SystemExit):
        altpaca.main(["list", A[:8], "--group", "nope"])
