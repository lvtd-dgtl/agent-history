"""Every Claude Code config profile is indexed, not just ~/.claude.

A second account runs with CLAUDE_CONFIG_DIR=~/.claude-org (or any
~/.claude-<name>), which keeps its own projects/ tree. Those sessions must be
found without any per-machine setting, and search must say which profile a
hit came from.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

from agent_history import config, index, search, store
from agent_history.adapters.claude_code import ClaudeCodeAdapter, profile_of
from conftest import msg, write_transcript


@pytest.fixture
def home(isolated_env, monkeypatch):
    """A fake home directory with profile discovery turned on.

    conftest pins AGENT_HISTORY_PROJECTS for every test; discovery only runs
    without that override, so it is removed here.
    """
    h = isolated_env / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("USERPROFILE", str(h))  # Path.home() on Windows
    monkeypatch.delenv("AGENT_HISTORY_PROJECTS", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    return h


def _session(projects, name, text, session):
    return write_transcript(projects, "-work", name,
                            [msg("user", text, session=session)])


def _by_session(records):
    return {r.session_id: r for r in records}


def test_default_and_org_profiles_are_both_read(home):
    _session(home / ".claude/projects", "a", "question asked under the main account", "main-1")
    _session(home / ".claude-org/projects", "b", "question asked under the org account", "org-1")

    roots = ClaudeCodeAdapter().roots()
    assert roots == [home / ".claude/projects", home / ".claude-org/projects"]
    got = _by_session(ClaudeCodeAdapter().records())
    assert set(got) == {"main-1", "org-1"}
    assert profile_of(got["main-1"].origin) == "claude"
    assert profile_of(got["org-1"].origin) == "claude-org"


def test_any_named_profile_is_found(home):
    _session(home / ".claude-client-x/projects", "c", "a session from a third profile", "x-1")
    assert "x-1" in _by_session(ClaudeCodeAdapter().records())


def test_memory_files_of_every_profile_are_indexed(home):
    memory = home / ".claude-org/projects/-work/memory"
    memory.mkdir(parents=True)
    (memory / "deploy.md").write_text("the org account deploys through the staging pipeline")
    got = [r for r in ClaudeCodeAdapter().records() if r.role == "memory"]
    assert [r.session_id for r in got] == ["memory:deploy"]
    assert profile_of(got[0].origin) == "claude-org"


def test_lookalikes_without_projects_are_ignored(home):
    (home / ".claude.json").write_text("{}")
    (home / ".claude-server-commander").mkdir()
    (home / ".claude/projects").mkdir(parents=True)
    assert config.claude_profile_dirs() == [home / ".claude/projects"]


def test_claude_config_dir_outside_home_is_picked_up(home, isolated_env, monkeypatch):
    elsewhere = isolated_env / "elsewhere/work-account"
    _session(elsewhere / "projects", "w", "a session under a relocated config dir", "w-1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))

    assert config.claude_profile_dirs() == [elsewhere / "projects"]
    got = _by_session(ClaudeCodeAdapter().records())
    assert profile_of(got["w-1"].origin) == "work-account"


def test_claude_config_dir_pointing_at_a_home_profile_is_not_doubled(home, monkeypatch):
    _session(home / ".claude-org/projects", "b", "only one copy of this should be read", "org-1")
    # Same directory spelled two ways: once by the glob, once via the env var.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home / ".claude-org") + os.sep)

    assert config.claude_profile_dirs() == [home / ".claude-org/projects"]
    assert len(list(ClaudeCodeAdapter().records())) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlinked_profile_is_collapsed(home):
    real = home / ".claude-org"
    _session(real / "projects", "b", "a session reachable through two paths", "org-1")
    (home / ".claude-alias").symlink_to(real, target_is_directory=True)

    assert len(config.claude_profile_dirs()) == 1
    assert len(list(ClaudeCodeAdapter().records())) == 1


def test_explicit_projects_override_wins(home, isolated_env, monkeypatch):
    _session(home / ".claude/projects", "a", "must not be read under the override", "main-1")
    _session(home / ".claude-org/projects", "b", "must not be read under the override", "org-1")
    cfg = isolated_env / "cfg"
    _session(cfg / "projects", "c", "must not be read under the override", "cfg-1")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cfg))
    pinned = isolated_env / "pinned"
    _session(pinned, "p", "the only transcript that should be read", "pin-1")
    monkeypatch.setenv("AGENT_HISTORY_PROJECTS", str(pinned))

    assert config.claude_profile_dirs() == [pinned]
    assert set(_by_session(ClaudeCodeAdapter().records())) == {"pin-1"}


def test_extra_dirs_still_add_to_discovery(home, isolated_env, monkeypatch):
    _session(home / ".claude-org/projects", "b", "a session found by discovery", "org-1")
    extra = isolated_env / "extra"
    _session(extra, "e", "a session from an explicitly added root", "extra-1")
    monkeypatch.setenv("AGENT_HISTORY_EXTRA_DIRS", str(extra))

    assert set(_by_session(ClaudeCodeAdapter().records())) == {"org-1", "extra-1"}


def test_windows_profile_with_only_a_second_account_is_found(home, isolated_env, monkeypatch):
    users = isolated_env / "Users"
    (users / "Public").mkdir(parents=True)
    (users / "dana/.claude-org/projects").mkdir(parents=True)
    (users / "dana/.claude/projects").mkdir(parents=True)
    monkeypatch.delenv("AGENT_HISTORY_NO_WSL", raising=False)

    assert config.claude_profile_dirs(_users_dir=users) == [
        users / "dana/.claude/projects",
        users / "dana/.claude-org/projects",
    ]
    # Before this change a profile was only a candidate if it had .claude/.
    (users / "erin/.claude-org/projects").mkdir(parents=True)
    assert config.wsl_windows_homes(_users_dir=users, marker=".claude*/projects") == []


@pytest.mark.parametrize("origin, expected", [
    ("/home/u/.claude/projects/-home-u/abc.jsonl", "claude"),
    ("/home/u/.claude-org/projects/-home-u/abc/subagents/agent-1.jsonl", "claude-org"),
    ("/home/u/projects/.claude-org/projects/-home-u-projects/abc.jsonl", "claude-org"),
    ("/mnt/c/Users/Mia/.claude-org/projects/C--Users-Mia/abc.jsonl", "claude-org"),
    ("/srv/work-account/projects/-w/abc.jsonl", "work-account"),
    ("/tmp/custom-root/-w/abc.jsonl", None),
    ("", None),
])
def test_profile_is_read_from_the_origin_path(origin, expected):
    assert profile_of(origin) == expected


@pytest.mark.skipif(not store.extensions_supported(),
                    reason="this Python cannot load SQLite extensions")
def test_search_tells_the_accounts_apart(home, fake_embed, capsys):
    _session(home / ".claude/projects", "a", "rotating the staging database password", "main-1")
    _session(home / ".claude-org/projects", "b", "rotating the staging database password today", "org-1")
    assert index.run() == 0
    capsys.readouterr()

    rows = search.search_rows("staging database password", k=5, mode="text")
    assert {r["session_id"]: r["profile"] for r in rows} == {
        "main-1": "claude", "org-1": "claude-org"}

    search.run("staging database password", k=5, mode="text", as_json=True)
    assert {r["profile"] for r in json.loads(capsys.readouterr().out)} == {
        "claude", "claude-org"}

    search.run("staging database password", k=5, mode="text")
    out = capsys.readouterr().out
    assert "claude-code[claude-org]/user" in out
    assert "claude-code/user" in out  # the default profile reads as before
