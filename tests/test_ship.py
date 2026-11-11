"""`ship`: redacted Records out, idempotent, and nothing lost on a failed upload.

The transport is swapped for a copy into a local directory, so no test opens a
network connection or runs rsync.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import stat

import pytest

from agent_history import config, index, sessions, ship, store, trigger
from conftest import msg

GH = "".join(["gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"])


@pytest.fixture
def hub(tmp_path, monkeypatch):
    """A fake hub: uploads are copied into this directory."""
    landing = tmp_path / "landing"
    landing.mkdir()
    monkeypatch.setenv("AGENT_HISTORY_HUB_INGEST", "ahub@example.invalid:")

    def upload(files, dest, key):
        for f in files:
            shutil.copy(f, landing / f.name)
            f.unlink()
        return True

    monkeypatch.setattr(ship, "upload", upload)
    return landing


def shipped(landing):
    out = []
    for f in sorted(landing.glob("*.jsonl")):
        out += [json.loads(line) for line in f.read_text().splitlines()]
    return out


def test_ship_sends_records_with_the_documented_fields(transcripts, hub):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    assert ship.run() == 0
    (rec,) = shipped(hub)
    assert rec == {
        "v": 1, "machine": "testbox", "agent": "claude-code",
        "origin": rec["origin"], "session_id": "sess-1", "cwd": "/work",
        "timestamp": "2026-01-01T00:00:00Z", "role": "user",
        "text": "a message worth shipping to the hub",
        "session_uid": sessions.derive_uid("claude-code", "testbox", "sess-1"),
    }


def test_ship_redacts_even_when_local_redaction_is_off(transcripts, hub, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_REDACT", "off")
    assert config.redact_enabled() is False
    transcripts("proj", "s", [msg("user", f"push failed, my token is {GH} please fix")])
    ship.run()
    (rec,) = shipped(hub)
    assert GH not in rec["text"]
    assert "[REDACTED:github_token]" in rec["text"]
    # nothing unredacted is left behind in the outbox either
    assert not list(config.outbox_dir().glob("*"))


def test_ship_is_idempotent(transcripts, hub):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    ship.run()
    ship.run()
    assert len(shipped(hub)) == 1
    transcripts("proj", "s2", [msg("user", "a second message, shipped later on")])
    ship.run()
    assert len(shipped(hub)) == 2


def test_injected_recall_is_not_shipped(transcripts, hub):
    transcripts("proj", "s", [msg(
        "user", "Possibly relevant excerpts from past agent sessions: something long")])
    ship.run()
    assert shipped(hub) == []


def test_failed_upload_keeps_everything_and_retries(transcripts, hub, monkeypatch, capsys):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    good = ship.upload
    monkeypatch.setattr(ship, "upload", lambda files, dest, key: False)
    assert ship.run() == 1
    assert shipped(hub) == []
    assert len(list(config.outbox_dir().glob("*.jsonl"))) == 1

    # Still failing: the same record is not written into a second batch.
    assert ship.run() == 1
    assert len(list(config.outbox_dir().glob("*.jsonl"))) == 1

    monkeypatch.setattr(ship, "upload", good)
    assert ship.run() == 0
    assert len(shipped(hub)) == 1
    assert not list(config.outbox_dir().glob("*.jsonl"))
    # and it is now remembered as shipped
    ship.run()
    assert len(shipped(hub)) == 1


def test_a_raising_transport_loses_nothing(transcripts, hub, monkeypatch):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    good = ship.upload

    def boom(files, dest, key):
        raise RuntimeError("network down")

    monkeypatch.setattr(ship, "upload", boom)
    assert ship.run() == 1
    monkeypatch.setattr(ship, "upload", good)
    assert ship.run() == 0
    assert len(shipped(hub)) == 1


def test_state_is_only_marked_shipped_after_the_upload(transcripts, hub, monkeypatch):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    monkeypatch.setattr(ship, "upload", lambda files, dest, key: False)
    ship.run()
    db = sqlite3.connect(config.ship_state_path())
    assert db.execute("SELECT count(*) FROM shipped").fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM queued").fetchone()[0] == 1


def test_batches_are_capped_and_immutably_named(transcripts, hub, monkeypatch):
    monkeypatch.setattr(ship, "BATCH_LINES", 3)
    transcripts("proj", "s", [msg("user", f"message number {i} in a long session",
                                  timestamp=f"2026-01-01T00:00:{i:02d}Z")
                              for i in range(7)])
    ship.run()
    files = sorted(hub.glob("*.jsonl"))
    assert [len(f.read_text().splitlines()) for f in files] == [3, 3, 1]
    assert len({f.name for f in files}) == 3
    assert all(f.name.endswith(".jsonl") and "-" in f.name for f in files)


@pytest.mark.skipif(config.WINDOWS, reason="Windows has no POSIX mode bits")
def test_outbox_and_state_are_owner_only(transcripts, hub, monkeypatch):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    monkeypatch.setattr(ship, "upload", lambda files, dest, key: False)
    ship.run()
    assert stat.S_IMODE(config.outbox_dir().stat().st_mode) == 0o700
    (batch,) = config.outbox_dir().glob("*.jsonl")
    assert stat.S_IMODE(batch.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.ship_state_path().stat().st_mode) == 0o600


def test_dry_run_reports_without_writing_or_uploading(transcripts, hub, capsys, monkeypatch):
    transcripts("proj", "s", [msg("user", f"a message with a token {GH} inside")])
    monkeypatch.setattr(ship, "upload", lambda *a: pytest.fail("uploaded"))
    assert ship.run(dry_run=True) == 0
    out = capsys.readouterr().out
    assert "1 new record(s) would ship" in out
    assert "1 github_token" in out
    assert not config.outbox_dir().exists()
    assert not config.ship_state_path().exists()


def test_ship_needs_a_destination(transcripts, monkeypatch, capsys):
    monkeypatch.delenv("AGENT_HISTORY_HUB_INGEST", raising=False)
    assert ship.run() == 1
    assert "AGENT_HISTORY_HUB_INGEST" in capsys.readouterr().err


def test_an_overlapping_run_is_a_noop(transcripts, hub):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    config.ensure_data_home()
    with trigger._exclusive(config.ship_lock_path()) as held:
        assert held
        assert ship.run() == 0
    assert shipped(hub) == []


def test_record_hash_fields_are_unambiguous():
    a = ship.record_hash("a", "b", "c", "d", "e", "f")
    assert a == ship.record_hash("a", "b", "c", "d", "e", "f")
    assert a != ship.record_hash("a", "bc", "", "d", "e", "f")
    assert a != ship.record_hash("a", "b", "c", "d", "e", "g")


def test_rsync_command_line(monkeypatch, tmp_path):
    seen = {}

    class Done:
        returncode = 0
        stderr = ""

    def fake_run(argv, **kw):
        seen["argv"] = argv
        return Done()

    monkeypatch.setattr(ship.subprocess, "run", fake_run)
    f = tmp_path / "a.jsonl"
    assert ship.upload([f], "ahub@host:", "/home/me/.ssh/hub key")
    argv = seen["argv"]
    assert argv[0] == "rsync" and "--remove-source-files" in argv
    ssh = argv[argv.index("-e") + 1]
    assert "BatchMode=yes" in ssh and "'/home/me/.ssh/hub key'" in ssh
    assert argv[-2:] == [str(f), "ahub@host:"]
    assert not ship.upload([f], "-oProxyCommand=x", None)


# --- backfill ---------------------------------------------------------------

needs_vec = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


@needs_vec
def test_backfill_exports_orphaned_chunks_redacted_with_a_watermark(
        transcripts, fake_embed, hub, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_REDACT", "off")
    path = transcripts("proj", "gone", [msg("user", f"deleted later, token {GH} here")])
    index.run()
    path.unlink()
    db = store.open_for_read()
    assert any(GH in r[0] for r in db.execute("SELECT text FROM chunks")), \
        "the local index keeps the credential"

    ship.run(backfill=True)
    (rec,) = shipped(hub)
    assert GH not in rec["text"] and "[REDACTED:github_token]" in rec["text"]
    assert rec["machine"] == "testbox"

    # Watermark: a second backfill exports nothing, a new chunk exports once.
    ship.run(backfill=True)
    assert len(shipped(hub)) == 1
    transcripts("proj", "new", [msg("user", "a brand new message to index now")])
    index.run()
    ship.run(backfill=True)
    assert len(shipped(hub)) == 2


@needs_vec
def test_backfill_skips_chunks_merged_in_from_other_machines(
        transcripts, fake_embed, hub):
    db, _ = store.open_for_write()
    store.insert_chunk(
        db, agent="codex", origin="/o", session_id="s", cwd="/w",
        timestamp="2026-01-01T00:00:00Z", role="user",
        text="someone else's history, not mine to re-ship", content_hash="h",
        vector=[0.0] * 8, machine="elsewhere")
    db.commit()
    ship.run(backfill=True)
    assert shipped(hub) == []


@needs_vec
def test_backfill_does_not_resend_what_ship_already_sent(
        transcripts, fake_embed, hub, monkeypatch):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    index.run()
    ship.run()
    ship.run(backfill=True)
    assert len(shipped(hub)) == 1


@needs_vec
def test_trigger_ships_after_indexing(transcripts, fake_embed, hub):
    transcripts("proj", "s", [msg("user", "a message worth shipping to the hub")])
    assert trigger.run("hook", foreground=True) == 0
    assert len(shipped(hub)) == 1


def test_subagent_and_fork_fields_ride_the_wire_and_survive_the_inbox(
        transcripts, hub):
    from agent_history.adapters import inbox
    fork = {"sessionId": "parent-1", "messageUuid": "u"}
    transcripts("proj/parent-1/subagents", "agent-a74d", [
        msg("assistant", "subagent output that should be labelled",
            session="parent-1", isSidechain=True, agentId="a74d",
            attributionAgent="job-researcher")])
    transcripts("proj", "child-1", [
        msg("user", "copied line from the parent session", session="child-1",
            forkedFrom=fork),
        msg("user", "a new turn in the forked session", session="child-1")])
    transcripts("proj", "parent-1", [msg(
        "user", "copied line from the parent session", session="parent-1")])
    assert ship.run() == 0
    records = shipped(hub)
    by_text = {r["text"]: r for r in records}
    # the fork's copy is not shipped; the parent's own line is (once)
    assert len(records) == 3
    copies = [r for r in records if r["text"] == "copied line from the parent session"]
    assert [r["session_id"] for r in copies] == ["parent-1"]
    sub = by_text["subagent output that should be labelled"]
    assert (sub["agent_id"], sub["agent_type"], sub["sidechain"]) == (
        "a74d", "job-researcher", True)
    new = by_text["a new turn in the forked session"]
    assert new["fork_of"] == "parent-1"
    assert not {"agent_id", "agent_type", "sidechain"} & new.keys()
    parsed = inbox._parse(json.dumps(sub), "laptop")
    assert (parsed.agent_id, parsed.agent_type, parsed.sidechain) == (
        "a74d", "job-researcher", True)
    assert inbox._parse(json.dumps(new), "laptop").fork_of == "parent-1"


def test_hub_inbox_drops_hostile_labels_and_fork_ids():
    from agent_history.adapters import inbox
    base = {"v": 1, "agent": "claude-code", "origin": "/o", "session_id": "s",
            "text": "a line with odd labels"}
    for bad in ("\x1b[31mred", "a\nb", "x" * 300, {"a": 1}, ["a"], 5, "a b!"):
        rec = inbox._parse(json.dumps({**base, "agent_id": bad,
                                       "agent_type": bad, "fork_of": bad}), "m")
        assert (rec.agent_id, rec.agent_type, rec.fork_of) == (None, None, None), bad
    ok = inbox._parse(json.dumps({**base, "agent_id": "a74d", "agent_type": "job-researcher",
                                  "fork_of": "parent-1"}), "m")
    assert (ok.agent_id, ok.agent_type, ok.fork_of) == (
        "a74d", "job-researcher", "parent-1")


def test_a_v1_line_without_the_new_fields_still_parses():
    from agent_history.adapters import inbox
    old = {"v": 1, "agent": "claude-code", "origin": "/o", "session_id": "s",
           "cwd": "/w", "timestamp": "t", "role": "user", "text": "plain old line"}
    rec = inbox._parse(json.dumps(old), "laptop")
    assert rec.text == "plain old line"
    assert (rec.agent_id, rec.agent_type, rec.sidechain, rec.fork_of) == (
        None, None, False, None)


@needs_vec
def test_backfill_carries_fork_of_from_the_index(transcripts, fake_embed, hub):
    fork = {"sessionId": "parent-1", "messageUuid": "u"}
    paths = [
        transcripts("proj", "parent-1", [
            msg("user", "the parent session question about caching",
                session="parent-1")]),
        transcripts("proj", "child-1", [
            msg("user", "the parent session question about caching",
                session="child-1", forkedFrom=fork),
            msg("user", "a new turn in the forked session", session="child-1")]),
        transcripts("proj", "plain-1", [
            msg("user", "an unrelated standalone session message", session="plain-1")]),
    ]
    index.run()
    for p in paths:  # only the index remains, so the backfill must supply it
        p.unlink()
    ship.run(backfill=True)
    by_text = {r["text"]: r for r in shipped(hub)}
    assert by_text["a new turn in the forked session"]["fork_of"] == "parent-1"
    assert "fork_of" not in by_text["an unrelated standalone session message"]
    assert "fork_of" not in by_text["the parent session question about caching"]


@needs_vec
def test_backfill_of_a_fork_whose_parent_was_never_indexed(
        transcripts, fake_embed, hub):
    # The parent's transcript is gone, so the fork's copied lines are the
    # only record of that history: they must be indexed and shipped.
    fork = {"sessionId": "ghost-parent", "messageUuid": "u"}
    p = transcripts("proj", "orphan-1", [
        msg("user", "copied from a parent we never saw", session="orphan-1",
            forkedFrom=fork),
        msg("user", "a new turn in an orphaned fork", session="orphan-1")])
    index.run()
    p.unlink()
    assert ship.run(backfill=True) == 0
    by_text = {r["text"]: r for r in shipped(hub)}
    assert set(by_text) == {"copied from a parent we never saw",
                            "a new turn in an orphaned fork"}
    assert all("fork_of" not in r for r in by_text.values())


def test_wire_labels_are_capped_redacted_and_never_self_forks(transcripts, hub):
    from agent_history.adapters import inbox
    transcripts("proj/p/subagents", "agent-x", [
        msg("assistant", "subagent text for the label checks here",
            session="p", isSidechain=True, agentId="a" * 300,
            attributionAgent=f"type-{GH}")])
    ship.run()
    (rec,) = shipped(hub)
    assert "agent_id" not in rec  # over the hub's cap: the adapter drops it first (the wire check has its own test)
    assert GH not in rec["agent_type"] and "[REDACTED:github_token]" in rec["agent_type"]
    selfish = {"v": 1, "agent": "claude-code", "origin": "/o", "session_id": "s",
               "text": "a line forking itself", "fork_of": "s"}
    assert inbox._parse(json.dumps(selfish), "laptop").fork_of is None


@needs_vec
def test_backfill_looks_up_each_fork_parent_once(transcripts, fake_embed, hub,
                                                 monkeypatch):
    fork = {"sessionId": "parent-1", "messageUuid": "u"}
    transcripts("proj", "parent-1", [msg("user", "the parent session question", session="parent-1")])
    p = transcripts("proj", "child-1", [
        msg("user", f"fork turn number {n} in the child session", session="child-1")
        for n in range(3)])
    index.run()
    p.unlink()
    calls = []
    real = ship._fork_parent_sid
    monkeypatch.setattr(ship, "_fork_parent_sid",
                        lambda db, uid: calls.append(uid) or real(db, uid))
    ship.run(backfill=True)
    assert len(calls) == len(set(calls))


def test_wire_labels_follow_the_hub_charset_and_cap(tmp_path):
    # Straight into Batches.add: the adapter also cleans these values, so only
    # a direct call proves the wire layer does its own shape check.
    state = ship.State.open(writable=True)
    batches = ship.Batches(state, tmp_path, dry_run=False)
    batches.add(agent="claude-code", origin="/o", session_id="s1",
                cwd="/w", timestamp="2026-01-01T00:00:00Z", role="user",
                text="line with bad labels", agent_id="a" * 65,
                agent_type="bad\nchar", fork_of="short")
    batches.add(agent="claude-code", origin="/o", session_id="s1",
                cwd="/w", timestamp="2026-01-01T00:00:01Z", role="user",
                text="line with good labels", agent_id="a" * 64,
                agent_type="job/researcher 2", fork_of="parent-session-01")
    lines = [json.loads(l) for _, l in batches.pending]
    bad, good = lines
    assert not {"agent_id", "agent_type", "fork_of"} & set(bad)
    assert good["agent_id"] == "a" * 64
    assert good["agent_type"] == "job/researcher 2"
    assert good["fork_of"] == "parent-session-01"
