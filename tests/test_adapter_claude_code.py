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
    transcripts("proj", "parent-1", [
        msg("user", "the parent's own question here", session="parent-1")])
    transcripts("proj", "child-1", [
        msg("user", "copied from the parent at the fork point",
            session="child-1", forkedFrom=fork),
        msg("assistant", "also copied from the parent transcript",
            session="child-1", forkedFrom=fork),
        msg("user", "a genuinely new turn after the fork", session="child-1"),
    ])
    (rec,) = [r for r in records() if r.session_id == "child-1"]
    assert rec.text == "a genuinely new turn after the fork"
    assert rec.session_id == "child-1" and rec.fork_of == "parent-1"


def test_copied_lines_are_kept_when_the_parent_was_never_scanned(transcripts):
    # Risk A: the parent transcript is gone (removed, other machine), so the
    # fork's copies are the only copy of that history.
    fork = {"sessionId": "gone-parent", "messageUuid": "u-1"}
    transcripts("proj", "child-1", [
        msg("user", "copied from a parent that is no longer here",
            session="child-1", forkedFrom=fork),
        msg("user", "a genuinely new turn after the fork", session="child-1"),
    ])
    got = {r.text: r.fork_of for r in records()}
    assert got == {"copied from a parent that is no longer here": "gone-parent",
                   "a genuinely new turn after the fork": "gone-parent"}


def _fork_with_parent_file(transcripts, make_parent):
    fork = {"sessionId": "parent-1", "messageUuid": "u-1"}
    make_parent()
    transcripts("proj", "child-1", [
        msg("user", "copied from a parent whose file is unusable",
            session="child-1", forkedFrom=fork),
        msg("user", "a genuinely new turn after the fork", session="child-1")])
    return {r.text for r in records() if r.session_id == "child-1"}


def test_copies_are_kept_when_the_parent_file_is_empty(transcripts):
    # Risk: an empty parent file counted as "known" and the copies vanished.
    texts = _fork_with_parent_file(
        transcripts, lambda: transcripts("proj", "parent-1", []))
    assert "copied from a parent whose file is unusable" in texts


def test_copies_are_kept_when_the_parent_file_is_truncated(transcripts):
    def make():
        path = transcripts("proj", "parent-1", [])
        path.write_text('{"type": "user", "sessionId": "parent-1", "mess')
    texts = _fork_with_parent_file(transcripts, make)
    assert "copied from a parent whose file is unusable" in texts


def test_copies_are_kept_when_the_parent_file_cannot_be_opened(transcripts):
    def make():
        # A directory with the right name: listed, but open() fails.
        (transcripts.root / "proj" / "parent-1.jsonl").mkdir(parents=True)
    texts = _fork_with_parent_file(transcripts, make)
    assert "copied from a parent whose file is unusable" in texts


def test_a_parent_holding_only_copies_is_not_known(transcripts):
    def make():
        transcripts("proj", "parent-1", [
            msg("user", "copy inside the parent itself", session="parent-1",
                forkedFrom={"sessionId": "elder-001", "messageUuid": "u"})])
    texts = _fork_with_parent_file(transcripts, make)
    assert "copied from a parent whose file is unusable" in texts


def test_malformed_forked_from_is_treated_as_absent(transcripts):
    bad = ["parent-1", ["parent-1"], {"sessionId": {"x": 1}}, {"sessionId": 7},
           {"sessionId": "a b\x1b[31m"}, {"sessionId": "x" * 300},
           {"sessionId": "short"}, {}]
    transcripts("proj", "child-1", [
        msg("user", f"line {i} that carries an odd forkedFrom value",
            session="child-1", forkedFrom=v) for i, v in enumerate(bad)])
    got = records()
    assert len(got) == len(bad)  # every line kept
    assert {r.fork_of for r in got} == {None}


def test_fork_of_a_fork_names_the_last_copied_line_s_parent(transcripts):
    grand = {"sessionId": "grandparent-1", "messageUuid": "u"}
    direct = {"sessionId": "parent-0001", "messageUuid": "u"}
    transcripts("proj", "grandparent-1", [
        msg("user", "the grandparent's own line here", session="grandparent-1")])
    transcripts("proj", "parent-0001", [
        msg("user", "the parent's own line here", session="parent-0001")])
    transcripts("proj", "child-0001", [
        msg("user", "copied from the grandparent long ago", session="child-0001",
            forkedFrom=grand),
        msg("user", "copied from the direct parent at the fork point",
            session="child-0001", forkedFrom=direct),
        msg("user", "a genuinely new turn after the fork", session="child-0001"),
    ])
    got = [r for r in records() if r.session_id == "child-0001"]
    (rec,) = got
    assert rec.fork_of == "parent-0001"


def test_hostile_labels_are_dropped_and_never_crash_indexing(transcripts):
    transcripts("proj/par/subagents", "agent-a1", [
        msg("assistant", "line with an ANSI escape in its type", session="par",
            isSidechain=True, agentId="ok-id", attributionAgent="\x1b[31mred"),
        msg("assistant", "line with a newline in its type", session="par",
            isSidechain=True, agentId="ok-id", attributionAgent="a\nb"),
        msg("assistant", "line with a dict agent id", session="par",
            isSidechain=True, agentId={"x": 1}, attributionAgent=["t"]),
        msg("assistant", "line with a list agent id", session="par",
            isSidechain=True, agentId=["a"], attributionAgent=5),
        msg("assistant", "line with a 300 character agent id", session="par",
            isSidechain=True, agentId="a" * 300, attributionAgent="t" * 300),
        msg("assistant", "line with a clean pair of labels", session="par",
            isSidechain=True, agentId="a74d:1@x", attributionAgent="job/researcher 2"),
    ])
    got = {r.text.split(" with ")[1]: (r.agent_id, r.agent_type) for r in records()}
    assert got["an ANSI escape in its type"] == ("ok-id", None)
    assert got["a newline in its type"] == ("ok-id", None)
    assert got["a dict agent id"] == (None, None)
    assert got["a list agent id"] == (None, None)
    assert got["a 300 character agent id"] == (None, None)
    assert got["a clean pair of labels"] == ("a74d:1@x", "job/researcher 2")


# ------------------------------------------ brief convention: parent session id

PARENT = "0b1c2d3e-4f50-4a6b-8c7d-9e0f1a2b3c4d"
LINE = f"Parent session id: {PARENT}"


def brief_of(transcripts, messages, name="s", project="proj"):
    transcripts(project, name, messages)
    return {r.text: r.fork_of for r in records()}


def test_brief_line_on_the_first_user_message_links_the_parent(transcripts):
    got = brief_of(transcripts, [
        msg("user", f"{LINE}\nNow research the billing bug in detail."),
        msg("assistant", "on it, reading the billing code now"),
    ], name="child")
    assert set(got.values()) == {PARENT}
    # the line stays in the text as before
    assert any(t.startswith(LINE) for t in got)


def test_brief_tolerates_crlf_trailing_space_and_bom(transcripts):
    for i, text in enumerate([f"{LINE}\r\nthe rest of the brief",
                              f"{LINE}  \t\nthe rest of the brief",
                              f"﻿{LINE}\r\nthe rest of the brief",
                              f"{LINE}\n\nbody text"]):
        got = brief_of(transcripts, [msg("user", text, session=f"c{i}")],
                       name=f"c{i}")
        assert PARENT in got.values(), text


def test_brief_that_must_not_link(transcripts):
    cases = {
        "mid": f"Intro line first\n{LINE}\nmore",
        "later-message": None,
        "case": f"parent session id: {PARENT}\nbody text here",
        "two-spaces": f"Parent session id:  {PARENT}\nbody text here",
        "trailing-text": f"{LINE} and more\nbody text here",
        "leading-space": f" {LINE}\nbody text here",
        "upper": f"Parent session id: {PARENT.upper()}\nbody text here",
        "short": "Parent session id: parent-1\nbody text here",
        "long": f"Parent session id: {PARENT}0\nbody text here",
        "free-text": "Parent session id: sk-secret-token-value-1234567890abcdef\nbody",
        "quoted": f"> {LINE}\nbody text here",
    }
    for name, text in cases.items():
        if text is None:
            msgs = [msg("user", "an ordinary opening message", session=name),
                    msg("user", f"{LINE}\nsecond message", session=name)]
        else:
            msgs = [msg("user", text, session=name)]
        got = brief_of(transcripts, msgs, name=name)
        assert set(got.values()) == {None}, name


def test_quote_in_assistant_text_or_tool_output_does_not_link(transcripts):
    got = brief_of(transcripts, [
        msg("user", "an ordinary opening message"),
        msg("assistant", f"{LINE}\nquoting the convention back"),
        {"type": "user", "sessionId": "sess-1", "cwd": "/work",
         "timestamp": "2026-01-01T00:00:00Z",
         "message": {"content": [{"type": "tool_result",
                                  "content": f"{LINE}\ntool output"}]}},
    ])
    assert set(got.values()) == {None}


def test_brief_on_a_sidechain_or_subagent_file_does_not_link(transcripts):
    got = brief_of(transcripts, [
        msg("user", f"{LINE}\nsidechain prompt", isSidechain=True)])
    assert set(got.values()) == {None}
    transcripts("proj/par/subagents", "agent-a1", [
        msg("user", f"{LINE}\nsubagent prompt text", session="par")])
    assert {r.fork_of for r in records() if r.text.startswith(LINE)
            and "subagent" in r.text} == {None}


def test_brief_naming_the_sessions_own_id_is_ignored(transcripts):
    own = "11111111-2222-4333-8444-555555555555"
    got = brief_of(transcripts, [msg("user", f"Parent session id: {own}\nbody text",
                                     session=own)], name=own)
    assert set(got.values()) == {None}


def test_real_fork_wins_over_the_brief_line(transcripts):
    fork = {"sessionId": "real-parent", "messageUuid": "u"}
    got = brief_of(transcripts, [
        msg("user", "copied from the real parent at the fork point",
            session="c", forkedFrom=fork),
        msg("user", f"{LINE}\nfirst new turn, after the fork", session="c"),
    ], name="c")
    assert set(got.values()) == {"real-parent"}
