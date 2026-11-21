"""Grok CLI updates.jsonl parsing: chunk joining, scaffolding, settling."""
from __future__ import annotations

import json
import os
import time

from agent_history.adapters.grok import GrokAdapter


def _update(kind, text, ts):
    return {"timestamp": ts, "method": "session/update", "params": {
        "update": {"sessionUpdate": kind,
                   "content": {"type": "text", "text": text}}}}


def write_session(tmp_path, lines, *, sid="0192f7a0-aaaa-7000-8000-000000000001",
                  group="%2Fhome%2Fme%2Frepo", summary=None, age=3600):
    d = tmp_path / "grok/sessions" / group / sid
    d.mkdir(parents=True)
    path = d / "updates.jsonl"
    path.write_text("\n".join(json.dumps(x) if not isinstance(x, str) else x
                              for x in lines) + "\n")
    if summary is not None:
        (d / "summary.json").write_text(json.dumps(summary))
    old = time.time() - age
    os.utime(path, (old, old))
    return path


def test_chunks_join_into_messages(isolated_env):
    write_session(isolated_env, [
        _update("user_message_chunk", "Please add a retry ", 1795230000),
        _update("user_message_chunk", "to the uploader module", 1795230000),
        _update("agent_thought_chunk", "thinking about retries", 1795230001),
        _update("agent_message_chunk", "I will add exponential ", 1795230002),
        _update("agent_message_chunk", "backoff to uploader.py.", 1795230002),
        {"timestamp": 1795230003, "method": "session/update", "params": {
            "update": {"sessionUpdate": "tool_call", "title": "edit"}}},
        _update("agent_message_chunk", "Done: the uploader now retries.", 1795230004),
    ], summary={"info": {"cwd": "/home/me/repo"}})
    recs = list(GrokAdapter().records())
    assert [(r.role, r.text) for r in recs] == [
        ("user", "Please add a retry to the uploader module"),
        ("assistant", "I will add exponential backoff to uploader.py."),
        ("assistant", "Done: the uploader now retries."),
    ]
    assert all(r.agent == "grok" and r.cwd == "/home/me/repo" for r in recs)
    assert recs[0].session_id == "0192f7a0-aaaa-7000-8000-000000000001"
    assert "thinking" not in " ".join(r.text for r in recs)


def test_cwd_falls_back_to_decoded_group(isolated_env):
    write_session(isolated_env, [
        _update("user_message_chunk", "Hello there, explain this repo layout", 1)])
    assert list(GrokAdapter().records())[0].cwd == "/home/me/repo"


def test_streaming_tail_is_held_until_settled(isolated_env):
    lines = [_update("user_message_chunk", "A complete question for grok?", 1),
             _update("agent_message_chunk", "Partial answer that is still str", 2)]
    write_session(isolated_env, lines, age=1)  # just written
    assert [r.role for r in GrokAdapter().records()] == ["user"]
    adapter = GrokAdapter()
    adapter.settle_seconds = 0
    assert [r.role for r in adapter.records()] == ["user", "assistant"]


def test_torn_and_foreign_lines_are_skipped(isolated_env):
    write_session(isolated_env, [
        "{not json", "[]", {"timestamp": 1, "method": "x", "params": {}},
        _update("user_message_chunk", "One valid message that is long enough", 5),
    ])
    assert len(list(GrokAdapter().records())) == 1


def test_auth_json_is_never_read(isolated_env):
    home = isolated_env / "grok"
    (home / "sessions").mkdir(parents=True)
    (home / "auth.json").write_text('{"token": "SECRET-TOKEN-VALUE-1234567890"}')
    write_session(isolated_env, [
        _update("user_message_chunk", "A normal question about the build", 5)])
    text = " ".join(r.text for r in GrokAdapter().records())
    assert "SECRET-TOKEN" not in text


def test_unavailable_without_sessions_dir(isolated_env):
    assert not GrokAdapter().available()
