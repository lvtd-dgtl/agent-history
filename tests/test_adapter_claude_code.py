"""Claude Code transcript parsing — the part most exposed to messy real data."""
from __future__ import annotations

import json

from agent_history.adapters.claude_code import ClaudeCodeAdapter
from conftest import msg


def records(adapter=None):
    return list((adapter or ClaudeCodeAdapter()).records())


def test_user_and_assistant_text_is_kept(transcripts):
    transcripts("proj", "s", [
        msg("user", "why did we split the auth middleware"),
        msg("assistant", "because session refresh has to run first"),
    ])
    got = records()
    assert [r.role for r in got] == ["user", "assistant"]
    assert got[0].cwd == "/work"
    assert got[0].session_id == "sess-1"
    assert got[0].agent == "claude-code"


def test_messages_under_the_minimum_are_dropped(transcripts):
    transcripts("proj", "s", [msg("user", "ok"), msg("user", "a" * 25)])
    assert len(records()) == 1


def test_harness_noise_is_dropped(transcripts):
    transcripts("proj", "s", [
        msg("user", "<system-reminder>this is scaffolding you should ignore</system-reminder>"),
        msg("user", "<local-command-stdout>output that is not conversation</local-command-stdout>"),
        msg("user", "a genuine question about the codebase"),
    ])
    got = records()
    assert len(got) == 1
    assert got[0].text.startswith("a genuine question")


def test_tool_only_messages_produce_nothing(transcripts):
    """The bulk of a transcript is tool traffic and must not be indexed."""
    transcripts("proj", "s", [
        {"type": "assistant", "sessionId": "s", "cwd": "/w",
         "timestamp": "2026-01-01T00:00:00Z",
         "message": {"content": [{"type": "tool_use", "name": "Read",
                                  "input": {"file_path": "/etc/passwd"}}]}},
    ])
    assert records() == []


def test_meta_entries_are_skipped(transcripts):
    transcripts("proj", "s", [
        dict(msg("user", "this line is marked as meta scaffolding"), isMeta=True),
        msg("user", "this line is real conversation content"),
    ])
    assert len(records()) == 1


def test_non_user_assistant_types_are_skipped(transcripts):
    transcripts("proj", "s", [
        dict(msg("user", "a real message that should be kept"), **{"type": "summary"}),
        msg("assistant", "another real message that should be kept"),
    ])
    assert len(records()) == 1


def test_malformed_lines_do_not_abort_the_file(transcripts):
    path = transcripts("proj", "s", [msg("user", "first real message in the file")])
    with path.open("a", encoding="utf-8") as f:
        f.write("{not valid json at all\n")
        f.write("\n")
        f.write(json.dumps(msg("user", "message after the malformed line")) + "\n")
    got = records()
    assert len(got) == 2, "a broken line must not stop the rest of the file"


def test_plain_string_content_is_supported(transcripts):
    """Older transcripts store content as a bare string rather than blocks."""
    transcripts("proj", "s", [{
        "type": "user", "sessionId": "s", "cwd": "/w",
        "timestamp": "2026-01-01T00:00:00Z",
        "message": {"content": "a plain string message body here"},
    }])
    assert len(records()) == 1


def test_subagent_transcripts_in_nested_dirs_are_found(transcripts):
    transcripts("proj/sub/agents", "agent-x", [
        msg("assistant", "a subagent result worth remembering"),
    ])
    assert len(records()) == 1


def test_memory_files_are_indexed(transcripts, isolated_env):
    mem = isolated_env / "projects" / "proj" / "memory"
    mem.mkdir(parents=True)
    (mem / "decisions.md").write_text("We chose sqlite-vec over a separate service.")
    got = records()
    assert len(got) == 1
    assert got[0].role == "memory"
    assert got[0].session_id == "memory:decisions"


def test_empty_memory_file_is_ignored(transcripts, isolated_env):
    mem = isolated_env / "projects" / "proj" / "memory"
    mem.mkdir(parents=True)
    (mem / "empty.md").write_text("   ")
    assert records() == []


def test_available_is_false_without_any_roots(monkeypatch, tmp_path):
    monkeypatch.setenv("AGENT_HISTORY_PROJECTS", str(tmp_path / "missing"))
    assert ClaudeCodeAdapter().available() is False


def test_base64_payloads_are_scrubbed_before_indexing(transcripts):
    transcripts("proj", "s", [msg("user", "here is the file: " + "A1b2C3d4" * 60)])
    assert "[blob]" in records()[0].text


# ------------------------------------------------- subagents and forked sessions

def test_subagent_file_records_are_labelled(transcripts):
    transcripts("proj/parent-1/subagents", "agent-a74d1234", [
        msg("assistant", "the subagent found three candidate files",
            session="parent-1", isSidechain=True, agentId="a74d1234",
            attributionAgent="job-researcher"),
    ])
    (rec,) = records()
    assert rec.session_id == "parent-1"  # still the parent's id on the wire
    assert (rec.sidechain, rec.agent_id, rec.agent_type) == (
        True, "a74d1234", "job-researcher")


def test_subagent_id_falls_back_to_the_file_name(transcripts):
    transcripts("proj/parent-1/subagents", "agent-bee99", [
        msg("user", "a task handed to a subagent without ids", session="parent-1"),
    ])
    (rec,) = records()
    assert (rec.sidechain, rec.agent_id, rec.agent_type) == (True, "bee99", None)


def test_main_session_records_are_not_marked(transcripts):
    transcripts("proj", "s", [msg("user", "an ordinary top-level message")])
    (rec,) = records()
    assert (rec.sidechain, rec.agent_id, rec.agent_type, rec.fork_of) == (
        False, None, None, None)


def test_forked_lines_are_skipped_and_the_rest_carry_fork_of(transcripts):
    fork = {"sessionId": "parent-1", "messageUuid": "u-1"}
    transcripts("proj", "child-1", [
        msg("user", "copied from the parent at the fork point",
            session="child-1", forkedFrom=fork),
        msg("assistant", "also copied from the parent transcript",
            session="child-1", forkedFrom=fork),
        msg("user", "a genuinely new turn after the fork", session="child-1"),
    ])
    (rec,) = records()
    assert rec.text == "a genuinely new turn after the fork"
    assert rec.session_id == "child-1" and rec.fork_of == "parent-1"
