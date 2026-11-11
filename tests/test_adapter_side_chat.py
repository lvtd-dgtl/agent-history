"""Side-chat adapter, and its link to the parent Claude Code session."""
from __future__ import annotations

import json
import sys

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


@pytest.mark.parametrize("bad", ["../../etc/passwd", "a/b/c/d/e/f/g/h", "x", "a" * 200,
                                 PARENT + "\n", "abcdefgh\0x", " " + PARENT, 12345678,
                                 ["list"], {"a": 1}, True])
def test_parent_with_bad_shape_is_dropped_not_linked(side, bad):
    side(SIDE, [line("user", "a long enough question here", parent=bad)])
    got = list(SideChatAdapter().records())
    assert len(got) == 1 and got[0].fork_of is None


def test_parent_equal_to_session_is_dropped(side):
    side(SIDE, [line("user", "a long enough question here", parent=SIDE)])
    assert list(SideChatAdapter().records())[0].fork_of is None


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlinked_files_do_not_crash(side, isolated_env):
    root = isolated_env / "side-chats"
    side(SIDE, [line("user", "a long enough question here")])
    (root / "dangling.jsonl").symlink_to(root / "nowhere.jsonl")
    (root / "loop.jsonl").symlink_to(root / "loop.jsonl")
    (root / "adir.jsonl").symlink_to(isolated_env)  # a directory
    got = list(SideChatAdapter().records())
    assert [r.session_id for r in got] == [SIDE]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlink_to_non_jsonl_content_yields_nothing(side, isolated_env):
    root = isolated_env / "side-chats"
    side(SIDE, [line("user", "a long enough question here")])
    other = isolated_env / "secret.txt"
    other.write_text("-----BEGIN KEY-----\nabc\n")
    (root / "link-to-text.jsonl").symlink_to(other)
    assert [r.session_id for r in SideChatAdapter().records()] == [SIDE]


def test_malformed_and_oversized_lines_do_not_crash(side):
    path = side(SIDE, [line("user", "a long enough question here")])
    big = json.dumps(line("assistant", "y" * (5 * 1024 * 1024)))
    with path.open("ab") as f:
        f.write(b"\xff\xfe not utf8 \x00\n")
        f.write(b"[1, 2, 3]\n")
        f.write(b'"just a string"\n')
        f.write(b'{"v": 1, "role": "user", "text": 5}\n')
        f.write(b'{"v": 1, "role": "user", "text": "unterminated\n')
        f.write(b"\n")
        f.write(big.encode() + b"\n")
        f.write(json.dumps(line("user", "a last good line after the bad")).encode() + b"\n")
    got = list(SideChatAdapter().records())
    assert [r.role for r in got][0] == "user"
    assert got[-1].text.startswith("a last good line")


@needs_ext
def test_bad_parent_and_symlink_do_not_break_indexing(side, transcripts, fake_embed,
                                                       isolated_env):
    root = isolated_env / "side-chats"
    side(SIDE, [line("user", "quick side question about the ttl", parent="../../x")])
    if sys.platform != "win32":
        (root / "dangling.jsonl").symlink_to(root / "nowhere.jsonl")
    assert index.run() == 0
    db = store.open_for_read()
    c = sessions.derive_uid("claude-code-side", "testbox", SIDE)
    meta = json.loads(db.execute("SELECT meta FROM sessions WHERE uid = ?",
                                 (c,)).fetchone()[0] or "{}")
    assert "fork_of" not in meta


@needs_ext
def test_existing_different_parent_is_kept_and_same_parent_links(side, transcripts,
                                                                 fake_embed):
    """link_fork keeps a child's existing different parent (PR #48)."""
    other = "aaaa1111-2222-4333-8444-555566667777"
    transcripts("proj", PARENT, [msg("user", "the main conversation about caching",
                                     session=PARENT)])
    transcripts("proj", other, [msg("user", "an unrelated conversation here",
                                    session=other)])
    side(SIDE, [line("user", "quick side question about the ttl")])
    index.run()
    db = store.open_for_read()
    p = sessions.derive_uid("claude-code", "testbox", PARENT)
    o = sessions.derive_uid("claude-code", "testbox", other)
    c = sessions.derive_uid("claude-code-side", "testbox", SIDE)
    db.close()
    wdb = store.open_for_maintenance()
    sessions.link_fork(wdb, c, o)  # a later, different claimed parent
    wdb.commit()
    m = lambda u: json.loads(wdb.execute("SELECT meta FROM sessions WHERE uid = ?",
                                         (u,)).fetchone()[0])
    assert m(c)["fork_of"] == p and "forks" not in m(o)
    sessions.link_fork(wdb, c, p)  # same parent again is a no-op
    assert m(p)["forks"] == [c]
