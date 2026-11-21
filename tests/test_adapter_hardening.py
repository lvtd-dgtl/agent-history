"""Review fixes: consistent SQLite snapshots, gemini $set replace, grok settle."""
from __future__ import annotations

import json
import os
import sqlite3

from agent_history.adapters import _sqlite
from agent_history.adapters.gemini_cli import GeminiCliAdapter
from agent_history.adapters.grok import GrokAdapter

from test_adapter_gemini_cli import HEADER, chat, msg
from test_adapter_grok import _update, write_session


# ---- gemini: a $set that carries messages replaces the accumulated list

def test_set_messages_replaces_and_empty_list_clears(isolated_env):
    chat(isolated_env, [
        HEADER,
        {"$set": {"messages": [msg("m1", "user", "The first question, long enough")]}},
        {"$set": {"messages": [msg("m2", "user", "The replacement question, long enough")]}},
    ])
    assert [r.text for r in GeminiCliAdapter().records()] == [
        "The replacement question, long enough"]
    chat(isolated_env, [
        HEADER,
        {"$set": {"messages": [msg("m1", "user", "A question later cleared, long enough")]}},
        {"$set": {"messages": []}},
    ], name="session-cleared")
    assert [r for r in GeminiCliAdapter().records()
            if "cleared" in r.session_id] == []


def test_metadata_only_set_keeps_messages(isolated_env):
    chat(isolated_env, [
        HEADER,
        {"$set": {"messages": [msg("m1", "user", "A question that must survive, long")]}},
        {"$set": {"lastUpdated": "later"}},
    ])
    assert len(list(GeminiCliAdapter().records())) == 1


# ---- grok: the trailing message is judged against the file's state at flush

def test_trailing_message_held_if_file_grows_during_read(isolated_env, monkeypatch):
    path = write_session(isolated_env, [
        _update("user_message_chunk", "A complete question for grok?", 1),
        _update("agent_message_chunk", "Partial answer that is still str", 2)])
    adapter = GrokAdapter()
    adapter.settle_seconds = 0
    real_open = type(path).open

    def growing(self, *a, **k):
        handle = real_open(self, *a, **k)
        if self == path:  # the writer appends while we read
            with real_open(self, "a") as f:
                f.write(json.dumps(
                    _update("agent_message_chunk", "eaming on", 3)) + "\n")
        return handle

    monkeypatch.setattr(type(path), "open", growing)
    assert [r.role for r in adapter.records()] == ["user"]


# ---- sqlite fallback copy

def _live_db(path):
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE t (x TEXT)")
    db.execute("INSERT INTO t VALUES ('hello')")
    db.commit()
    db.close()


def _force_copy(monkeypatch):
    real = _sqlite._connect
    state = {"first": True}

    def flaky(p):
        if state["first"]:
            state["first"] = False
            raise sqlite3.OperationalError("disk I/O error")
        return real(p)

    monkeypatch.setattr(_sqlite, "_connect", flaky)


def test_journal_present_means_no_copy(isolated_env, monkeypatch):
    path = isolated_env / "j.sqlite"
    _live_db(path)
    (isolated_env / "j.sqlite-journal").write_bytes(b"x")
    _force_copy(monkeypatch)
    with _sqlite.open_readonly(path) as conn:
        assert conn is None


def test_snapshot_retries_when_source_moves_then_settles(isolated_env, monkeypatch):
    path = isolated_env / "m.sqlite"
    _live_db(path)
    _force_copy(monkeypatch)
    real_copy = _sqlite.shutil.copyfile
    calls = []

    def touching(src, dest):
        real_copy(src, dest)
        calls.append(src)
        if len(calls) == 1:  # a checkpoint lands mid-copy, once
            os.utime(path, (1_500_000_000, 1_500_000_000))

    monkeypatch.setattr(_sqlite.shutil, "copyfile", touching)
    with _sqlite.open_readonly(path) as conn:
        assert conn.execute("SELECT x FROM t").fetchone() == ("hello",)
    assert len(calls) > 1


def test_snapshot_gives_up_if_source_never_settles(isolated_env, monkeypatch):
    path = isolated_env / "n.sqlite"
    _live_db(path)
    _force_copy(monkeypatch)
    real_copy = _sqlite.shutil.copyfile
    tick = [1_600_000_000]

    def always(src, dest):
        real_copy(src, dest)
        tick[0] += 10
        os.utime(path, (tick[0], tick[0]))

    monkeypatch.setattr(_sqlite.shutil, "copyfile", always)
    with _sqlite.open_readonly(path) as conn:
        assert conn is None


def test_snapshot_metadata_error_skips_the_source(isolated_env, monkeypatch):
    path = isolated_env / "p.sqlite"
    _live_db(path)
    _force_copy(monkeypatch)

    def denied(p):
        raise PermissionError("denied")

    monkeypatch.setattr(_sqlite, "_stamp", denied)
    with _sqlite.open_readonly(path) as conn:
        assert conn is None
