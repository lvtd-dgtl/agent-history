"""Gemini CLI chat jsonl: $set folding, scaffolding skipped, project root."""
from __future__ import annotations

import json

from agent_history.adapters.gemini_cli import GeminiCliAdapter


def chat(tmp_path, lines, *, project="atrium", name="session-2026-06-30T20-58-abcd"):
    d = tmp_path / "gemini/tmp" / project
    (d / "chats").mkdir(parents=True, exist_ok=True)
    (d / ".project_root").write_text("/home/me/repos/atrium\n")
    path = d / "chats" / f"{name}.jsonl"
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
    return path


HEADER = {"sessionId": "s1", "projectHash": "h", "startTime": "t",
          "lastUpdated": "t", "kind": "main"}


def msg(i, typ, content, ts="2026-06-30T20:58:32.096Z"):
    return {"id": i, "timestamp": ts, "type": typ, "content": content}


def test_session_context_only_chat_yields_nothing(isolated_env):
    chat(isolated_env, [HEADER, {"$set": {"lastUpdated": "t", "messages": [
        msg("m1", "user", [{"text": "<session_context>\nThis is the Gemini CLI."
                                    " Today is a day.</session_context>"}])]}}])
    assert list(GeminiCliAdapter().records()) == []


def test_messages_fold_by_id_and_map_roles(isolated_env):
    chat(isolated_env, [
        HEADER,
        {"$set": {"messages": [msg("m1", "user", [{"text": "How do I rotate the signing key?"}])]}},
        msg("m2", "gemini", "Partial answer that is being str"),
        {"$set": {"messages": [
            msg("m1", "user", [{"text": "How do I rotate the signing key?"}]),
            msg("m2", "gemini", "Run the rotate command, then redeploy the service."),
            msg("m3", "info", "Update available for the CLI tool soon"),
        ]}},
    ])
    recs = list(GeminiCliAdapter().records())
    assert [(r.role, r.text) for r in recs] == [
        ("user", "How do I rotate the signing key?"),
        ("assistant", "Run the rotate command, then redeploy the service."),
    ]
    assert recs[0].agent == "gemini-cli"
    assert recs[0].cwd == "/home/me/repos/atrium"
    assert recs[0].session_id == "s1:session-2026-06-30T20-58-abcd"


def test_cwd_falls_back_to_project_dir_name(isolated_env):
    path = chat(isolated_env, [HEADER, msg("m1", "user", "A question long enough to index")])
    (path.parent.parent / ".project_root").unlink()
    assert list(GeminiCliAdapter().records())[0].cwd == "atrium"
