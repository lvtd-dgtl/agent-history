"""Where the all-harness adapters look: Windows homes seen from WSL, and the
read-only SQLite fallback for databases on a Windows drive."""
from __future__ import annotations

import sqlite3

import pytest

from agent_history import config
from agent_history.adapters import _sqlite
from agent_history.adapters.hermes import HermesAdapter


@pytest.fixture
def wsl(isolated_env, monkeypatch):
    """A fake /mnt/c/Users with one profile, and WSL detection switched on."""
    users = isolated_env / "Users"
    (users / "Public").mkdir(parents=True)
    home = isolated_env / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("AGENT_HISTORY_NO_WSL", raising=False)
    monkeypatch.delenv("AGENT_HISTORY_HERMES_DIRS", raising=False)
    return users


def test_hermes_t3_profile_found_in_windows_home(wsl, isolated_env):
    win = wsl / "dana/.hermes-t3"
    win.mkdir(parents=True)
    (win / "state.db").write_bytes(b"")
    local = isolated_env / "home/.hermes-t3"
    local.mkdir(parents=True)
    (local / "state.db").write_bytes(b"")
    assert config.hermes_state_dbs(_users_dir=wsl) == [
        local / "state.db", win / "state.db"]


def test_hermes_profile_without_state_db_ignored(wsl):
    (wsl / "dana/.hermes-t3").mkdir(parents=True)
    assert config.hermes_state_dbs(_users_dir=wsl) == []


def test_windows_claude_morph_profile_is_discovered(wsl, monkeypatch):
    monkeypatch.delenv("AGENT_HISTORY_PROJECTS")
    (wsl / "dana/.claude-morph/projects/C--Users-dana").mkdir(parents=True)
    assert config.claude_profile_dirs(_users_dir=wsl) == [
        wsl / "dana/.claude-morph/projects"]


def test_grok_gemini_t3_defaults_cover_wsl_and_windows(wsl, monkeypatch, isolated_env):
    for name in ("AGENT_HISTORY_GROK_DIRS", "AGENT_HISTORY_GEMINI_DIRS",
                 "AGENT_HISTORY_T3_DBS"):
        monkeypatch.delenv(name)
    monkeypatch.setattr(config, "wsl_windows_homes",
                        lambda _u=None, marker="": [wsl / "dana"])
    home = isolated_env / "home"
    assert config.grok_session_dirs() == [
        home / ".grok/sessions", wsl / "dana/.grok/sessions"]
    assert config.gemini_cli_dirs() == [
        home / ".gemini/tmp", wsl / "dana/.gemini/tmp"]
    assert config.t3_state_dbs() == [
        home / ".t3/userdata/statev2.sqlite",
        wsl / "dana/.t3/userdata/statev2.sqlite"]


def test_grok_home_env_moves_the_sessions_root(isolated_env, monkeypatch):
    monkeypatch.delenv("AGENT_HISTORY_GROK_DIRS")
    monkeypatch.setenv("GROK_HOME", str(isolated_env / "elsewhere"))
    assert config.grok_session_dirs() == [isolated_env / "elsewhere/sessions"]


def test_open_readonly_falls_back_to_a_copy(isolated_env, monkeypatch):
    path = isolated_env / "live.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE t (x TEXT)")
    db.execute("INSERT INTO t VALUES ('hello')")
    db.commit()
    db.close()
    real = _sqlite._connect
    calls = []

    def flaky(p):
        calls.append(p)
        if len(calls) == 1:
            raise sqlite3.OperationalError("disk I/O error")
        return real(p)

    monkeypatch.setattr(_sqlite, "_connect", flaky)
    with _sqlite.open_readonly(path) as conn:
        assert conn.execute("SELECT x FROM t").fetchone() == ("hello",)
    assert calls[1] != path  # worked from a private copy
    assert path.exists()  # the source is untouched


def test_open_readonly_yields_none_for_garbage(isolated_env):
    path = isolated_env / "bad.sqlite"
    path.write_bytes(b"not a database at all")
    with _sqlite.open_readonly(path) as conn:
        assert conn is None


def test_hermes_still_reads_through_the_helper(isolated_env, monkeypatch):
    profile = isolated_env / "hermes/.hermes-t3"
    profile.mkdir(parents=True)
    db = sqlite3.connect(profile / "state.db")
    db.executescript(
        "CREATE TABLE sessions (id TEXT PRIMARY KEY, started_at REAL, cwd TEXT);"
        "CREATE TABLE messages (id INTEGER PRIMARY KEY, session_id TEXT, "
        "role TEXT, content TEXT, timestamp REAL);"
        "INSERT INTO sessions VALUES ('s1', 1.0, '/tmp');"
        "INSERT INTO messages (session_id, role, content, timestamp) VALUES "
        "('s1','user','Reply with exactly: hermes wsl ok',1.0);")
    db.commit()
    db.close()
    monkeypatch.setenv("AGENT_HISTORY_HERMES_DIRS", str(profile))
    recs = list(HermesAdapter().records())
    assert [r.text for r in recs] == ["Reply with exactly: hermes wsl ok"]
