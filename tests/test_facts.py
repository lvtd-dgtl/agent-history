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


# --- review fixes ---------------------------------------------------------------

@needs_vec
def test_scoped_facts_show_in_search_with_their_scope(indexed, tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    assert fact_cli("set", "repo-build", "command", "make release", "--scope", ".") == 0
    capsys.readouterr()
    (row,) = facts.rows("repo-build")
    assert row["scope"] == str(repo.resolve()), "--scope . is stored as an absolute path"
    assert search.search_rows("repo-build command", mode="text")[0]["role"] == "fact"
    narrowed = search.search_rows("repo-build command", mode="text", cwd="repo")
    assert narrowed[0]["role"] == "fact"
    elsewhere = search.search_rows("repo-build command", mode="text", cwd="other-project")
    assert all(r["role"] != "fact" for r in elsewhere)


def test_windows_scopes_compare_like_posix_ones():
    fact = {"scope": "C:\\Users\\me\\proj"}
    assert facts.in_scope(fact, "C:\\Users\\me\\proj\\sub")
    assert not facts.in_scope(fact, "C:\\Users\\me\\project")


def _row(fid, value, since, tenant=None):
    row = {"fact_id": "fact-" + fid * 20, "subject": "db", "predicate": "host",
           "value": value, "valid_from": since, "created_at": since}
    if tenant:
        row["tenant"] = tenant
    return row


def test_tenants_keep_separate_histories():
    rows = [_row("a", "X", "2026-01-01T00:00:00+00:00", "a"),
            _row("b", "Y", "2026-06-01T00:00:00+00:00", "b")]
    assert sorted(f["value"] for f in facts.current(rows)) == ["X", "Y"]
    hits = [{**facts.as_hit(r), "tenant": r["tenant"]} for r in rows]
    assert len(facts.reconcile(hits)) == 2


def test_one_tenant_and_this_machine_are_one_history():
    rows = [_row("a", "old-local", "2026-01-01T00:00:00+00:00"),
            _row("b", "new-hub", "2026-06-01T00:00:00+00:00", "kim")]
    assert [f["value"] for f in facts.current(rows)] == ["new-hub"]


def test_forget_also_drops_copies_still_queued(monkeypatch, capsys):
    from agent_history import ship

    monkeypatch.setattr(ship, "upload", lambda *a: False)
    monkeypatch.setenv("AGENT_HISTORY_HUB_INGEST", "ahub@hub.example.invalid:")
    fact_cli("set", "secret-box", "password", "hunter2")
    fact_cli("set", "other-box", "password", "keep-me")
    assert len(list(facts.outbox_dir().glob("*.jsonl"))) == 2
    assert fact_cli("forget", "secret-box", "--yes") == 0
    (left,) = facts.outbox_dir().glob("*.jsonl")
    assert "other-box" in left.read_text()
    capsys.readouterr()
    db = facts.open_store(write=True)
    assert db.execute("PRAGMA secure_delete").fetchone()[0] == 1
    db.close()


def test_local_redaction_applies_to_facts_too(monkeypatch, capsys):
    monkeypatch.setenv("AGENT_HISTORY_REDACT", "on")
    fact_cli("set", "ci", "token", GH)
    capsys.readouterr()
    assert GH not in facts.rows("ci")[0]["value"]


@needs_vec
def test_injected_facts_are_capped(indexed, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_HISTORY_INJECT", "1")
    fact_cli("set", "lvtd-fleet-1", "ssh notes", "x" * 1900)
    capsys.readouterr()
    payload = io.StringIO(json.dumps({"session_id": "now", "cwd": "/w",
                                      "prompt": "ssh notes for lvtd-fleet-1"}))
    assert inject.run_hook(payload) == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert len(context) <= inject.MAX_TOTAL_CHARS
    assert "x" * (inject.FACT_CHARS + 10) not in context


@needs_vec
def test_a_facts_only_tenant_still_answers_search(tmp_path, monkeypatch, fake_embed, capsys):
    monkeypatch.setenv("AGENT_HISTORY_TENANTS", str(tmp_path / "tenants"))
    with config.tenant_home("kim"):
        facts.save(facts.build("gateway", "host", "kim-host"))
    capsys.readouterr()
    assert serve_ssh.run("kim", "search --json -- gateway host") == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert row["role"] == "fact" and "kim-host" in row["text"]


# --- edge cases found in review -----------------------------------------------------

def test_a_tie_on_valid_from_goes_to_the_fact_created_later():
    old = {**_row("f", "stale-local", "2026-06-01T00:00:00+00:00"),
           "created_at": "2026-06-01T00:00:00+00:00"}
    new = {**_row("0", "hub-correction", "2026-06-01T00:00:00+00:00", "kim"),
           "created_at": "2026-06-02T00:00:00+00:00"}
    hits = [facts.as_hit(old), {**facts.as_hit(new), "tenant": "kim"}]
    assert hits[0]["fact"]["created_at"] == old["created_at"]
    (kept,) = [h for h in facts.reconcile(hits)]
    assert kept["fact"]["value"] == "hub-correction", \
        "the larger fact id must not beat the later correction"


def test_history_prints_facts_from_several_tenants(capsys):
    rows = [_row("a", "X", "2026-01-01T00:00:00+00:00", "a"),
            _row("b", "Y", "2026-06-01T00:00:00+00:00", "b")]
    facts.print_history(rows, None)
    out = capsys.readouterr().out
    assert "db · host  [a]" in out and "db · host  [b]" in out


def test_search_with_facts_but_no_index_yet(fake_embed, capsys):
    assert not config.db_path().exists()
    fact_cli("set", "gateway", "host", "10.0.0.9")
    capsys.readouterr()
    rows = search.search_rows("gateway host", mode="text")
    assert [r["role"] for r in rows] == ["fact"]
    assert "no transcript index yet" in capsys.readouterr().err
    with pytest.raises(store.StoreError, match="no index"):
        search.search_rows("nothing curated about this", mode="text")


def _inject(prompt, capsys, cwd="/w"):
    payload = io.StringIO(json.dumps({"session_id": "now", "cwd": cwd, "prompt": prompt}))
    assert inject.run_hook(payload) == 0
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"]["additionalContext"] if out else ""


def test_facts_inject_before_any_index_exists(fake_embed, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_HISTORY_INJECT", "1")
    assert not config.db_path().exists()
    fact_cli("set", "lvtd-fleet-1", "ssh", "linuxuser@100.103.244.8")
    capsys.readouterr()
    assert "linuxuser@100.103.244.8" in _inject("ssh to lvtd-fleet-1 please", capsys)


@needs_vec
def test_hints_off_also_silences_fact_warnings(indexed, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_HISTORY_INJECT", "1")
    monkeypatch.setenv("AGENT_HISTORY_INJECT_MAX_DIST", "1000")
    fact_cli("set", "linuxuser", "fleet ssh", "linuxuser@100.103.244.8", "--since", "2026-10-11")
    capsys.readouterr()
    on = _inject("ssh linuxuser fleet box login", capsys)
    assert "\u26a0" in on, "the old address is flagged while hints are on"
    monkeypatch.setenv("AGENT_HISTORY_SUPERSEDE_HINTS", "0")
    off = _inject("ssh linuxuser fleet box login", capsys)
    assert "linuxuser@100.103.244.8" in off and "⚠" not in off


def test_the_facts_section_fits_the_size_budget():
    base = len(inject._header("/x")) - len("/x")
    long_cwd = "/" + "d" * (inject.MAX_TOTAL_CHARS - 100 - base - 1)
    fact = facts.resolve([_row("a", "X", "2026-01-01T00:00:00+00:00")])
    assert len(inject._header(long_cwd)) == inject.MAX_TOTAL_CHARS - 100
    out = inject._render([], long_cwd, facts_in_force=[fact])
    assert len(out) <= inject.MAX_TOTAL_CHARS
    assert "Current facts" not in out
    many = [fact] * 50
    out = inject._render([], "/w", facts_in_force=many)
    assert len(out) <= inject.MAX_TOTAL_CHARS and out.count("\n- ") >= 1


@needs_vec
def test_facts_inject_when_the_index_was_built_with_another_model(indexed, monkeypatch,
                                                                 capsys):
    monkeypatch.setenv("AGENT_HISTORY_INJECT", "1")
    fact_cli("set", "lvtd-fleet-1", "ssh", "linuxuser@100.103.244.8")
    capsys.readouterr()
    monkeypatch.setenv("AGENT_HISTORY_MODEL", "some-other-model")
    context = _inject("ssh to lvtd-fleet-1 please", capsys)
    assert "linuxuser@100.103.244.8" in context and "45.76.1.5" not in context
