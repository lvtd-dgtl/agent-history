"""Side-chat adapter, and its link to the parent Claude Code session."""
from __future__ import annotations

import json

import pytest

from agent_history import index, sessions, store
from agent_history.adapters import all_adapters
from agent_history.adapters.side_chat import SideChatAdapter
from agent_history.textproc import MIN_CHARS
from conftest import msg

SIDE = "ecf3deb0-1111-4222-8333-444455556666"
PARENT = "e18e5130-5211-4d3c-8162-addd565dd983"


@pytest.fixture
def side(isolated_env):
    root = isolated_env / "side-chats"

    def _write(session, lines):
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"{session}.jsonl"
        path.write_text("".join(json.dumps(x) + "\n" for x in lines))
        return path
    return _write


needs_ext = pytest.mark.skipif(not store.extensions_supported(),
                               reason="this Python cannot load SQLite extensions")


def line(role, text, parent=PARENT, session=SIDE, **extra):
    d = {"v": 1, "session_id": session, "parent_session_id": parent,
         "cwd": "/work", "timestamp": "2026-11-09T10:00:00.000Z",
         "role": role, "text": text, "entrypoint": "claude-desktop"}
    d.update(extra)
    return d


def test_registered():
    assert "claude-code-side" in [a.name for a in all_adapters()]


def test_unavailable_without_directory(isolated_env):
    assert not SideChatAdapter().available()


def test_records_carry_fields_and_fork_of(side):
    side(SIDE, [line("user", "what does this cache invalidation do?"),
                line("assistant", "it drops entries older than the ttl window")])
    got = list(SideChatAdapter().records())
    assert [r.role for r in got] == ["user", "assistant"]
    r = got[0]
    assert (r.agent, r.session_id, r.cwd, r.fork_of) == (
        "claude-code-side", SIDE, "/work", PARENT)
    assert r.timestamp == "2026-11-09T10:00:00.000Z"


def test_min_chars_bad_lines_and_no_parent(side):
    path = side(SIDE, [line("user", "x" * (MIN_CHARS - 1)),
                       line("user", "a long enough question here", parent=None),
                       line("user", "wrong version of a line here", v=2),
                       line("system", "not a conversation role at all")])
    with path.open("a") as f:
        f.write("not json\n")
    got = list(SideChatAdapter().records())
    assert len(got) == 1 and got[0].fork_of is None


@needs_ext
def test_secrets_are_redacted_at_ingest(side, fake_embed, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_REDACT", "on")
    secret = "sk-ant-api03-" + "A1b2C3d4" * 6
    side(SIDE, [line("user", f"please check this key {secret} is valid")])
    index.run()
    db = store.open_for_read()
    texts = [r[0] for r in db.execute("SELECT text FROM chunks")]
    assert texts and all(secret not in t for t in texts)


@needs_ext
def test_side_chat_links_to_claude_code_parent(side, transcripts, fake_embed):
    transcripts("proj", PARENT, [msg("user", "the main conversation about caching",
                                     session=PARENT)])
    side(SIDE, [line("user", "quick side question about the ttl")])
    index.run()
    index.run()
    db = store.open_for_read()
    p = sessions.derive_uid("claude-code", "testbox", PARENT)
    c = sessions.derive_uid("claude-code-side", "testbox", SIDE)

    def meta(uid):
        return json.loads(db.execute("SELECT meta FROM sessions WHERE uid = ?",
                                     (uid,)).fetchone()[0])
    assert meta(c)["fork_of"] == p
    assert meta(p)["forks"] == [c]
    agents = {r[0] for r in db.execute("SELECT DISTINCT agent FROM chunks")}
    assert agents == {"claude-code", "claude-code-side"}
