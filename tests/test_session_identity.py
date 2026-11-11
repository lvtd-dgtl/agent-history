"""Session identity: deterministic uids, aliases, bridge linking, migration,
and uids carried through ship and the hub inbox."""
from __future__ import annotations

import json
import shutil
import sqlite3

import pytest

from agent_history import (cli, config, embed, index, search, sessions, ship,
                           show, store)
from agent_history.adapters.inbox import InboxAdapter
from conftest import msg

pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)

SID = "0b6a5a8e-1111-4222-8333-944455556666"
SUFFIX = "01AbCdEfGhJkLmNpQrStUvWx"


def bridge_line(session=SID, bridge=f"cse_{SUFFIX}"):
    return {"type": "bridge-session", "sessionId": session,
            "bridgeSessionId": bridge, "lastSequenceNum": 0}


def uids_of(db, **where):
    (column, value), = where.items()
    return {row[0] for row in db.execute(
        f"SELECT session_uid FROM chunks WHERE {column} = ?", (value,))}


# ------------------------------------------------------------------ derivation

def test_uid_is_pinned_forever():
    # A regression here would orphan every uid ever shipped to a hub.
    assert sessions.derive_uid("claude-code", "laptop", SID) == \
        "ae59fa24-9b1b-5558-bfa3-a4a96c622359"


def test_uid_depends_on_agent_machine_and_id_only():
    a = sessions.derive_uid("claude-code", "laptop", SID, "/a/x.jsonl")
    assert a == sessions.derive_uid("claude-code", "laptop", SID, "/moved/x.jsonl")
    assert a != sessions.derive_uid("claude-code", "server", SID)
    assert a != sessions.derive_uid("codex", "laptop", SID)
    # None means this machine
    assert sessions.derive_uid("claude-code", None, SID) == \
        sessions.derive_uid("claude-code", "testbox", SID)


def test_pseudo_sessions_are_keyed_by_origin():
    one = sessions.derive_uid("claude-code", "m", "memory:MEMORY",
                              "/p/a/memory/MEMORY.md")
    two = sessions.derive_uid("claude-code", "m", "memory:MEMORY",
                              "/p/b/memory/MEMORY.md")
    assert one != two
    assert sessions.derive_uid("hermes", "m", "", "/x") != \
        sessions.derive_uid("hermes", "m", "", "/y")


def test_reindex_derives_the_same_uids(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "a message that gets indexed twice",
                                  session=SID)])
    index.run()
    before = uids_of(store.open_for_read(), session_id=SID)
    index.run(reindex=True)
    after = uids_of(store.open_for_read(), session_id=SID)
    assert before == after == {sessions.derive_uid("claude-code", "testbox", SID)}


# --------------------------------------------------------------- memory clash

def test_memory_files_of_two_projects_are_two_sessions(transcripts, fake_embed):
    root = transcripts.root
    for project, text in (("proj-a", "alpha project durable memory note"),
                          ("proj-b", "beta project durable memory note")):
        (root / project / "memory").mkdir(parents=True)
        (root / project / "memory" / "MEMORY.md").write_text(text)
    index.run()
    db = store.open_for_read()
    uids = uids_of(db, session_id="memory:MEMORY")
    assert len(uids) == 2, "memory:MEMORY must not collide across projects"
    # The pseudo id alone is ambiguous: no resolution, raw matching as before.
    assert sessions.resolve_session(db, "memory:MEMORY") is None
    assert len(show.rows_for("memory:MEMORY")) == 2
    for uid in uids:
        (row,) = show.rows_for(uid)
        assert row["session_uid"] == uid


# ------------------------------------------------------------------ resolving

def test_resolve_accepts_uid_raw_id_and_alias(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "a resolvable conversation, long",
                                  session=SID)])
    index.run()
    db = store.open_for_write()[0]
    uid = sessions.derive_uid("claude-code", "testbox", SID)
    assert sessions.resolve_session(db, uid) == uid
    assert sessions.resolve_session(db, SID) == uid
    assert sessions.resolve_session(db, f"  {SID}\n") == uid
    store.link_alias(db, uid, "export", "claude-ai-export-uuid")
    db.commit()
    assert store.resolve_session(db, "claude-ai-export-uuid") == uid
    assert sessions.resolve_session(db, "nope") is None
    assert sessions.resolve_session(db, "") is None


def test_unknown_alias_kind_is_refused(fake_embed):
    db, _ = store.open_for_write()
    for kind in ("guess", "fork_of"):  # a fork is not an alias
        with pytest.raises(ValueError):
            sessions.link_alias(db, "x", kind, "y")


def test_forks_are_recorded_in_meta_not_merged(fake_embed):
    db, _ = store.open_for_write()
    parent = sessions.derive_uid("claude-code", "m", "parent")
    child = sessions.derive_uid("claude-code", "m", "child")
    for uid, sid in ((parent, "parent"), (child, "child")):
        sessions.register_session(db, uid, agent="claude-code", machine="m",
                                  session_id=sid, origin="/x")
    sessions.link_fork(db, child, parent)
    sessions.link_fork(db, child, parent)  # idempotent

    def meta(uid):
        return json.loads(db.execute("SELECT meta FROM sessions WHERE uid = ?",
                                     (uid,)).fetchone()[0])
    assert meta(child)["fork_of"] == parent
    assert meta(parent)["forks"] == [child]
    assert sessions.resolve_session(db, "child") == child


# ------------------------------------------------------------------- bridging

def test_bridge_line_links_the_remote_control_ids(transcripts, fake_embed):
    transcripts("proj", "s", [
        msg("user", "a remote controlled session message", session=SID),
        bridge_line(),  # Claude Code writes it once connected, mid-file
        msg("assistant", "the reply that came over the bridge", session=SID),
    ])
    index.run()
    db = store.open_for_read()
    uid = sessions.derive_uid("claude-code", "testbox", SID)
    for alias in (f"cse_{SUFFIX}", f"session_{SUFFIX}"):
        assert sessions.resolve_session(db, alias) == uid
    assert sessions.links_of(db, uid) == [("bridge", f"session_{SUFFIX}"),
                                          ("cse", f"cse_{SUFFIX}")]
    rows = show.rows_for(f"session_{SUFFIX}")
    assert [r["role"] for r in rows] == ["user", "assistant"]


def test_sessions_json_bridge_id_is_evidence_too(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "a session named in sessions json",
                                  session=SID)])
    sessions_dir = transcripts.root.parent / "sessions"
    sessions_dir.mkdir()
    (sessions_dir / "4242.json").write_text(json.dumps(
        {"pid": 4242, "sessionId": SID, "bridgeSessionId": f"cse_{SUFFIX}"}))
    index.run()
    db = store.open_for_read()
    assert sessions.resolve_session(db, f"session_{SUFFIX}") == \
        sessions.derive_uid("claude-code", "testbox", SID)


def test_two_sessions_on_one_bridge_merge_earliest_first(transcripts, fake_embed):
    teleported = "99999999-aaaa-4bbb-8ccc-dddddddddddd"
    transcripts("proj", "late", [
        msg("user", "the teleported copy continues the talk", session=teleported,
            timestamp="2026-02-01T00:00:00Z"),
        bridge_line(session=teleported),
    ])
    transcripts("proj", "early", [
        msg("user", "the original start of the conversation", session=SID,
            timestamp="2026-01-01T00:00:00Z"),
        bridge_line(),
    ])
    index.run()
    db = store.open_for_read()
    early = sessions.derive_uid("claude-code", "testbox", SID)
    late = sessions.derive_uid("claude-code", "testbox", teleported)
    # chunks keep the uid they were written with...
    assert uids_of(db, session_id=teleported) == {late}
    # ...and every id resolves to the earliest
    for any_id in (early, late, SID, teleported, f"cse_{SUFFIX}"):
        assert sessions.resolve_session(db, any_id) == early
    rows = show.rows_for(teleported)
    assert [r["session_id"] for r in rows] == [SID, teleported]
    assert {r["session_uid"] for r in rows} == {early}
    # re-running is a no-op
    index.run()
    assert sessions.resolve_session(store.open_for_read(), late) == early


def test_a_session_indexed_under_its_bridge_id_is_merged(fake_embed):
    db, _ = store.open_for_write()
    cloud = sessions.derive_uid("claude-code", "testbox", f"session_{SUFFIX}")
    sessions.register_session(db, cloud, agent="claude-code", machine="testbox",
                              session_id=f"session_{SUFFIX}", origin="/cloud",
                              timestamp="2026-03-01T00:00:00Z")
    local = sessions.derive_uid("claude-code", "testbox", SID)
    sessions.register_session(db, local, agent="claude-code", machine="testbox",
                              session_id=SID, origin="/local",
                              timestamp="2026-01-01T00:00:00Z")
    assert sessions.link_bridge(db, local, f"cse_{SUFFIX}") == local
    assert sessions.resolve_session(db, cloud) == local


def test_source_ids_never_merge(fake_embed):
    db, _ = store.open_for_write()
    for machine in ("a", "b"):
        uid = sessions.derive_uid("claude-code", machine, SID)
        sessions.register_session(db, uid, agent="claude-code", machine=machine,
                                  session_id=SID, origin="/x")
    assert db.execute("SELECT count(*) FROM session_aliases "
                      "WHERE kind = 'uid'").fetchone()[0] == 0


# ------------------------------------------------------------------- outputs

def test_search_json_and_show_carry_the_uid(transcripts, fake_embed, capsys):
    transcripts("proj", "s", [msg("user", "deploy pipeline broke on friday",
                                  session=SID)])
    index.run()
    uid = sessions.derive_uid("claude-code", "testbox", SID)
    (hit,) = search.search_rows("deploy pipeline", mode="text")
    assert (hit["session_id"], hit["session_uid"]) == (SID, uid)
    capsys.readouterr()
    assert show.run(SID, as_json=True) == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert row["session_uid"] == uid
    assert show.run(uid) == 0
    assert f"uid: {uid}" in capsys.readouterr().out


def test_purge_by_alias(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "something to forget entirely", session=SID),
                              bridge_line()])
    index.run()
    db = store.open_for_write()[0]
    assert len(store.purge_targets(db, session=f"session_{SUFFIX}")) == 1


def test_purge_preview_lists_every_linked_id(transcripts, fake_embed, capsys):
    teleported = "99999999-aaaa-4bbb-8ccc-dddddddddddd"
    transcripts("proj", "early", [
        msg("user", "the original start of the conversation", session=SID,
            timestamp="2026-01-01T00:00:00Z"), bridge_line()])
    transcripts("proj", "late", [
        msg("user", "the teleported copy continues the talk", session=teleported,
            timestamp="2026-02-01T00:00:00Z"), bridge_line(session=teleported)])
    index.run()
    capsys.readouterr()
    assert cli.main(["purge", "--session", SID]) == 0
    out = capsys.readouterr().out
    for linked in (sessions.derive_uid("claude-code", "testbox", SID),
                   sessions.derive_uid("claude-code", "testbox", teleported),
                   SID, teleported, f"cse_{SUFFIX}", f"session_{SUFFIX}"):
        assert linked in out
    assert "would delete 2 chunk(s)" in out
    # the preview deleted nothing
    db = store.open_for_read()
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 2


def test_new_chunks_record_their_provenance(transcripts, fake_embed):
    # long enough to be cut into several chunks, none of them identical
    text = " ".join(f"word{n}" for n in range(1500))
    transcripts("proj", "s", [msg("user", text, session=SID)])
    index.run()
    rows = store.open_for_read().execute(
        "SELECT record_id, chunk_ordinal, char_start, char_end, text "
        "FROM chunks ORDER BY chunk_ordinal").fetchall()
    assert len(rows) > 1
    assert len({r[0] for r in rows}) == 1 and rows[0][0]
    assert [r[1] for r in rows] == list(range(len(rows)))
    for _rid, _n, start, end, piece in rows:
        assert text[start:end] == piece


# ------------------------------------------------------------------ migration

OLD_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE chunks (
    id INTEGER PRIMARY KEY, agent TEXT NOT NULL DEFAULT 'claude-code',
    origin TEXT, session_id TEXT, cwd TEXT, timestamp TEXT, role TEXT,
    text TEXT, content_hash TEXT UNIQUE);
CREATE INDEX idx_chunks_origin ON chunks(origin);
CREATE INDEX idx_chunks_agent  ON chunks(agent);
CREATE VIRTUAL TABLE vec_chunks USING vec0(embedding float[8]);
CREATE VIRTUAL TABLE fts_chunks USING fts5(text, content='chunks', content_rowid='id');
INSERT INTO meta VALUES ('model', 'nomic-embed-text'), ('dimension', '8');
"""


def build_old_index(rows):
    """A 0.16-shaped index (no machine, no session identity) with vectors."""
    config.ensure_data_home()
    db = store._raw_connect(config.db_path())
    db.executescript(OLD_SCHEMA)
    for n, (agent, origin, sid, ts) in enumerate(rows, 1):
        text = f"chunk {n} of {sid} from {origin}"
        db.execute("INSERT INTO chunks (id, agent, origin, session_id, cwd, "
                   "timestamp, role, text, content_hash) VALUES (?,?,?,?,?,?,?,?,?)",
                   (n, agent, origin, sid, "/w", ts, "user", text, f"h{n}"))
        db.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES (?, ?)",
                   (n, embed.pack([n / 10] * 8)))
        db.execute("INSERT INTO fts_chunks(rowid, text) VALUES (?, ?)", (n, text))
    db.commit()
    db.close()


OLD_ROWS = [
    ("claude-code", "/p/a/s.jsonl", SID, "2026-01-01T00:00:00Z"),
    ("claude-code", "/p/a/s.jsonl", SID, "2026-01-02T00:00:00Z"),
    ("claude-code", "/p/a/memory/MEMORY.md", "memory:MEMORY", "2026-01-03"),
    ("claude-code", "/p/b/memory/MEMORY.md", "memory:MEMORY", "2026-01-04"),
    ("codex", "/c/r.jsonl", "codex-1", ""),
]


def test_migration_backfills_uids_and_loses_nothing(fake_embed):
    build_old_index(OLD_ROWS)
    db = store.open_for_read()
    columns = {row[1] for row in db.execute("PRAGMA table_info(chunks)")}
    assert {"machine", "session_uid", "record_id", "chunk_ordinal",
            "char_start", "char_end"} <= columns
    rows = db.execute("SELECT id, agent, origin, session_id, text, session_uid, "
                      "machine, record_id FROM chunks ORDER BY id").fetchall()
    assert len(rows) == len(OLD_ROWS)
    for (rowid, agent, origin, sid, text, uid, machine, rid), old in zip(rows, OLD_ROWS):
        assert (agent, origin, sid) == old[:3]
        assert text == f"chunk {rowid} of {sid} from {origin}"
        assert uid == sessions.derive_uid(agent, "testbox", sid, origin)
        assert machine is None and rid is None  # untouched / unknowable
    assert db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0] == len(OLD_ROWS)
    assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 4
    uid = sessions.derive_uid("claude-code", "testbox", SID)
    assert db.execute("SELECT first_seen, last_seen, machine FROM sessions "
                      "WHERE uid = ?", (uid,)).fetchone() == \
        ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00Z", None)
    assert sessions.resolve_session(db, SID) == uid
    assert sessions.resolve_session(db, "codex-1") is not None
    # full-text still finds the old rows
    assert store.text_query(db, "chunk", 10)
    db.close()
    # a second open has nothing left to do and changes nothing
    again = store.open_for_read()
    assert again.execute("SELECT count(*) FROM chunks WHERE session_uid IS NULL"
                         ).fetchone()[0] == 0
    assert again.execute("SELECT count(*) FROM sessions").fetchone()[0] == 4


def test_rows_written_later_by_an_older_version_are_picked_up(fake_embed):
    build_old_index(OLD_ROWS[:1])
    store.open_for_read().close()
    raw = sqlite3.connect(config.db_path())  # an old writer: no uid column known
    raw.execute("INSERT INTO chunks (agent, origin, session_id, timestamp, text, "
                "content_hash) VALUES ('codex', '/c', 'late', '2026', 't', 'hx')")
    raw.commit()
    raw.close()
    db = store.open_for_read()
    assert uids_of(db, session_id="late") == {
        sessions.derive_uid("codex", "testbox", "late")}
    assert sessions.resolve_session(db, "late")


# ----------------------------------------------------------------- ship / hub

@pytest.fixture
def hub(tmp_path, monkeypatch):
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
    return [json.loads(line) for f in sorted(landing.glob("*.jsonl"))
            for line in f.read_text().splitlines()]


def test_ship_carries_uid_and_bridge_aliases(transcripts, hub):
    transcripts("proj", "s", [msg("user", "a bridged message worth shipping",
                                  session=SID), bridge_line()])
    ship.run()
    (rec,) = shipped(hub)
    assert rec["session_uid"] == sessions.derive_uid("claude-code", "testbox", SID)
    assert rec["aliases"] == [["cse", f"cse_{SUFFIX}"]]


def test_backfill_from_index_carries_stored_uids_and_links(transcripts, hub, fake_embed):
    transcripts("proj", "s", [msg("user", "an indexed bridged message here",
                                  session=SID), bridge_line()])
    index.run()
    (transcripts.root / "proj" / "s.jsonl").unlink()  # the index is the only copy
    ship.run(backfill=True)
    (rec,) = shipped(hub)
    assert rec["session_uid"] == sessions.derive_uid("claude-code", "testbox", SID)
    assert rec["aliases"] == [["bridge", f"session_{SUFFIX}"],
                              ["cse", f"cse_{SUFFIX}"]]


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    path = tmp_path / "inbox"
    path.mkdir()
    monkeypatch.setenv("AGENT_HISTORY_INBOX", str(path))
    monkeypatch.setenv("AGENT_HISTORY_HUB", "1")
    return path


def drop(inbox, machine, *records):
    d = inbox / machine
    d.mkdir(parents=True, exist_ok=True)
    n = len(list(d.glob("*.jsonl")))
    (d / f"b{n}.jsonl").write_text(
        "\n".join(json.dumps({"v": 1, "agent": "claude-code", "origin": "/o.jsonl",
                              "session_id": SID, "cwd": "/w",
                              "timestamp": "2026-01-01T00:00:00Z", "role": "user",
                              **r}) for r in records) + "\n")


def test_hub_keeps_the_shippers_uid_and_links(inbox, fake_embed):
    claimed = sessions.derive_uid("claude-code", "laptop", SID)
    drop(inbox, "laptop", {"text": "shipped with its own identity, long",
                           "session_uid": claimed,
                           "aliases": [["cse", f"cse_{SUFFIX}"], ["uid", "evil"],
                                       ["source", "x"], ["fork_of", "y"]]})
    (rec,) = list(InboxAdapter().records())
    assert rec.aliases == (("cse", f"cse_{SUFFIX}"),), "uid/source are hub-only"
    index.run()
    db = store.open_for_read()
    assert uids_of(db, session_id=SID) == {claimed}
    assert sessions.resolve_session(db, f"session_{SUFFIX}") == claimed
    assert db.execute("SELECT machine FROM sessions WHERE uid = ?",
                      (claimed,)).fetchone() == ("laptop",)


def test_hub_derives_from_the_inbox_machine_not_the_claim(inbox, fake_embed):
    # The shipper calls itself "dennis-laptop"; its inbox directory is "laptop".
    # Only the directory is authenticated, so the claim is ignored.
    drop(inbox, "laptop", {"text": "shipped under a different name, long",
                           "session_uid": sessions.derive_uid(
                               "claude-code", "dennis-laptop", SID)})
    index.run()
    assert uids_of(store.open_for_read(), session_id=SID) == {
        sessions.derive_uid("claude-code", "laptop", SID)}


def test_a_uid_cannot_be_squatted_before_its_owner_ships(inbox, fake_embed):
    victim = sessions.derive_uid("claude-code", "server", SID)
    drop(inbox, "laptop", {"text": "laptop claims the server's uid first",
                           "session_uid": victim})
    index.run()
    drop(inbox, "server", {"text": "the server's own conversation, later"})
    index.run()
    db = store.open_for_read()
    assert uids_of(db, machine="laptop") == {
        sessions.derive_uid("claude-code", "laptop", SID)}
    assert uids_of(db, machine="server") == {victim}


def test_hub_derives_a_uid_for_old_format_records(inbox, fake_embed):
    drop(inbox, "laptop", {"text": "an old shipper sent no identity at all"})
    drop(inbox, "laptop", {"text": "and a malformed uid is ignored too, ok",
                           "session_uid": "not-a-uuid", "aliases": "junk"})
    index.run()
    db = store.open_for_read()
    assert uids_of(db, session_id=SID) == {
        sessions.derive_uid("claude-code", "laptop", SID)}


def test_hub_ignores_a_uid_claimed_from_another_machine(inbox, fake_embed):
    victim = sessions.derive_uid("claude-code", "server", SID)
    drop(inbox, "server", {"text": "the server's own conversation, long"})
    index.run()
    drop(inbox, "laptop", {"text": "laptop tries to file under the server",
                           "session_uid": victim})
    index.run()
    db = store.open_for_read()
    assert uids_of(db, machine="laptop") == {
        sessions.derive_uid("claude-code", "laptop", SID)}
    assert uids_of(db, machine="server") == {victim}


# ------------------------------------------------- subagent labels and forks

def test_fork_is_linked_and_copied_lines_stay_with_the_parent(transcripts,
                                                               fake_embed):
    transcripts("proj", "parent-1", [msg("user", "the original question about caching",
                                       session="parent-1")])
    fork = {"sessionId": "parent-1", "messageUuid": "u"}
    transcripts("proj", "child-1", [
        msg("user", "the original question about caching", session="child-1",
            forkedFrom=fork),
        msg("user", "a new direction after branching off", session="child-1"),
    ])
    index.run()
    index.run()  # re-linking is idempotent
    db = store.open_for_read()
    parent = sessions.derive_uid("claude-code", "testbox", "parent-1")
    child = sessions.derive_uid("claude-code", "testbox", "child-1")

    def meta(uid):
        return json.loads(db.execute("SELECT meta FROM sessions WHERE uid = ?",
                                     (uid,)).fetchone()[0])
    assert meta(child)["fork_of"] == parent
    assert meta(parent)["forks"] == [child]
    texts = {r[0]: r[1] for r in db.execute(
        "SELECT text, session_id FROM chunks")}
    assert texts["the original question about caching"] == "parent-1"
    assert texts["a new direction after branching off"] == "child-1"


def test_orphaned_fork_text_is_indexed_and_still_linked_later(transcripts,
                                                              fake_embed):
    # Risk A end to end: the parent was never indexed, so nothing is lost;
    # when it shows up on a later run the link is made both ways.
    transcripts("proj", "child-1", [
        msg("user", "history copied from a parent we never indexed",
            session="child-1",
            forkedFrom={"sessionId": "ghost-parent", "messageUuid": "u"}),
        msg("user", "a new direction after branching off", session="child-1")])
    index.run()
    texts = {r[0] for r in store.open_for_read().execute("SELECT text FROM chunks")}
    assert "history copied from a parent we never indexed" in texts
    transcripts("proj", "ghost-parent", [
        msg("user", "the parent's own later question here", session="ghost-parent")])
    index.run()
    db = store.open_for_read()
    parent = sessions.derive_uid("claude-code", "testbox", "ghost-parent")
    child = sessions.derive_uid("claude-code", "testbox", "child-1")
    meta = lambda uid: json.loads(db.execute(
        "SELECT meta FROM sessions WHERE uid = ?", (uid,)).fetchone()[0])
    assert meta(child)["fork_of"] == parent
    assert meta(parent)["forks"] == [child]


def test_parent_deleted_between_runs_keeps_one_copy_with_the_parent(
        transcripts, fake_embed):
    transcripts("proj", "parent-1", [
        msg("user", "the original question about caching", session="parent-1")])
    transcripts("proj", "child-1", [
        msg("user", "the original question about caching", session="child-1",
            forkedFrom={"sessionId": "parent-1", "messageUuid": "u"}),
        msg("user", "a new direction after branching off", session="child-1")])
    index.run()
    (transcripts.root / "proj" / "parent-1.jsonl").unlink()
    index.run()  # the copy is no longer dropped, but its text is already held
    rows = store.open_for_read().execute(
        "SELECT text, session_id FROM chunks").fetchall()
    assert sorted(rows) == [("a new direction after branching off", "child-1"),
                            ("the original question about caching", "parent-1")]


def test_a_fork_without_chunks_of_its_own_gets_no_session_row(transcripts,
                                                              fake_embed):
    # The parent was indexed, then deleted; the fork has only copies, all of
    # whose text is held by the parent's chunks: no empty session row.
    transcripts("proj", "parent-1", [
        msg("user", "the original question about caching", session="parent-1")])
    child = transcripts("proj", "child-1", [
        msg("user", "the original question about caching", session="child-1",
            forkedFrom={"sessionId": "parent-1", "messageUuid": "u"})])
    index.run()
    (transcripts.root / "proj" / "parent-1.jsonl").unlink()
    index.run()
    db = store.open_for_read()
    uid = sessions.derive_uid("claude-code", "testbox", "child-1")
    assert db.execute("SELECT count(*) FROM sessions WHERE uid = ?",
                      (uid,)).fetchone()[0] == 0
    assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 1


def test_a_locked_database_is_reported_once_per_pass_and_skipped(transcripts, fake_embed,
                                                        monkeypatch, capsys):
    import sqlite3
    for n in range(3):
        transcripts("proj", f"child-0{n}", [
            msg("user", f"a new direction number {n} after branching",
                session=f"child-0{n}",
                forkedFrom={"sessionId": "parent-01", "messageUuid": "u"})])

    def locked(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(sessions, "link_fork", locked)
    assert index.run() == 0
    err = capsys.readouterr().err
    # once per linking pass (before and after embedding), not once per link
    assert err.count("database is locked") == 2


def test_an_unexpected_error_in_a_fork_link_is_loud_but_never_blocks_indexing(
        transcripts, fake_embed, monkeypatch, capsys):
    transcripts("proj", "child-1", [
        msg("user", "a new direction after branching off", session="child-1",
            forkedFrom={"sessionId": "parent-01", "messageUuid": "u"})])
    monkeypatch.setattr(sessions, "link_fork",
                        lambda *a, **k: (_ for _ in ()).throw(KeyError("x")))
    assert index.run() == 0
    err = capsys.readouterr().err
    assert "ERROR in a fork link" in err and "KeyError" in err
    assert store.open_for_read().execute(
        "SELECT count(*) FROM chunks").fetchone()[0] == 1


def test_an_aliasless_record_does_not_shadow_a_later_aliased_one(inbox, fake_embed):
    claimed = sessions.derive_uid("claude-code", "laptop", SID)
    drop(inbox, "laptop",
         {"text": "first line, a fork-only record without aliases",
          "session_uid": claimed, "fork_of": "parent-session-01"},
         {"text": "second line of the same session carrying the link",
          "session_uid": claimed, "aliases": [["cse", f"cse_{SUFFIX}"]]})
    index.run()
    db = store.open_for_read()
    assert sessions.resolve_session(db, f"session_{SUFFIX}") == claimed


def test_one_bad_fork_link_does_not_abort_the_run(transcripts, fake_embed,
                                                  monkeypatch, capsys):
    transcripts("proj", "child-1", [
        msg("user", "a new direction after branching off", session="child-1",
            forkedFrom={"sessionId": "parent-01", "messageUuid": "u"})])
    transcripts("proj", "other-01", [
        msg("user", "an unrelated session with its own text", session="other-01")])

    def boom(*a, **k):
        raise ValueError("bad link")
    monkeypatch.setattr(sessions, "link_fork", boom)
    assert index.run() == 0
    assert "skipped a fork link" in capsys.readouterr().err
    assert store.open_for_read().execute(
        "SELECT count(*) FROM chunks").fetchone()[0] == 2


def _fork_db():
    db, _ = store.open_for_write()
    uids = {}
    for sid in ("parent-1", "child-1", "child-2"):
        uids[sid] = sessions.derive_uid("claude-code", "m", sid)
        sessions.register_session(db, uids[sid], agent="claude-code", machine="m",
                                  session_id=sid, origin="/x")
    meta = lambda uid: json.loads(db.execute(
        "SELECT meta FROM sessions WHERE uid = ?", (uid,)).fetchone()[0])
    return db, uids, meta


def test_link_fork_caps_the_forks_list(fake_embed):
    db, uids, meta = _fork_db()
    sessions.update_meta(db, uids["parent-1"],
                         forks=[f"u{i}" for i in range(sessions.MAX_FORKS)])
    sessions.link_fork(db, uids["child-1"], uids["parent-1"])
    assert len(meta(uids["parent-1"])["forks"]) == sessions.MAX_FORKS
    assert uids["child-1"] not in meta(uids["parent-1"])["forks"]


def test_link_fork_writes_nothing_when_nothing_changed(fake_embed):
    db, uids, meta = _fork_db()
    sessions.link_fork(db, uids["child-1"], uids["parent-1"])
    seen = []
    db.set_trace_callback(lambda q: seen.append(q))
    sessions.link_fork(db, uids["child-1"], uids["parent-1"])
    assert not [q for q in seen if q.lstrip().upper().startswith("UPDATE")]


def test_link_fork_without_a_child_row_leaves_the_parent_alone(fake_embed):
    db, uids, meta = _fork_db()
    sessions.link_fork(db, "no-such-child-uid", uids["parent-1"])
    assert "forks" not in meta(uids["parent-1"])


def test_link_fork_first_writer_wins(fake_embed):
    db, uids, meta = _fork_db()
    sessions.link_fork(db, uids["child-1"], uids["parent-1"])
    sessions.link_fork(db, uids["child-1"], uids["child-2"])
    assert meta(uids["child-1"])["fork_of"] == uids["parent-1"]
    assert "forks" not in meta(uids["child-2"])


def test_subagent_chunks_are_labelled_in_search_show_and_mcp_rows(
        transcripts, fake_embed, capsys):
    transcripts("proj/par/subagents", "agent-a74d", [
        msg("assistant", "subagent reports the flaky migration is fixed",
            session="par", isSidechain=True, agentId="a74d",
            attributionAgent="job-researcher")])
    transcripts("proj", "par", [msg("assistant", "main agent says the migration ok",
                                    session="par")])
    index.run()
    hits = {h["text"]: h for h in search.search_rows("migration", mode="text")}
    sub = hits["subagent reports the flaky migration is fixed"]
    assert (sub["sidechain"], sub["agent_id"], sub["agent_type"]) == (
        True, "a74d", "job-researcher")
    assert hits["main agent says the migration ok"]["sidechain"] is False
    assert search.sub_label(sub) == " [sub:job-researcher a74d]"
    assert search.sub_label(hits["main agent says the migration ok"]) == ""
    search._print_row(1, sub, False)
    assert "claude-code/assistant [sub:job-researcher a74d]" in capsys.readouterr().out
    assert show.run("par") == 0
    out = capsys.readouterr().out
    assert "assistant [sub:job-researcher a74d]:" in out
    assert "assistant:" in out  # the main-session chunk has no prefix


def test_migration_adds_subagent_columns_to_an_existing_index(fake_embed):
    build_old_index(OLD_ROWS[:1])
    db = store.open_for_read()
    columns = {row[1] for row in db.execute("PRAGMA table_info(chunks)")}
    assert {"agent_id", "agent_type", "sidechain"} <= columns
    (row,) = store.session_rows(db, session_id=SID)
    assert row[store.ROW_WIDTH - 3:] == (None, None, 0)


def test_fork_and_parent_in_one_run_link_both_ways(transcripts, fake_embed):
    """The parent's row does not exist until its chunks are written, so the
    link must be applied after embedding, not only before."""
    transcripts("proj", "parent-1", [msg("user", "the original question about caching",
                                       session="parent-1")])
    transcripts("proj", "child-1", [
        msg("user", "a new direction after branching off", session="child-1",
            forkedFrom={"sessionId": "parent-1", "messageUuid": "u"}),
        msg("user", "and one more fresh turn on the fork", session="child-1")])
    # first line of the fork is forkedFrom: skipped, so only the second remains
    index.run()  # ONE run
    db = store.open_for_read()
    parent = sessions.derive_uid("claude-code", "testbox", "parent-1")
    child = sessions.derive_uid("claude-code", "testbox", "child-1")
    meta = lambda uid: json.loads(db.execute(
        "SELECT meta FROM sessions WHERE uid = ?", (uid,)).fetchone()[0])
    assert meta(child)["fork_of"] == parent
    assert meta(parent)["forks"] == [child]
    assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 2


def test_a_fork_of_itself_is_ignored(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "a line that claims to fork itself ok",
                                  session="s",
                                  forkedFrom={"sessionId": "s", "messageUuid": "u"})])
    index.run()
    db = store.open_for_read()
    uid = sessions.derive_uid("claude-code", "testbox", "s")
    assert "fork_of" not in json.loads(db.execute(
        "SELECT meta FROM sessions WHERE uid = ?", (uid,)).fetchone()[0])
    assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 1  # text kept


def test_relabel_previews_then_fills_old_subagent_chunks(transcripts, fake_embed,
                                                         capsys):
    from agent_history import relabel
    path = transcripts("proj/par/subagents", "agent-a74d", [
        msg("assistant", "subagent reports the flaky migration is fixed",
            session="par", isSidechain=True, agentId="a74d",
            attributionAgent="job-researcher")])
    transcripts("proj", "par", [msg("assistant", "main agent says migration ok",
                                    session="par")])
    index.run()
    db = store.open_for_write()[0]
    db.execute("UPDATE chunks SET agent_id = NULL, agent_type = NULL, "
               "sidechain = NULL")  # as indexed by an older version
    db.commit()
    db.close()

    assert relabel.run() == 0  # preview
    assert "would relabel 1 chunk(s) in 1 subagent transcript(s)" in capsys.readouterr().out
    db = store.open_for_read()
    assert db.execute("SELECT count(*) FROM chunks WHERE agent_id IS NOT NULL"
                      ).fetchone()[0] == 0

    assert relabel.run(apply=True) == 0
    assert "relabelled 1 chunk(s)" in capsys.readouterr().out
    rows = dict(((t, (a, ty, s)) for t, a, ty, s in store.open_for_read().execute(
        "SELECT text, agent_id, agent_type, sidechain FROM chunks")))
    assert rows["subagent reports the flaky migration is fixed"] == (
        "a74d", "job-researcher", 1)
    assert rows["main agent says migration ok"] == (None, None, None)
    # without the transcript the id is still derived; the type stays unknown
    path.unlink()
    db = store.open_for_write()[0]
    db.execute("UPDATE chunks SET agent_id = NULL, agent_type = NULL, sidechain = NULL")
    db.commit()
    db.close()
    relabel.run(apply=True)
    row = store.open_for_read().execute(
        "SELECT agent_id, agent_type, sidechain FROM chunks WHERE origin LIKE '%agent-a74d%'"
    ).fetchone()
    assert row == ("a74d", None, 1)


# ---------------------------------------- brief convention: parent session id

def _ids(n):
    return [f"{i:08x}-1111-4222-8333-444444444444" for i in range(1, n + 1)]


def _meta(db, sid):
    uid = sessions.derive_uid("claude-code", "testbox", sid)
    return json.loads(db.execute("SELECT meta FROM sessions WHERE uid = ?",
                                 (uid,)).fetchone()[0])


def test_brief_parent_links_even_when_it_is_indexed_later(transcripts, fake_embed):
    parent, child = _ids(2)
    transcripts("proj", child, [msg(
        "user", f"Parent session id: {parent}\nresearch the billing bug", session=child)])
    index.run()  # parent not indexed yet: no error, link applied on arrival
    transcripts("proj", parent, [msg("user", "the original question", session=parent)])
    index.run()
    db = store.open_for_read()
    assert _meta(db, child)["fork_of"] == sessions.derive_uid(
        "claude-code", "testbox", parent)
    assert _meta(db, parent)["forks"] == [sessions.derive_uid(
        "claude-code", "testbox", child)]


def test_brief_cycle_does_not_hang_and_a_real_fork_is_not_overwritten(
        transcripts, fake_embed, capsys):
    a, b, c = _ids(3)
    transcripts("proj", a, [msg("user", f"Parent session id: {b}\nfrom a", session=a)])
    transcripts("proj", b, [msg("user", f"Parent session id: {a}\nfrom b", session=b)])
    fork = {"sessionId": c, "messageUuid": "u"}
    transcripts("proj", "d", [
        msg("user", "copied from the parent line", session="d", forkedFrom=fork),
        msg("user", f"Parent session id: {a}\nnew turn here", session="d")])
    index.run()
    index.run()
    db = store.open_for_read()
    uid = lambda s: sessions.derive_uid("claude-code", "testbox", s)
    assert _meta(db, a)["fork_of"] == uid(b)
    assert _meta(db, b)["fork_of"] == uid(a)
    assert _meta(db, "d")["fork_of"] == uid(c)  # forkedFrom wins


def test_link_fork_keeps_an_existing_different_parent(fake_embed):
    db, _ = store.open_for_write()
    uid = lambda s: sessions.derive_uid("claude-code", "m", s)
    for sid in ("a", "b", "c"):
        sessions.register_session(db, uid(sid), agent="claude-code", machine="m",
                                  session_id=sid, origin="/x")
    sessions.link_fork(db, uid("a"), uid("b"))
    sessions.link_fork(db, uid("a"), uid("c"))
    meta = json.loads(db.execute("SELECT meta FROM sessions WHERE uid = ?",
                                 (uid("a"),)).fetchone()[0])
    assert meta["fork_of"] == uid("b")


def _unlabel(db_rows_sql="UPDATE chunks SET agent_id = NULL, agent_type = NULL, "
                          "sidechain = NULL"):
    db = store.open_for_write()[0]
    db.execute(db_rows_sql)
    db.commit()
    db.close()


def _subagent_index(transcripts):
    path = transcripts("proj/par/subagents", "agent-a74d", [
        msg("assistant", "subagent reports the flaky migration is fixed",
            session="par", isSidechain=True, agentId="a74d",
            attributionAgent="job-researcher")])
    index.run()
    return path


def test_relabel_dry_run_writes_nothing_and_second_apply_is_a_no_op(
        transcripts, fake_embed, capsys):
    from agent_history import relabel
    _subagent_index(transcripts)
    _unlabel()
    snap = lambda: store.open_for_read().execute(
        "SELECT agent_id, agent_type, sidechain FROM chunks").fetchall()
    before = snap()
    relabel.run()
    assert snap() == before
    capsys.readouterr()
    relabel.run(apply=True)
    assert "relabelled 1 chunk(s)" in capsys.readouterr().out
    relabel.run(apply=True)
    assert "relabelled 0 chunk(s) in 0" in capsys.readouterr().out


def test_relabel_fills_a_missing_type_on_a_later_run(transcripts, fake_embed,
                                                     capsys):
    from agent_history import relabel
    _subagent_index(transcripts)
    _unlabel("UPDATE chunks SET agent_type = NULL")  # id and flag already set
    relabel.run(apply=True)
    assert "relabelled 1 chunk(s)" in capsys.readouterr().out
    assert store.open_for_read().execute(
        "SELECT agent_type FROM chunks").fetchone()[0] == "job-researcher"


def test_relabel_never_reads_files_for_other_machines_rows(transcripts,
                                                           fake_embed):
    from agent_history import relabel
    _subagent_index(transcripts)
    _unlabel("UPDATE chunks SET agent_id = NULL, agent_type = NULL, "
             "sidechain = NULL, machine = 'other-box'")
    relabel.run(apply=True)
    assert store.open_for_read().execute(
        "SELECT agent_id, agent_type, sidechain FROM chunks").fetchone() == (
        "a74d", None, 1)


def test_relabel_only_opens_origins_under_a_transcript_root(tmp_path,
                                                            transcripts,
                                                            fake_embed):
    from agent_history import relabel
    outside = tmp_path / "elsewhere" / "subagents"
    outside.mkdir(parents=True)
    f = outside / "agent-zz.jsonl"
    f.write_text(json.dumps({"attributionAgent": "planted"}) + "\n")
    transcripts("proj", "par", [msg("user", "a main thread line here", session="par")])
    index.run()
    db = store.open_for_write()[0]
    db.execute("UPDATE chunks SET origin = ?, sidechain = NULL", (str(f),))
    db.commit()
    db.close()
    relabel.run(apply=True)
    assert store.open_for_read().execute(
        "SELECT agent_id, agent_type, sidechain FROM chunks").fetchone() == (
        "zz", None, 1)
    assert relabel.agent_type_of("a\x00b") is None  # NUL in a path: no crash


def test_relabel_origin_pattern_is_strict(transcripts, fake_embed):
    from agent_history import relabel
    for bad in ("/x/subagents/agent-a b.jsonl", "/x/subagents/agent-" + "a" * 65
                + ".jsonl", "/x/subagents/agent-ok.jsonl\n", "/x/subagents/agent-ok.jsonl.bak"):
        assert relabel._ORIGIN.search(bad) is None, bad
    assert relabel._ORIGIN.search("/x/subagents/agent-ok_1-2.jsonl")
    # the same id class as live indexing, so both label a file alike
    assert relabel._ORIGIN.search("/x/subagents/agent-a.b:c@d.jsonl")
