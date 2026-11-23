"""Curated facts with valid time: history, as-of, search, injection, hub."""
from __future__ import annotations

import hashlib
import io
import json

import pytest

from agent_history import cli, config, embed, facts, inject, search, serve_ssh, store

needs_vec = pytest.mark.skipif(not store.extensions_supported(),
                               reason="this Python cannot load SQLite extensions")

GH = "".join(["gh", "p_", "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"])


def fact_cli(*argv):
    return cli.main(["fact", *argv])


def out_json(capsys):
    return json.loads(capsys.readouterr().out)


# --- the record and its history ---------------------------------------------------

def test_set_twice_keeps_a_two_row_history(capsys):
    assert fact_cli("set", "lvtd-fleet-1", "ssh", "linuxuser@45.76.1.5",
                    "--since", "2026-08-01") == 0
    assert fact_cli("set", "LVTD-Fleet-1", "SSH", "linuxuser@100.103.244.8",
                    "--since", "2026-10-11") == 0
    capsys.readouterr()

    assert fact_cli("get", "lvtd-fleet-1", "--json") == 0
    (live,) = out_json(capsys)
    assert live["value"] == "linuxuser@100.103.244.8"
    assert live["previous"]["value"] == "linuxuser@45.76.1.5"

    assert fact_cli("get", "lvtd-fleet-1", "ssh", "--as-of", "2026-09-01", "--json") == 0
    (then,) = out_json(capsys)
    assert then["value"] == "linuxuser@45.76.1.5"
    assert then["valid_to"].startswith("2026-10-1")

    assert fact_cli("get", "lvtd-fleet-1", "--history", "--json") == 0
    rows = out_json(capsys)
    assert [r["value"] for r in rows] == ["linuxuser@45.76.1.5", "linuxuser@100.103.244.8"]
    assert [r["current"] for r in rows] == [False, True]

    assert fact_cli("get", "lvtd-fleet-1", "--history") == 0
    human = capsys.readouterr().out
    assert "* 2026-10-1" in human and "linuxuser@45.76.1.5" in human


def test_before_any_value_there_is_nothing(capsys):
    fact_cli("set", "hub", "version", "0.19.0", "--since", "2026-11-19")
    capsys.readouterr()
    assert fact_cli("get", "hub", "--as-of", "2026-11-01") == 0
    assert "(no facts in force)" in capsys.readouterr().out


def test_retire_ends_a_value_but_keeps_it_in_history(capsys):
    fact_cli("set", "winch-bot", "host", "lvtd-fleet-1", "--since", "2026-06-01")
    fact_cli("retire", "winch-bot", "host", "--since", "2026-10-01")
    capsys.readouterr()
    assert fact_cli("get", "winch-bot", "--json") == 0
    assert out_json(capsys) == []
    assert fact_cli("get", "winch-bot", "--as-of", "2026-07-01", "--json") == 0
    assert out_json(capsys)[0]["value"] == "lvtd-fleet-1"
    assert fact_cli("get", "winch-bot", "--history", "--json") == 0
    assert [r["value"] for r in out_json(capsys)] == ["lvtd-fleet-1", None]


def test_a_back_dated_value_does_not_replace_a_later_one(capsys):
    fact_cli("set", "atrium", "api host", "lvtd-dgtl-1", "--since", "2026-09-01")
    fact_cli("set", "atrium", "api host", "old-box", "--since", "2026-01-01")
    capsys.readouterr()
    assert fact_cli("get", "atrium", "--json") == 0
    assert out_json(capsys)[0]["value"] == "lvtd-dgtl-1"


def test_list_shows_every_fact_in_force(capsys):
    fact_cli("set", "a-thing", "colour", "red")
    fact_cli("set", "b-thing", "colour", "blue")
    capsys.readouterr()
    assert fact_cli("list", "--json") == 0
    assert [f["subject"] for f in out_json(capsys)] == ["a-thing", "b-thing"]


def test_bad_input_is_refused(capsys):
    assert fact_cli("set", "  ", "p", "v") == 1
    assert "subject is required" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        fact_cli("set", "s", "p", "v", "--since", "someday")
    capsys.readouterr()


def test_forget_previews_then_deletes(capsys):
    fact_cli("set", "secret-box", "root password", "hunter2")
    capsys.readouterr()
    assert fact_cli("forget", "secret-box") == 0
    assert "would delete 1" in capsys.readouterr().out
    assert facts.rows("secret-box")
    assert fact_cli("forget", "secret-box", "--yes") == 0
    assert not facts.rows("secret-box")
    capsys.readouterr()


def test_unused_facts_are_inert():
    assert not facts.db_path().exists()
    assert facts.rows() == [] and facts.matching("anything at all") == []
    assert not facts.db_path().exists(), "reading must not create facts.db"


# --- matching a query ---------------------------------------------------------------

def test_matching_needs_the_subject_named():
    fact = {"subject": "lvtd-fleet-1", "predicate": "ssh"}
    terms = facts._terms
    assert facts.match_score(fact, terms("how do I ssh into lvtd-fleet-1")) > 0
    assert facts.match_score(fact, terms("ssh into fleet-1")) > 0, "half the name + predicate"
    assert facts.match_score(fact, terms("what is the weather")) == 0
    assert facts.match_score(fact, terms("ssh into fleet-1"), strict=True) == 0
    assert facts.match_score(fact, terms("ssh lvtd-fleet-1"), strict=True) > 0
    one = {"subject": "hub", "predicate": "version"}
    assert facts.match_score(one, terms("hub"), strict=True) == 0, \
        "a one-word subject alone is too easy to hit in a prompt"


def test_a_scoped_fact_applies_only_inside_its_tree():
    fact = {"subject": "db", "predicate": "port", "scope": "/repos/atrium"}
    assert facts.in_scope(fact, "/repos/atrium/api")
    assert not facts.in_scope(fact, "/repos/atrium-old")
    assert not facts.in_scope(fact, None)
    assert facts.in_scope({"scope": None}, None)


# --- search -----------------------------------------------------------------------

def put(db, text, ts, session, cwd="/w"):
    store.insert_chunk(
        db, agent="claude-code", origin=f"/t/{session}.jsonl", session_id=session,
        cwd=cwd, timestamp=ts, role="assistant", text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        vector=embed.embed(text))
    db.commit()


@pytest.fixture
def indexed(fake_embed):
    db, _ = store.open_for_write()
    put(db, "fleet box login: ssh linuxuser@45.76.1.5", "2026-08-01T00:00:00Z", "a")
    put(db, "unrelated: the kitchen budget spreadsheet", "2026-08-02T00:00:00Z", "b")
    return db


@needs_vec
def test_a_matching_fact_leads_search_above_k_hits(indexed, capsys):
    fact_cli("set", "lvtd-fleet-1", "ssh", "linuxuser@100.103.244.8", "--since", "2026-10-11")
    capsys.readouterr()
    rows = search.search_rows("ssh into lvtd-fleet-1", k=1, mode="text")
    assert rows[0]["role"] == "fact" and rows[0]["agent"] == "fact"
    assert "linuxuser@100.103.244.8" in rows[0]["text"]
    assert len(rows) == 2, "the fact rides above the k transcript hits"

    assert search.run("ssh into lvtd-fleet-1", mode="text") == 0
    out = capsys.readouterr().out
    assert "#1  fact" in out and "agent-history fact get lvtd-fleet-1 --history" in out
    # The fact is newer and names a different login: the transcript is flagged.
    assert "possibly superseded" in out


@needs_vec
def test_as_of_search_returns_the_fact_as_it_was(indexed, capsys):
    fact_cli("set", "lvtd-fleet-1", "ssh", "linuxuser@45.76.1.5", "--since", "2026-07-01")
    fact_cli("set", "lvtd-fleet-1", "ssh", "linuxuser@100.103.244.8", "--since", "2026-10-11")
    capsys.readouterr()
    from agent_history import timeaxis

    rows = search.search_rows("lvtd-fleet-1 ssh", k=3, mode="text",
                              window=timeaxis.window(as_of="2026-09-01"))
    assert "45.76.1.5" in rows[0]["text"] and rows[0]["role"] == "fact"


@needs_vec
def test_agent_fact_returns_facts_alone_and_newest_sort_skips_them(indexed, capsys):
    fact_cli("set", "lvtd-fleet-1", "ssh", "linuxuser@100.103.244.8")
    capsys.readouterr()
    only = search.search_rows("lvtd-fleet-1 ssh", agent="fact", mode="text")
    assert [r["role"] for r in only] == ["fact"]
    newest = search.search_rows("lvtd-fleet-1 ssh", sort="newest", mode="text")
    assert all(r["role"] != "fact" for r in newest)
    other = search.search_rows("lvtd-fleet-1 ssh", agent="claude-code", mode="text")
    assert all(r["role"] != "fact" for r in other)


def test_reconcile_keeps_the_newest_copy_of_a_key():
    old = facts.as_hit({"fact_id": "fact-" + "a" * 20, "subject": "x", "predicate": "y",
                        "value": "1", "valid_from": "2026-01-01T00:00:00+00:00"})
    new = facts.as_hit({"fact_id": "fact-" + "b" * 20, "subject": "X", "predicate": "y",
                        "value": "2", "valid_from": "2026-06-01T00:00:00+00:00"})
    dup = {**new, "tenant": "kim"}
    kept = facts.reconcile([old, new, dup, {"role": "user", "text": "t"}])
    assert [h.get("fact", {}).get("value") for h in kept if h["role"] == "fact"] == ["2"]
    assert len(kept) == 2


# --- injection ----------------------------------------------------------------------

@needs_vec
def test_injection_puts_the_current_fact_first(indexed, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_HISTORY_INJECT", "1")
    monkeypatch.setenv("AGENT_HISTORY_INJECT_MAX_DIST", "1000")
    fact_cli("set", "lvtd-fleet-1", "ssh", "linuxuser@100.103.244.8", "--since", "2026-10-11")
    capsys.readouterr()
    payload = io.StringIO(json.dumps({"session_id": "now", "cwd": "/w",
                                      "prompt": "how do I ssh to lvtd-fleet-1 again?"}))
    assert inject.run_hook(payload) == 0
    out = json.loads(capsys.readouterr().out)
    context = out["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("Possibly relevant excerpts")
    facts_at = context.index("Current facts")
    assert "lvtd-fleet-1 · ssh: linuxuser@100.103.244.8" in context
    old_at = context.find("45.76.1.5")
    assert old_at == -1 or old_at > facts_at, "the fact comes before any stale excerpt"
    if old_at != -1:
        assert "possibly superseded" in context or "predates the current fact" in context
    assert "1 fact" in out["systemMessage"]


@needs_vec
def test_strict_injection_takes_only_project_scoped_facts(indexed, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_HISTORY_STRICT", "1")
    monkeypatch.setenv("AGENT_HISTORY_INJECT", "1")
    fact_cli("set", "lvtd-fleet-1", "ssh", "linuxuser@100.103.244.8")
    capsys.readouterr()
    payload = io.StringIO(json.dumps({"session_id": "now", "cwd": "/elsewhere",
                                      "prompt": "ssh to lvtd-fleet-1 please"}))
    assert inject.run_hook(payload) == 0
    assert "Current facts" not in capsys.readouterr().out


@needs_vec
def test_facts_alone_inject_when_the_embedder_is_down(indexed, monkeypatch, capsys):
    from agent_history import embed as embed_module

    def down(*a, **k):
        raise embed_module.EmbedError("down")

    monkeypatch.setattr(embed_module, "embed", down)
    monkeypatch.setenv("AGENT_HISTORY_INJECT", "1")
    fact_cli("set", "lvtd-fleet-1", "ssh", "linuxuser@100.103.244.8")
    capsys.readouterr()
    payload = io.StringIO(json.dumps({"session_id": "now", "cwd": "/w",
                                      "prompt": "ssh to lvtd-fleet-1 please"}))
    assert inject.run_hook(payload) == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "linuxuser@100.103.244.8" in context
    assert "45.76.1.5" not in context, "no embedder, no excerpts"


# --- shipping and the hub ---------------------------------------------------------

def test_ship_sends_a_redacted_copy_and_keeps_the_raw_one(monkeypatch, tmp_path, capsys):
    from agent_history import ship

    sent = []

    def upload(files, dest, key):
        sent.append((dest, [f.read_text() for f in files]))
        return True

    monkeypatch.setattr(ship, "upload", upload)
    monkeypatch.setenv("AGENT_HISTORY_HUB_INGEST", "ahub@hub.example.invalid:")
    assert fact_cli("set", "ci", "token", GH) == 0
    assert "queued for the hub" in capsys.readouterr().out
    ((dest, bodies),) = sent
    assert dest == "ahub@hub.example.invalid:facts/"
    assert GH not in bodies[0] and "[REDACTED" in bodies[0]
    assert facts.rows("ci")[0]["value"] == GH, "this machine keeps the raw value"
    assert not list(facts.outbox_dir().glob("*.jsonl")), "shipped files leave the outbox"


def test_a_failed_upload_stays_queued(monkeypatch, capsys):
    from agent_history import ship

    monkeypatch.setattr(ship, "upload", lambda *a: False)
    monkeypatch.setenv("AGENT_HISTORY_HUB_INGEST", "ahub@hub.example.invalid:")
    assert fact_cli("set", "a", "b", "c") == 0
    assert "hub unreachable" in capsys.readouterr().out
    assert len(list(facts.outbox_dir().glob("*.jsonl"))) == 1


def test_hub_indexes_the_inbox_redacts_again_and_retires(tmp_path, monkeypatch, capsys):
    inbox = tmp_path / "inbox"
    monkeypatch.setenv("AGENT_HISTORY_INBOX", str(inbox))
    monkeypatch.setenv("AGENT_HISTORY_HUB", "1")
    raw = facts.build("ci", "token", GH, since="2026-10-01")   # a misbehaving shipper
    facts.write_file(inbox / "pc1" / "facts", raw)
    (inbox / "pc1" / "facts" / "junk.jsonl").write_text("not json\n{}\n")
    assert facts.inbox_pending()
    assert facts.index_inbox() == 0
    (row,) = facts.rows("ci")
    assert row["machine"] == "pc1" and GH not in row["value"]
    assert not facts.inbox_pending()
    assert list((inbox / ".processed" / "pc1" / "facts").glob("*.jsonl"))
    assert facts.index_inbox() == 0, "nothing left; nothing to do"
    capsys.readouterr()


def test_parse_refuses_malformed_lines():
    good = facts.build("s", "p", "v")
    assert facts.parse(json.dumps(good)) is not None
    for bad in ({**good, "fact_id": "nope"}, {**good, "format": 2},
                {**good, "subject": ""}, {**good, "valid_from": "never"}, [1, 2]):
        assert facts.parse(json.dumps(bad)) is None


@needs_vec
def test_serve_ssh_reads_facts_but_never_writes(tmp_path, monkeypatch, fake_embed, capsys):
    monkeypatch.setenv("AGENT_HISTORY_TENANTS", str(tmp_path / "tenants"))
    for tenant, value in (("kim", "kim-host"), ("globex", "globex-host")):
        with config.tenant_home(tenant):
            facts.save(facts.build("gateway", "host", value))
    capsys.readouterr()
    assert serve_ssh.run("kim", "fact get gateway --history --json") == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["value"] for r in rows] == ["kim-host"] and rows[0]["tenant"] == "kim"
    for command in ("fact set gateway host evil", "fact forget gateway --yes",
                    "fact retire gateway host", "fact index", "fact ship"):
        assert serve_ssh.run("kim", command) == serve_ssh.REJECTED
    capsys.readouterr()
    with config.tenant_home("kim"):
        assert [r["value"] for r in facts.rows("gateway")] == ["kim-host"]


def test_get_merges_this_machine_and_the_hub(monkeypatch, capsys):
    from agent_history import remote

    fact_cli("set", "gateway", "host", "local-old", "--since", "2026-01-01")
    capsys.readouterr()
    hub_row = {"fact_id": "fact-" + "c" * 20, "subject": "gateway", "predicate": "host",
               "value": "hub-new", "valid_from": "2026-06-01T00:00:00+00:00",
               "created_at": "2026-06-01T00:00:00+00:00", "machine": "pc2",
               "tenant": "kim", "text": "..."}
    asked = []

    def query_json(command, require="text"):
        asked.append(command)
        return [hub_row]

    monkeypatch.setattr(remote, "query_json", query_json)
    monkeypatch.setenv("AGENT_HISTORY_REMOTE", "ssh://reader@hub.example.invalid")
    assert fact_cli("get", "gateway", "--json") == 0
    assert out_json(capsys)[0]["value"] == "hub-new"
    assert asked[0][:3] == ["fact", "get", "gateway"] and "--history" in asked[0]
    assert fact_cli("get", "gateway", "--local", "--json") == 0
    assert out_json(capsys)[0]["value"] == "local-old"

    def down(command, require="text"):
        raise remote.RemoteError("timed out")

    monkeypatch.setattr(remote, "query_json", down)
    assert fact_cli("get", "gateway", "--json") == 0
    cap = capsys.readouterr()
    assert json.loads(cap.out)[0]["value"] == "local-old"
    assert "hub unavailable" in cap.err
