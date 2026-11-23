""""Possibly superseded" hints: identifier drift among one search's hits."""
from __future__ import annotations

import hashlib
import io
import json

import pytest

from agent_history import config, embed, inject, search, store, supersede


def hit(text, ts, session, **extra):
    return {"text": text, "timestamp": ts, "session_id": session, **extra}


def hints(hits):
    return [h.get("superseded_hint") for h in supersede.annotate(hits)]


# --- what it should catch ------------------------------------------------------

def test_a_server_migration_flags_the_old_login():
    old = hit("to log in: ssh -i ~/.ssh/id_ed25519 linuxuser@45.76.1.5", "2026-08-01", "a")
    new = hit("we moved fleet-1; now ssh linuxuser@100.103.244.8 over the tailnet",
              "2026-10-11", "b")
    got = hints([old, new])
    assert got[1] is None
    assert got[0]["kind"] in ("ssh-target", "ipv4")
    assert got[0]["old"] in ("45.76.1.5",) and got[0]["new"] == "100.103.244.8"
    assert got[0]["by_rank"] == 2 and got[0]["by_timestamp"] == "2026-10-11"


def test_an_address_with_shared_context():
    old = hit("the hexboard database lives at 10.20.0.5 on port 5434", "2026-05-01", "a")
    new = hit("hexboard database migrated, it is at 10.20.0.9 now", "2026-09-01", "b")
    (first, second) = hints([old, new])
    assert first["kind"] == "ipv4" and (first["old"], first["new"]) == ("10.20.0.5", "10.20.0.9")
    assert second is None


def test_a_version_bump():
    old = hit("the hub runs agent-history 0.17.0 after the upgrade", "2026-11-11", "a")
    new = hit("upgraded the hub: agent-history 0.19.0 is live", "2026-11-20", "b")
    (first, _) = hints([new, old])  # order in the list does not matter
    assert first is None
    assert hints([new, old])[1]["kind"] == "version"


def test_a_moved_key_file():
    old = hit("deploy key for winch-bot is ~/.ssh/winch_deploy", "2026-04-01", "a")
    new = hit("winch-bot deploy key rotated to ~/.ssh/winch_deploy_2026", "2026-10-01", "b")
    assert hints([old, new])[0]["kind"] == "ssh-key"


def test_the_newest_replacement_is_named():
    a = hit("ssh ops@10.0.0.1 for the gateway box", "2026-01-01", "a")
    b = hit("gateway moved: ssh ops@10.0.0.2", "2026-05-01", "b")
    c = hit("gateway moved again: ssh ops@10.0.0.3", "2026-09-01", "c")
    got = hints([a, b, c])
    assert got[0]["new"] == "10.0.0.3"
    assert got[1]["new"] == "10.0.0.3"
    assert got[2] is None


# --- what it must not flag ---------------------------------------------------------

def test_two_hosts_in_one_session_are_not_a_migration():
    a = hit("web is ssh deploy@10.0.0.5", "2026-01-01T10:00", "same")
    b = hit("and the database is ssh deploy@10.0.0.9", "2026-01-01T11:00", "same")
    assert hints([a, b]) == [None, None]


def test_a_newer_hit_that_still_names_the_old_value_is_a_mention():
    a = hit("the gateway is ssh ops@10.0.0.1", "2026-01-01", "a")
    b = hit("gateway: ssh ops@10.0.0.1 is primary, ssh ops@10.0.0.2 is the spare",
            "2026-06-01", "b")
    assert hints([a, b])[0] is None


def test_unrelated_addresses_are_not_compared():
    a = hit("the printer at 192.168.1.40 jammed again", "2026-01-01", "a")
    b = hit("kubernetes pod network uses 10.244.0.12 for coredns", "2026-06-01", "b")
    assert hints([a, b]) == [None, None]


def test_different_users_on_different_hosts_are_different_things():
    a = hit("ssh ahub@45.76.1.5 for the hub", "2026-01-01", "a")
    b = hit("ssh linuxuser@100.103.244.8 for dev work", "2026-06-01", "b")
    assert [h and h["kind"] for h in hints([a, b])] in ([None, None], ["ipv4", None])
    assert not any(h and h["kind"] == "ssh-target" for h in hints([a, b]))


def test_loopback_and_netmask_addresses_are_ignored():
    assert supersede.identifiers("serve on 127.0.0.1 with mask 255.255.255.0 and 0.0.0.0") == []


def test_generic_words_do_not_name_a_version():
    assert supersede.identifiers("version 1.2.3 and release 2.0.0") == []


def test_undated_hits_and_lone_hits_get_nothing():
    a = hit("ssh ops@10.0.0.1 gateway", "", "a")
    b = hit("gateway now ssh ops@10.0.0.2", "2026-06-01", "b")
    assert hints([a, b])[0] is None
    assert supersede.annotate([b]) == [b]


def test_hints_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_SUPERSEDE_HINTS", "0")
    a = hit("ssh ops@10.0.0.1 gateway", "2026-01-01", "a")
    b = hit("gateway now ssh ops@10.0.0.2", "2026-06-01", "b")
    assert hints([a, b]) == [None, None]


def test_stale_hints_from_another_list_are_dropped():
    a = hit("nothing operational here at all", "2026-01-01", "a",
            superseded_hint={"kind": "ipv4", "old": "x", "new": "y", "by_rank": 9})
    b = hit("nor here", "2026-02-01", "b")
    assert all("superseded_hint" not in h for h in supersede.annotate([a, b]))


# --- surfaces ------------------------------------------------------------------

needs_vec = pytest.mark.skipif(not store.extensions_supported(),
                               reason="this Python cannot load SQLite extensions")


def put(db, text, ts, session, cwd="/w"):
    store.insert_chunk(
        db, agent="claude-code", origin=f"/t/{session}.jsonl", session_id=session,
        cwd=cwd, timestamp=ts, role="assistant", text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        vector=embed.embed(text))
    db.commit()


@needs_vec
def test_search_output_carries_the_hint(fake_embed, capsys):
    db, _ = store.open_for_write()
    put(db, "fleet login: ssh linuxuser@45.76.1.5", "2026-08-01T00:00:00Z", "a")
    put(db, "fleet login changed: ssh linuxuser@100.103.244.8", "2026-10-11T00:00:00Z", "b")
    assert search.run("fleet login ssh", mode="text", as_json=True) == 0
    rows = json.loads(capsys.readouterr().out)
    old = next(r for r in rows if "45.76.1.5" in r["text"])
    assert old["superseded_hint"]["new"] == "100.103.244.8"
    assert search.run("fleet login ssh", mode="text") == 0
    assert "possibly superseded: login host 45.76.1.5 -> 100.103.244.8" in \
        capsys.readouterr().out


@needs_vec
def test_injected_excerpts_carry_the_warning(fake_embed, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_HISTORY_INJECT", "1")
    monkeypatch.setenv("AGENT_HISTORY_INJECT_MAX_DIST", "1000")
    monkeypatch.setenv("AGENT_HISTORY_INJECT_CROSS_DIST", "1000")
    db, _ = store.open_for_write()
    put(db, "fleet login: ssh linuxuser@45.76.1.5", "2026-08-01T00:00:00Z", "a")
    put(db, "fleet login changed: ssh linuxuser@100.103.244.8", "2026-10-11T00:00:00Z", "b")
    payload = io.StringIO(json.dumps({"session_id": "now", "cwd": "/w",
                                      "prompt": "how do I ssh into the fleet box?"}))
    assert inject.run_hook(payload) == 0
    context = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "possibly superseded: login host 45.76.1.5 -> 100.103.244.8" in context
    assert context.startswith("Possibly relevant excerpts"), "loop guard prefix kept"
    assert config  # imported for the env isolation fixture's benefit
