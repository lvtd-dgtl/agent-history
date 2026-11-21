"""T3 Code statev2.sqlite: index only providers with no native adapter."""
from __future__ import annotations

import json
import sqlite3

from agent_history.adapters.t3code import T3CodeAdapter, is_native


def make_db(tmp_path):
    path = tmp_path / "t3/statev2.sqlite"
    path.parent.mkdir(parents=True)
    db = sqlite3.connect(path)
    db.executescript("""
    CREATE TABLE projection_projects (project_id TEXT PRIMARY KEY, title TEXT,
        workspace_root TEXT, secret TEXT);
    CREATE TABLE orchestration_v2_projection_threads (thread_id TEXT PRIMARY KEY,
        project_id TEXT, title TEXT, default_provider TEXT, deleted_at TEXT,
        provider_instance_id TEXT);
    CREATE TABLE orchestration_v2_projection_provider_threads (
        provider_thread_id TEXT PRIMARY KEY, thread_id TEXT, driver TEXT,
        provider_instance_id TEXT);
    CREATE TABLE orchestration_v2_projection_messages (message_id TEXT PRIMARY KEY,
        thread_id TEXT, role TEXT, streaming INTEGER, created_at TEXT,
        payload_json TEXT);
    CREATE TABLE auth_sessions (token TEXT);
    """)
    db.execute("INSERT INTO projection_projects VALUES ('p1','repos','/home/me/repos','x')")
    db.execute("INSERT INTO auth_sessions VALUES ('SECRET-SESSION-TOKEN')")
    db.commit()
    return db


def add_thread(db, tid, driver, instance=None, *, default=None, deleted=None):
    instance = instance or driver
    db.execute("INSERT INTO orchestration_v2_projection_threads VALUES (?,?,?,?,?,?)",
               (tid, "p1", "t", default or driver, deleted, instance))
    db.execute("INSERT INTO orchestration_v2_projection_provider_threads "
               "VALUES (?,?,?,?)", ("pt-" + tid, tid, driver, instance))
    db.commit()


_n = [0]


def add_msg(db, tid, role, text, *, streaming=0, ts="2026-11-21T03:07:00.905Z"):
    _n[0] += 1
    db.execute("INSERT INTO orchestration_v2_projection_messages VALUES (?,?,?,?,?,?)",
               (f"m{_n[0]}", tid, role, streaming, ts,
                json.dumps({"id": f"m{_n[0]}", "role": role, "text": text})))
    db.commit()


def test_native_provider_threads_are_skipped_others_indexed(isolated_env):
    db = make_db(isolated_env)
    add_thread(db, "t-claude", "claudeAgent", "claudeAgent_claude_lvtd")
    add_thread(db, "import:claudeAgent:abc", "claudeAgent")
    add_thread(db, "t-grok", "grok")
    add_thread(db, "t-codex", "codex")
    add_thread(db, "t-hermes", "acp", "acpRegistry_hermes_morph")
    add_thread(db, "t-agy", "antigravity", "antigravity")
    for t in ("t-claude", "import:claudeAgent:abc", "t-grok", "t-codex",
              "t-hermes", "t-agy"):
        add_msg(db, t, "user", f"question for thread {t} please")
    add_msg(db, "t-agy", "assistant", "Antigravity answer with enough text")
    recs = list(T3CodeAdapter().records())
    assert {r.session_id for r in recs} == {"t-agy"}
    assert [r.role for r in recs] == ["user", "assistant"]
    assert all(r.agent == "t3code" and r.cwd == "/home/me/repos" for r in recs)
    assert recs[0].origin.endswith("#antigravity")


def test_handed_off_thread_counts_as_uncovered(isolated_env):
    db = make_db(isolated_env)
    add_thread(db, "t-mix", "claudeAgent")
    db.execute("INSERT INTO orchestration_v2_projection_provider_threads "
               "VALUES ('pt2','t-mix','antigravity','antigravity')")
    add_msg(db, "t-mix", "user", "handed over to another provider")
    assert len(list(T3CodeAdapter().records())) == 1


def test_streaming_deleted_and_short_messages_skipped(isolated_env):
    db = make_db(isolated_env)
    add_thread(db, "t-a", "antigravity")
    add_thread(db, "t-del", "antigravity", deleted="2026-11-21T00:00:00Z")
    add_msg(db, "t-a", "assistant", "Half written reply that is long en", streaming=1)
    add_msg(db, "t-a", "user", "hi")
    add_msg(db, "t-a", "user", "A real question that is long enough")
    add_msg(db, "t-del", "user", "Message in a deleted thread, long enough")
    recs = list(T3CodeAdapter().records())
    assert [r.text for r in recs] == ["A real question that is long enough"]


def test_unrelated_tables_are_never_read(isolated_env):
    db = make_db(isolated_env)
    add_thread(db, "t-a", "antigravity")
    add_msg(db, "t-a", "user", "Question about the build pipeline please")
    assert all("SECRET" not in r.text for r in T3CodeAdapter().records())


def test_is_native_classification():
    assert is_native("claudeAgent_claude_lvtd") and is_native("codex")
    assert is_native("acpRegistry_hermes_morph") and is_native("opencode")
    assert not is_native("antigravity") and not is_native("cursor")
    assert not is_native(None)


def test_missing_or_broken_store_is_quiet(isolated_env):
    assert not T3CodeAdapter().available()
    p = isolated_env / "t3/statev2.sqlite"
    p.parent.mkdir(parents=True)
    p.write_bytes(b"not a database")
    assert list(T3CodeAdapter().records()) == []
