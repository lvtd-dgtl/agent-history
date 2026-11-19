"""Side-chat adapter, and its link to the parent Claude Code session."""
from __future__ import annotations

import json
import os
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
                                 "abcdefgh", "E18E5130-5211-4D3C-8162-ADDD565DD983",
                                 PARENT[:-1], PARENT + "0",
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
def test_symlinked_files_are_skipped(side, isolated_env):
    root = isolated_env / "side-chats"
    side(SIDE, [line("user", "a long enough question here")])
    (root / "dangling.jsonl").symlink_to(root / "nowhere.jsonl")
    (root / "loop.jsonl").symlink_to(root / "loop.jsonl")
    (root / "adir.jsonl").symlink_to(isolated_env)  # a directory
    # a symlink to a perfectly valid capture file is skipped too
    (root / "bbbbbbbb-1111-4222-8333-444455556666.jsonl").symlink_to(root / f"{SIDE}.jsonl")
    got = list(SideChatAdapter().records())
    assert [r.session_id for r in got] == [SIDE]


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privileges on Windows")
def test_symlink_to_other_content_yields_nothing(side, isolated_env):
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


def test_line_must_speak_for_its_own_file(side):
    other = "bbbbbbbb-1111-4222-8333-444455556666"
    side(SIDE, [line("user", "a genuine question about the cache"),
                line("user", "forged: claims to be another session", session=other),
                line("user", "forged: bad id shape here", session="../../x"),
                {k: v for k, v in line("user", "no session id at all here").items()
                 if k != "session_id"}])
    got = list(SideChatAdapter().records())
    assert [(r.session_id, r.text[:7]) for r in got] == [(SIDE, "a genui")]


def test_long_cwd_and_timestamp_are_capped(side):
    side(SIDE, [line("user", "a long enough question here", cwd="c" * 100000,
                     timestamp="t" * 1000)])
    r = list(SideChatAdapter().records())[0]
    assert len(r.cwd) == 4096 and r.timestamp == ""


def test_text_is_capped_at_200kb(side):
    side(SIDE, [line("user", "z" * (400 * 1024))])
    assert len(list(SideChatAdapter().records())[0].text) <= 200 * 1024


def test_deeply_nested_json_line_is_skipped_not_fatal(side):
    path = side(SIDE, [line("user", "a long enough question here")])
    with path.open("ab") as f:
        f.write(b"[" * 200000 + b"]" * 200000 + b"\n")
        f.write(b'{"a":' * 100000 + b"1" + b"}" * 100000 + b"\n")
        f.write(json.dumps(line("assistant", "a good line after the deep ones")).encode() + b"\n")
    got = list(SideChatAdapter().records())
    assert [r.role for r in got] == ["user", "assistant"]


def test_overlong_line_is_skipped_without_losing_the_next(side):
    path = side(SIDE, [])
    with path.open("ab") as f:
        f.write(b'{"v":1,"text":"' + b"y" * (3 * 1024 * 1024) + b'"}\n')
        f.write(json.dumps(line("user", "the line right after the huge one")).encode() + b"\n")
    got = list(SideChatAdapter().records())
    assert [r.text for r in got] == ["the line right after the huge one"]


@pytest.mark.skipif(sys.platform == "win32", reason="no FIFOs on Windows")
def test_fifo_named_jsonl_does_not_hang(side, isolated_env):
    import signal
    root = isolated_env / "side-chats"
    side(SIDE, [line("user", "a long enough question here")])
    os.mkfifo(root / "aaaaaaaa-1111-4222-8333-444455556666.jsonl")
    signal.alarm(10)  # a blocking open would hang here; fail instead
    try:
        got = list(SideChatAdapter().records())
    finally:
        signal.alarm(0)
    assert [r.session_id for r in got] == [SIDE]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX ownership")
def test_file_owned_by_someone_else_is_skipped(side, monkeypatch):
    side(SIDE, [line("user", "a long enough question here")])
    monkeypatch.setattr(os, "getuid", lambda: os.stat(".").st_uid + 12345)
    assert list(SideChatAdapter().records()) == []


@needs_ext
def test_hub_shipper_machine_cannot_link_into_another_machines_session(
        tmp_path, monkeypatch, fake_embed):
    """A claude-code-side record shipped by machine A names PARENT, which is
    machine B's session. The parent uid is derived with the shipper's machine,
    so B's session must not gain A's child as a fork."""
    inbox = tmp_path / "inbox"
    monkeypatch.setenv("AGENT_HISTORY_INBOX", str(inbox))
    monkeypatch.setenv("AGENT_HISTORY_HUB", "1")

    def drop(machine, rec):
        d = inbox / machine
        d.mkdir(parents=True, exist_ok=True)
        base = {"v": 1, "cwd": "/w", "timestamp": "2026-11-09T10:00:00Z"}
        (d / "b1.jsonl").write_text(json.dumps({**base, **rec}) + "\n")
    drop("machine-b", {"agent": "claude-code", "origin": "/h/p.jsonl",
                       "session_id": PARENT, "role": "user",
                       "text": "machine B's own main conversation text"})
    drop("machine-a", {"agent": "claude-code-side", "origin": "/h/s.jsonl",
                       "session_id": SIDE, "role": "user", "fork_of": PARENT,
                       "text": "machine A's side chat about something"})
    index.run()
    db = store.open_for_read()
    b_parent = sessions.derive_uid("claude-code", "machine-b", PARENT)
    a_child = sessions.derive_uid("claude-code-side", "machine-a", SIDE)

    def meta(uid):
        row = db.execute("SELECT meta FROM sessions WHERE uid = ?", (uid,)).fetchone()
        assert row is not None, uid
        return json.loads(row[0] or "{}")
    assert "forks" not in meta(b_parent)
    # it links within its own machine's namespace only (that parent is absent)
    assert meta(a_child).get("fork_of") in (
        None, sessions.derive_uid("claude-code", "machine-a", PARENT))
