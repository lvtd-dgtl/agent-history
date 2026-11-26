"""Row filters and the per-session cap: --subagents, --role, --min-chars,
--agent lists, --exclude-agent, --per-session — CLI, hub, MCP and eval."""
from __future__ import annotations

import hashlib
import json

import pytest

from agent_history import cli, config, embed, facts, remote, search, serve_ssh, store

pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)

TOPIC = "deploy the billing service to production"


def put(db, text, *, session, role="assistant", agent="claude-code",
        sidechain=None, agent_id=None, ts="2026-06-01T00:00:00Z"):
    store.insert_chunk(
        db, agent=agent, origin=f"/t/{session}.jsonl", session_id=session,
        cwd="/w", timestamp=ts, role=role, text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        vector=embed.embed(text))
    if sidechain or agent_id:
        db.execute("UPDATE chunks SET sidechain = ?, agent_id = ? WHERE content_hash = ?",
                   (1 if sidechain else 0, agent_id,
                    hashlib.sha256(text.encode()).hexdigest()))
    db.commit()


@pytest.fixture
def indexed(fake_embed):
    db, _ = store.open_for_write()
    # one long session that mentions the topic over and over
    for n in range(6):
        put(db, f"{TOPIC}: step {n} of the long rollout, with notes and checks",
            session="long", ts=f"2026-06-01T00:0{n}:00Z")
    put(db, f"{TOPIC} — the decision was blue/green behind the load balancer",
        session="decision")
    put(db, f"how do we {TOPIC}?", session="question", role="user")
    put(db, f"You are a subagent. Task: {TOPIC}. HARD RULES: read only.",
        session="parent", role="user", sidechain=True, agent_id="agent-a1b2")
    put(db, f"subagent report: {TOPIC} needs the migration first",
        session="parent", role="assistant", sidechain=True, agent_id="agent-a1b2")
    put(db, f"codex: {TOPIC} by running the release workflow", session="cx", agent="codex")
    put(db, f"runbook: {TOPIC} via make release && make deploy", session="doc",
        agent="docs", role="doc")
    db.close()


def hits(**options):
    return search.search_rows(TOPIC, k=20, mode="text", **options)


def sessions(rows):
    return [r["session_id"] for r in rows]


def test_subagents_exclude_only_and_include(indexed):
    every = hits()
    assert any(r["sidechain"] for r in every)
    assert not any(r["sidechain"] or r["agent_id"] for r in hits(subagents="exclude"))
    only = hits(subagents="only")
    assert only and all(r["sidechain"] for r in only)
    assert len(hits(subagents="exclude")) + len(only) == len(every)


def test_roles_take_lists_and_commas(indexed):
    assert {r["role"] for r in hits(roles=["user"])} == {"user"}
    assert {r["role"] for r in hits(roles=["assistant,doc"])} == {"assistant", "doc"}
    assert {r["role"] for r in hits(roles=["doc", "user"])} == {"doc", "user"}


def test_min_chars_drops_short_chunks(indexed):
    short = min(len(r["text"]) for r in hits())
    floor = short + 1
    assert all(len(r["text"]) >= floor for r in hits(min_chars=floor))
    assert len(hits(min_chars=floor)) < len(hits())


def test_agent_lists_and_exclusions(indexed):
    assert {r["agent"] for r in hits(agent="codex,docs")} == {"codex", "docs"}
    assert {r["agent"] for r in hits(exclude_agent=["docs", "codex"])} == {"claude-code"}
    assert {r["agent"] for r in hits(agent="codex")} == {"codex"}, "one agent still works"


def test_per_session_caps_one_long_conversation(indexed):
    plain = hits()
    assert sessions(plain).count("long") == 6
    capped = search.search_rows(TOPIC, k=5, mode="text", per_session=1)
    assert len(capped) == 5
    assert len(set(sessions(capped))) == 5, "five hits, five conversations"
    two = hits(per_session=2)
    assert sessions(two).count("long") == 2


def test_per_session_keeps_facts_and_filters_shape_facts(indexed):
    facts.save(facts.build("billing service", "deploy", "blue/green via make release"))
    rows = search.search_rows("billing service deploy", k=3, mode="text", per_session=1)
    assert rows[0]["role"] == "fact"
    assert all(r["role"] != "fact" for r in hits(roles=["assistant"]))
    assert any(r["role"] == "fact" for r in
               search.search_rows("billing service deploy", mode="text", roles=["fact"]))
    assert all(r["role"] != "fact" for r in
               search.search_rows("billing service deploy", mode="text",
                                  exclude_agent=["fact"]))
    assert all(r["role"] != "fact" for r in
               search.search_rows("billing service deploy", mode="text",
                                  subagents="only"))
    mixed = search.search_rows("billing service deploy", mode="text", agent="fact,codex")
    assert {r["agent"] for r in mixed} <= {"fact", "codex"} and mixed[0]["agent"] == "fact"


def test_defaults_are_unchanged(indexed):
    plain = search.search_rows(TOPIC, k=8, mode="text")
    explicit = search.search_rows(TOPIC, k=8, mode="text", roles=None, exclude_agent=None,
                                  subagents="include", min_chars=0, per_session=None)
    assert json.dumps(plain, sort_keys=True) == json.dumps(explicit, sort_keys=True)


def test_cli_flags(indexed, capsys):
    assert cli.main(["search", TOPIC, "--mode", "text", "--json", "--subagents", "exclude",
                     "--per-session", "1", "--role", "assistant", "--exclude-agent",
                     "docs"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows and len(set(sessions(rows))) == len(rows)
    assert all(r["role"] == "assistant" and r["agent"] != "docs"
               and not r["sidechain"] for r in rows)


@pytest.mark.parametrize("argv", [["--per-session", "0"], ["--min-chars", "-1"],
                                  ["--subagents", "some"], ["--per-session", "x"]])
def test_cli_refuses_bad_values(argv, capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["search", "x", *argv])
    assert exit_.value.code == 2
    capsys.readouterr()


def test_remote_args_send_filters_only_when_set():
    common = dict(k=5, cwd=None, agent=None, machine=None, exclude_machine=None,
                  mode="hybrid", sort="relevance", full=False, as_json=True)
    plain = remote.search_args("q", **common)
    for flag in ("--role", "--subagents", "--min-chars", "--per-session", "--exclude-agent"):
        assert flag not in plain
    args = remote.search_args("q", roles=["assistant"], exclude_agent=["docs"],
                              subagents="exclude", min_chars=80, per_session=2, **common)
    joined = " ".join(args)
    for part in ("--role assistant", "--exclude-agent docs", "--subagents exclude",
                 "--min-chars 80", "--per-session 2"):
        assert part in joined


def test_serve_ssh_applies_filters_per_tenant_and_after_merging(tmp_path, monkeypatch,
                                                                fake_embed, capsys):
    monkeypatch.setenv("AGENT_HISTORY_TENANTS", str(tmp_path / "tenants"))
    for tenant in ("kim", "globex"):
        with config.tenant_home(tenant):
            db, _ = store.open_for_write()
            for n in range(3):
                put(db, f"{tenant} {TOPIC} step {n}", session=f"{tenant}-long")
            put(db, f"You are a subagent: {tenant} {TOPIC}", session=f"{tenant}-p",
                role="user", sidechain=True, agent_id="agent-x")
            db.close()
    command = (f"search -k 10 --mode text --subagents exclude --per-session 1 "
               f"--json -- {TOPIC}")
    assert serve_ssh.run("kim,globex", command) == 0
    rows = json.loads(capsys.readouterr().out)
    assert sorted((r["tenant"], r["session_id"]) for r in rows) == [
        ("globex", "globex-long"), ("kim", "kim-long")]


def test_eval_can_try_a_filter_before_it_becomes_a_default(indexed, tmp_path, capsys):
    path = tmp_path / "q.jsonl"
    path.write_text(json.dumps({"query": TOPIC, "expect": "codex:"}) + "\n")
    assert cli.main(["eval", str(path), "--mode", "text", "-k", "5", "--json"]) == 0
    plain = json.loads(capsys.readouterr().out)
    assert cli.main(["eval", str(path), "--mode", "text", "-k", "5", "--json",
                     "--per-session", "1", "--subagents", "exclude"]) == 0
    filtered = json.loads(capsys.readouterr().out)
    assert plain["filters"] == {}
    assert filtered["filters"] == {"per_session": 1, "subagents": "exclude"}
    before, after = plain["results"][0]["ranks"][0], filtered["results"][0]["ranks"][0]
    assert before and after and after < before, \
        "dropping the subagent brief moves the real answer up"
    assert cli.main(["eval", str(path), "--mode", "text", "--subagents", "exclude"]) == 0
    assert "filters: subagents=exclude" in capsys.readouterr().out


# --- how the per-session cap fills k, and edge values ----------------------------

def _many(db, *, strong_sessions, strong_each, weak):
    """Strong old matches (every term) and weak new ones (one term), the weak
    ones each in a conversation of their own."""
    for s in range(strong_sessions):
        for n in range(strong_each):
            put(db, f"{TOPIC}: strong {s}-{n}", session=f"s{s}",
                ts=f"2026-01-{s + 1:02d}T00:{n:02d}:00Z")
    for n in range(weak):
        put(db, f"billing aside number {n}", session=f"w{n}",
            ts=f"2026-10-{n + 1:02d}T00:00:00Z")


@pytest.mark.parametrize("sort", ["relevance", "newest"])
def test_a_cap_that_never_binds_changes_nothing(fake_embed, sort):
    db, _ = store.open_for_write()
    _many(db, strong_sessions=20, strong_each=1, weak=20)
    db.close()
    plain = search.search_rows(TOPIC, k=8, mode="text", sort=sort)
    capped = search.search_rows(TOPIC, k=8, mode="text", sort=sort, per_session=100)
    assert sessions(capped) == sessions(plain)
    assert all(s.startswith("s") for s in sessions(capped)), "no weak match got in"


def test_a_cap_that_never_binds_keeps_the_hybrid_order(fake_embed):
    db, _ = store.open_for_write()
    _many(db, strong_sessions=60, strong_each=1, weak=60)
    db.close()
    plain = search.search_rows(TOPIC, k=8)
    capped = search.search_rows(TOPIC, k=8, per_session=100)
    assert [(r["session_id"], r["score"]) for r in capped] == \
        [(r["session_id"], r["score"]) for r in plain]


@pytest.mark.parametrize("mode", ["text", "vector", "hybrid"])
def test_a_binding_cap_fetches_deeper_to_fill_k(fake_embed, mode):
    db, _ = store.open_for_write()
    for n in range(60):
        put(db, f"{TOPIC} {TOPIC} {TOPIC}: long step {n}", session="long",
            ts=f"2026-06-01T{n // 60:02d}:{n % 60:02d}:00Z")
    for m in range(10):
        put(db, f"{TOPIC}, conversation {m}", session=f"c{m}")
    db.close()
    if mode == "text":
        top = search.search_rows(TOPIC, k=40, mode="text")
        assert sessions(top).count("long") == 40, "the long session fills the first 40"
    rows = search.search_rows(TOPIC, k=8, mode=mode, per_session=1)
    assert len(rows) == 8 and len(set(sessions(rows))) == 8


def test_with_newest_the_cap_picks_by_relevance_then_sorts(fake_embed):
    db, _ = store.open_for_write()
    _many(db, strong_sessions=4, strong_each=5, weak=20)
    db.close()
    rows = search.search_rows(TOPIC, k=4, mode="text", sort="newest", per_session=1)
    assert sessions(rows) == ["s3", "s2", "s1", "s0"], \
        "the four relevant conversations, newest first, not four weak newer ones"


@pytest.mark.parametrize("bad", [0, -1])
def test_per_session_below_one_is_refused(indexed, bad):
    with pytest.raises(ValueError, match="per_session"):
        hits(per_session=bad)
    with pytest.raises(ValueError):
        search.cap_per_session([], bad)


@pytest.mark.parametrize("empty", [",", " , ", ""])
def test_an_agent_list_naming_nothing_narrows_nothing(indexed, empty):
    assert sessions(hits(agent=empty)) == sessions(hits())


def test_min_chars_leaves_curated_facts_alone(indexed):
    facts.save(facts.build("billing service", "deploy", "blue/green"))
    rows = search.search_rows("billing service deploy", k=5, mode="text", min_chars=500)
    assert [r["role"] for r in rows] == ["fact"], "a short fact stays; short chunks go"


def test_viewer_refuses_a_per_session_below_one(indexed):
    import http.client
    import threading
    from http.server import ThreadingHTTPServer

    from agent_history import viewer

    server = ThreadingHTTPServer((viewer.HOST, 0), viewer.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1])
        for value in ("-1", "0", "x"):
            conn.request("GET", f"/api/search?q=deploy&mode=text&per_session={value}")
            resp = conn.getresponse()
            assert resp.status == 400 and b"per_session" in resp.read()
        conn.request("GET", "/api/search?q=deploy&mode=text&per_session=1&min_chars=")
        resp = conn.getresponse()
        rows = json.loads(resp.read())
        assert resp.status == 200 and len(set(sessions(rows))) == len(rows)
    finally:
        server.shutdown()
        server.server_close()


# --- agent lists against a hub -----------------------------------------------------

def _old_hub(monkeypatch, calls):
    """A hub from before agent lists: ``--agent a,b`` is one unknown agent."""
    def query_json(command, require="text"):
        calls.append(command)
        agent = command[command.index("--agent") + 1] if "--agent" in command else None
        if agent and "," in agent:
            return []
        return [{"agent": agent or "hermes", "file": f"/hub/{agent}.jsonl",
                 "session_id": f"hub-{agent}", "cwd": "/srv",
                 "timestamp": "2026-06-02T00:00:00Z", "role": "assistant",
                 "text": f"{agent} on the hub: {TOPIC}", "machine": "server",
                 "distance": None, "score": 0.02}]
    monkeypatch.setattr(remote, "query_json", query_json)
    monkeypatch.setenv("AGENT_HISTORY_REMOTE", "ssh://reader@hub.example.invalid")


def _agents_sent(calls):
    return sorted(c[c.index("--agent") + 1] for c in calls if "--agent" in c)


def test_an_agent_list_is_asked_of_the_hub_one_agent_at_a_time(indexed, monkeypatch, capsys):
    calls: list = []
    _old_hub(monkeypatch, calls)
    assert cli.main(["search", TOPIC, "--mode", "text", "--json",
                     "--agent", "codex,claude-code"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert _agents_sent(calls) == ["claude-code", "codex"]
    hub = {r["agent"] for r in rows if r["machine"] == "server"}
    assert hub == {"codex", "claude-code"}, "an older hub answers each agent"


def test_hub_only_search_with_an_agent_list(tmp_path, monkeypatch, capsys):
    calls: list = []
    _old_hub(monkeypatch, calls)
    assert not config.db_path().exists()
    assert cli.main(["search", TOPIC, "--json", "--agent", "codex,claude-code",
                     "--per-session", "1"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert _agents_sent(calls) == ["claude-code", "codex"]
    assert {r["agent"] for r in rows} == {"codex", "claude-code"}
    assert all("--per-session" in c for c in calls)
