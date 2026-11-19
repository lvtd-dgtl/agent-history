"""The sessions command — recent-session listing."""
from __future__ import annotations

import hashlib
import json

import pytest

import time

from agent_history import cli, config, embed, recent, store

# These exercise the sqlite-vec virtual table, which some Python builds
# cannot load at all (notably uv/Homebrew macOS builds on 3.11+).
pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


def put(db, text, *, ts, session="s1", origin="/t/s1.jsonl", role="user",
        agent="claude-code", cwd="/w", **labels):
    store.insert_chunk(
        db, agent=agent, origin=origin, session_id=session, cwd=cwd,
        timestamp=ts, role=role, text=text, **labels,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        vector=embed.embed(text))
    db.commit()


@pytest.fixture
def db(fake_embed):
    db, _ = store.open_for_write()
    return db


@pytest.fixture
def history(db):
    put(db, "fix the flaky reconnect test please", ts="2026-01-01T01:00:00Z",
        session="old", origin="/t/old.jsonl", cwd="/repos/app")
    put(db, "done, the timer was the culprit", ts="2026-01-01T02:00:00Z",
        session="old", origin="/t/old.jsonl", role="assistant", cwd="/repos/app")
    put(db, "write the release notes\nfor 0.16", ts="2026-01-02T01:00:00Z",
        session="new", origin="/t/new.jsonl", cwd="/repos/tool")
    put(db, "here is a second prompt in the same session",
        ts="2026-01-02T05:00:00Z", session="new", origin="/t/new.jsonl",
        cwd="/repos/tool")
    put(db, "codex was asked about the build", ts="2026-01-01T12:00:00Z",
        session="cx", origin="/t/cx.jsonl", agent="codex", cwd="/repos/app")
    return db


def test_lists_sessions_newest_first(history):
    rows = recent.recent()
    assert [r["session_id"] for r in rows] == ["new", "cx", "old"]
    assert rows[0]["last_active"] == "2026-01-02T05:00:00Z"
    assert rows[0]["cwd"] == "/repos/tool"
    assert rows[0]["agent"] == "claude-code"
    assert rows[0]["machine"] == config.machine_id()  # local chunks carry it


def test_first_prompt_is_the_earliest_user_chunk(history):
    rows = {r["session_id"]: r for r in recent.recent()}
    assert rows["new"]["first_prompt"] == "write the release notes\nfor 0.16"
    assert rows["old"]["first_prompt"] == "fix the flaky reconnect test please"


def test_limit_agent_and_cwd_narrow(history):
    assert [r["session_id"] for r in recent.recent(limit=1)] == ["new"]
    assert [r["session_id"] for r in recent.recent(agent="codex")] == ["cx"]
    assert [r["session_id"] for r in recent.recent(cwd="app")] == ["cx", "old"]
    assert len(recent.recent(limit=0)) == 1  # clamped, never "everything"


@pytest.mark.skipif(not hasattr(time, "tzset"),
                    reason="time.tzset is POSIX-only")
def test_mixed_timestamp_flavours_order_by_instant(db, monkeypatch):
    """A naive local timestamp must not misorder against a UTC one."""
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        # 20:00 naive New York == 01:00Z next day, later than 23:00Z.
        put(db, "a utc stamped session prompt", ts="2026-01-01T23:00:00Z",
            session="utc", origin="/t/utc.jsonl")
        put(db, "a naive local stamped prompt", ts="2026-01-01T20:00:00",
            session="naive", origin="/t/naive.jsonl")
        assert [r["session_id"] for r in recent.recent()] == ["naive", "utc"]
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()


def test_pseudo_sessions_hidden_by_default(history):
    put(history, "a durable memory note about the deploy",
        ts="2026-02-01T00:00:00", session="memory:deploy",
        origin="/p/x/memory/deploy.md", role="memory")
    put(history, "a runbook page in a docs tree", ts="2026-02-01T00:00:00",
        session="doc:runbook.md", origin="/docs/runbook.md", role="doc",
        agent="docs")
    put(history, "subagent task: audit the store module",
        ts="2026-02-01T00:00:00Z", session="old",
        origin="/p/x/old/subagents/agent-a1.jsonl")
    put(history, "orphaned subagent with no parent indexed",
        ts="2026-02-02T00:00:00Z", session="orphan",
        origin="/p/x/orphan/subagents/agent-a2.jsonl")
    put(history, "a native windows subagent transcript",
        ts="2026-02-03T00:00:00Z", session="winorphan",
        origin=r"C:\p\x\winorphan\subagents\agent-a3.jsonl")

    rows = recent.recent()
    assert [r["session_id"] for r in rows] == ["new", "cx", "old"]
    # The subagent chunk neither bumps the parent's activity nor its prompt.
    old = rows[-1]
    assert old["last_active"] == "2026-01-01T02:00:00Z"
    assert old["first_prompt"] == "fix the flaky reconnect test please"

    every = {r["session_id"] for r in recent.recent(include_pseudo=True)}
    assert {"memory:deploy", "doc:runbook.md", "orphan", "winorphan"} <= every


def test_machine_column_is_shown_when_present(history, capsys):
    history.execute("UPDATE chunks SET machine = 'laptop' WHERE session_id = 'cx'")
    history.commit()
    rows = {r["session_id"]: r for r in recent.recent()}
    assert rows["cx"]["machine"] == "laptop"
    assert rows["old"]["machine"] == config.machine_id()

    assert recent.run() == 0
    out = capsys.readouterr().out
    assert "codex@laptop" in out
    assert f"@{config.machine_id()}" not in out  # this machine goes unsaid


def test_text_output_is_one_line_per_session(history, capsys):
    assert cli.main(["sessions", "-n", "2"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert "new" in lines[0] and "/repos/tool" in lines[0]
    assert "write the release notes for 0.16" in lines[0]  # newline flattened


def test_long_prompts_are_truncated(db, capsys):
    put(db, "word " * 100, ts="2026-01-01T00:00:00Z")
    recent.run()
    line = capsys.readouterr().out.strip()
    assert line.endswith("…")
    assert len(line) < 200


def test_json_output(history, capsys):
    assert cli.main(["sessions", "--json", "--agent", "codex"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows == [{
        "last_active": "2026-01-01T12:00:00Z",
        "agent": "codex",
        "machine": config.machine_id(),
        "session_id": "cx",
        "cwd": "/repos/app",
        "profile": None,
        "first_prompt": "codex was asked about the build",
    }]


def test_empty_index(db, capsys):
    assert recent.run() == 0
    assert "(no sessions)" in capsys.readouterr().out


def test_labelled_subagent_chunks_are_hidden_by_default(history):
    """0.18 labels subagent chunks; a label alone hides them, whatever the path."""
    put(history, "labelled subagent output", ts="2026-03-01T00:00:00Z",
        session="old", origin="/t/old.jsonl", agent_id="a74d",
        agent_type="job-researcher", sidechain=True)
    rows = {r["session_id"]: r for r in recent.recent()}
    assert rows["old"]["last_active"] == "2026-01-01T02:00:00Z"
    every = {r["session_id"]: r for r in recent.recent(include_pseudo=True)}
    assert every["old"]["last_active"] == "2026-03-01T00:00:00Z"


def test_sessions_name_their_claude_profile(db, capsys):
    put(db, "a prompt in the work account", ts="2026-01-03T00:00:00Z",
        session="org", origin="/h/.claude-org/projects/-w/org.jsonl")
    put(db, "a prompt in the default account", ts="2026-01-02T00:00:00Z",
        session="dflt", origin="/h/.claude/projects/-w/dflt.jsonl")
    rows = {r["session_id"]: r for r in recent.recent()}
    assert rows["org"]["profile"] == "claude-org"
    assert rows["dflt"]["profile"] == "claude"
    recent.run()
    out = capsys.readouterr().out
    assert "claude-code[claude-org]" in out
    assert "claude-code[claude]" not in out
