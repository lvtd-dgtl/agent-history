"""`sync`: incremental, debounced, single-flight, loss-free, path-independent."""
from __future__ import annotations

import json
import shutil
import threading
import time

import pytest

from agent_history import config, embed, index, notify, ship, store, sync, trigger
from agent_history.adapters.claude_code import ClaudeCodeAdapter
from conftest import msg, write_transcript

LONG = "a message long enough to be indexed and shipped to the hub, number %d"


def lines(*items):
    return "".join(json.dumps(i) + "\n" for i in items)


@pytest.fixture
def hub_up():
    return {"ok": True}


@pytest.fixture
def hub(tmp_path, monkeypatch, hub_up):
    landing = tmp_path / "landing"
    landing.mkdir()
    monkeypatch.setenv("AGENT_HISTORY_HUB_INGEST", "ahub@example.invalid:")
    up = hub_up

    def upload(files, dest, key):
        if not up["ok"]:
            return False
        for f in files:
            shutil.copy(f, landing / f.name)
            f.unlink()
        return True

    monkeypatch.setattr(ship, "upload", upload)
    return landing


def shipped(landing):
    out = []
    for f in sorted(landing.glob("*.jsonl")):
        out += [json.loads(x) for x in f.read_text().splitlines()]
    return out


def indexed_texts():
    db = store.open_for_maintenance()
    try:
        return [r[0] for r in db.execute("SELECT text FROM chunks")]
    finally:
        db.close()


def run_once(path, **kw):
    kw.setdefault("do_index", True)
    return sync.sync_files([path], **kw)


# ---------------------------------------------------------------- tail reads

def test_tail_read_returns_only_new_records_and_leaves_partial_line(tmp_path):
    f = tmp_path / "t.jsonl"
    f.write_text(lines(msg("user", LONG % 1)))
    ad = ClaudeCodeAdapter()
    recs, end, _ = ad.read_tail(f, 0)
    assert [r.text for r in recs] == [LONG % 1] and end == f.stat().st_size
    full = json.dumps(msg("assistant", LONG % 2))
    with f.open("a") as fh:
        fh.write(full[:20])                      # half-written line
    recs, end2, _ = ad.read_tail(f, end)
    assert recs == [] and end2 == end            # not consumed, not lost
    with f.open("a") as fh:
        fh.write(full[20:] + "\n")
    recs, end3, _ = ad.read_tail(f, end2)
    assert [r.text for r in recs] == [LONG % 2] and end3 == f.stat().st_size


# ------------------------------------------------------------- incremental

def test_second_pass_reads_only_the_appended_turn(transcripts, hub, fake_embed):
    path = transcripts("p", "s", [msg("user", LONG % 1)])
    assert run_once(path)
    assert [r["text"] for r in shipped(hub)] == [LONG % 1]
    assert indexed_texts() == [LONG % 1]
    with path.open("a") as fh:
        fh.write(lines(msg("assistant", LONG % 2, timestamp="2026-01-01T00:01:00Z")))
    n = len(fake_embed)
    assert run_once(path)
    assert sorted(r["text"] for r in shipped(hub)) == [LONG % 1, LONG % 2]
    assert len(fake_embed) == n + 1              # one chunk embedded, not two
    state = ship.State.open(writable=False)
    assert state.get_file(str(path.resolve()))[2] == path.stat().st_size
    state.close()


def test_unchanged_file_is_skipped_without_reading(transcripts, hub, fake_embed,
                                                   monkeypatch):
    path = transcripts("p", "s", [msg("user", LONG % 1)])
    run_once(path)
    monkeypatch.setattr(ClaudeCodeAdapter, "read_tail",
                        lambda *a, **k: pytest.fail("re-read an unchanged file"))
    assert run_once(path)


def test_records_are_redacted_before_the_outbox(transcripts, hub, fake_embed):
    secret = "".join(["gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"])
    path = transcripts("p", "s", [msg("user", "my token is %s ok thanks" % secret)])
    run_once(path)
    (rec,) = shipped(hub)
    assert secret not in rec["text"] and "[REDACTED" in rec["text"]


# ---------------------------------------------------------------- offline

def test_offline_hub_queues_then_ships_later_without_duplicates(
        transcripts, hub, hub_up, fake_embed):
    path = transcripts("p", "s", [msg("user", LONG % 1)])
    hub_up["ok"] = False
    assert run_once(path)
    assert shipped(hub) == []
    assert len(list(config.outbox_dir().glob("*.jsonl"))) == 1
    hub_up["ok"] = True
    assert ship.run() == 0                       # cron safety net drains it
    assert [r["text"] for r in shipped(hub)] == [LONG % 1]
    assert run_once(path)                        # nothing re-queued
    assert len(shipped(hub)) == 1


def test_failed_embedding_keeps_the_offset_but_still_ships(
        transcripts, hub, monkeypatch):
    def boom(text, timeout=120):
        raise embed.EmbedError("ollama down")

    monkeypatch.setattr(embed, "embed", boom)
    monkeypatch.setattr(embed, "probe_dimension", lambda: 8)
    path = transcripts("p", "s", [msg("user", LONG % 1)])
    assert run_once(path)
    assert len(shipped(hub)) == 1                # shipping needs no embedder
    state = ship.State.open(writable=False)
    assert state.get_file(str(path.resolve())) is None   # will be re-read
    state.close()
    assert run_once(path)
    assert len(shipped(hub)) == 1                # re-read, deduplicated


# ----------------------------------------------- path-independent identity

def move(path, project):
    dest = path.parent.parent / project
    dest.mkdir(exist_ok=True)
    return shutil.move(str(path), str(dest / path.name)) and dest / path.name


def test_moved_transcript_ships_and_indexes_nothing_new(transcripts, hub,
                                                        fake_embed):
    path = transcripts("old", "s", [msg("user", LONG % 1),
                                    msg("assistant", LONG % 2)])
    assert ship.run() == 0
    index.run()
    before_ship, before_chunks = len(shipped(hub)), len(indexed_texts())
    embeds = len(fake_embed)
    new = move(path, "new")
    assert ship.run() == 0                       # full scan, the cron path
    assert index.run() == 0
    assert run_once(new)                         # and the incremental path
    assert len(shipped(hub)) == before_ship == 2
    assert len(indexed_texts()) == before_chunks
    assert len(fake_embed) == embeds


def test_moved_transcript_with_state_from_before_the_upgrade(transcripts, hub,
                                                             fake_embed):
    """ship.db written by 0.17.0 holds origin-bearing hashes; they still count."""
    path = transcripts("old", "s", [msg("user", LONG % 1)])
    state = ship.State.open(writable=True)
    state.queue([ship.record_hash("claude-code", str(path), "sess-1",
                                  "2026-01-01T00:00:00Z", "user", LONG % 1)],
                "legacy.jsonl")
    state.close()
    assert ship.run(migrate_only=True) == 0      # while the file is still there
    new = move(path, "new")
    assert ship.run() == 0
    assert shipped(hub) == []
    assert run_once(new, do_index=False)
    assert shipped(hub) == []


def test_memory_files_keep_their_origin_in_the_hash():
    a = ship.portable_hash("claude-code", "/p1/memory/MEMORY.md", "memory:MEMORY",
                           "t", "memory", "x")
    b = ship.portable_hash("claude-code", "/p2/memory/MEMORY.md", "memory:MEMORY",
                           "t", "memory", "x")
    assert a != b
    c = ship.portable_hash("claude-code", "/p1/s.jsonl", "sess", "t", "user", "x")
    assert c == ship.portable_hash("claude-code", "/p2/s.jsonl", "sess", "t",
                                   "user", "x")


def test_moved_transcript_keeps_its_session_uid(transcripts, fake_embed):
    path = transcripts("old", "s", [msg("user", LONG % 1)])
    index.run()
    new = move(path, "new")
    index.run()
    db = store.open_for_maintenance()
    uids = {r[0] for r in db.execute("SELECT DISTINCT session_uid FROM chunks")}
    db.close()
    assert len(uids) == 1


# ------------------------------------------------------------ spool + runner

def test_notify_coalesces_and_filters_payload_paths(isolated_env):
    p = str(isolated_env / "projects" / "a" / "s.jsonl")
    for _ in range(5):
        notify.notify({"transcript_path": p, "hook_event_name": "Stop"},
                      start=False)
    assert len(sync._entries()) == 1
    notify.notify({"transcript_path": "relative.jsonl"}, start=False)
    assert notify.SWEEP in [e.name for e in sync._entries()]   # fell back to sweep
    assert notify.payload_paths({"transcript_path": "/x/y.txt"}) == []


def test_run_once_drains_spool_and_ships(transcripts, hub, fake_embed,
                                         monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_SYNC_INTERVAL", "0")
    path = transcripts("p", "s", [msg("user", LONG % 1)])
    notify.notify({"transcript_path": str(path)}, start=False)
    store.open_for_write()[0].close()            # a local index exists
    assert sync.run(once=True) == 0
    assert [r["text"] for r in shipped(hub)] == [LONG % 1]
    assert sync._entries() == []


def test_entry_that_changes_during_a_pass_is_kept(transcripts, hub, fake_embed,
                                                  monkeypatch):
    path = transcripts("p", "s", [msg("user", LONG % 1)])
    notify.notify({"transcript_path": str(path)}, start=False)
    real = sync.sync_files

    def racing(files, **kw):
        notify.notify({"transcript_path": str(path)}, start=False)  # re-spool
        return real(files, **kw)

    monkeypatch.setattr(sync, "sync_files", racing)
    assert sync.run(once=True) == 0
    assert len(sync._entries()) == 1             # not lost


def test_sweep_picks_up_a_session_no_hook_reported(transcripts, hub, fake_embed):
    transcripts("p", "s", [msg("user", LONG % 1)])
    assert sync.run(sweep=True, once=True) == 0
    assert len(shipped(hub)) == 1


def test_single_flight_second_syncer_exits_without_working(transcripts, hub,
                                                           monkeypatch):
    transcripts("p", "s", [msg("user", LONG % 1)])
    notify.spool("*")
    monkeypatch.setattr(sync, "_pass",
                        lambda e: pytest.fail("ran while another held the lock"))
    with trigger._exclusive(config.sync_lock_path()) as got:
        assert got
        assert sync.run(gap=0) == 0
    assert len(sync._entries()) == 1             # left for the holder


def test_burst_collapses_to_leading_plus_one_trailing_pass(isolated_env,
                                                           monkeypatch):
    passes = []

    def fake_pass(entries):
        passes.append(time.time())
        for e in entries:
            e.unlink(missing_ok=True)
        return True

    monkeypatch.setattr(sync, "_pass", fake_pass)
    notify.spool("/x/a.jsonl")

    def burst():
        time.sleep(0.1)
        for i in range(10):                      # arrives during the cool-down
            notify.spool(f"/x/b{i % 3}.jsonl")
            time.sleep(0.01)

    t = threading.Thread(target=burst)
    t.start()
    assert sync.run(gap=1.0) == 0
    t.join()
    assert len(passes) == 2                      # leading + one trailing
    assert passes[1] - passes[0] >= 0.95         # at most one per gap
    assert sync._entries() == []


def test_env_file_fills_only_unset_variables(tmp_path, monkeypatch):
    f = tmp_path / "env"
    f.write_text("# c\nAGENT_HISTORY_HUB_INGEST='ahub@h:'\n"
                 "export AGENT_HISTORY_MACHINE=pcx\nPATH=/evil\n")
    monkeypatch.setenv("AGENT_HISTORY_ENV_FILE", str(f))
    monkeypatch.setenv("AGENT_HISTORY_MACHINE", "keep")
    config.load_env_file()
    assert config.hub_ingest() == "ahub@h:"
    assert config.machine_id() == "keep"
