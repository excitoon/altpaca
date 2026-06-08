"""Hermetic tests for altpaca.

Each test builds a fake Claude app-support tree under a tmp dir and points
altpaca at it via ALTPACA_* env vars, so nothing touches a real install.
"""

import argparse
import json

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
