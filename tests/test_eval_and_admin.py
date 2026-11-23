"""`eval` (retrieval metrics), `mcp token rotate`, and `purge --tenant`."""
from __future__ import annotations

import hashlib
import json

import pytest

from agent_history import cli, config, embed, evaluate, mcp_auth, store

needs_vec = pytest.mark.skipif(not store.extensions_supported(),
                               reason="this Python cannot load SQLite extensions")


def put(db, text, *, session, ts="2026-06-01T00:00:00Z", agent="claude-code"):
    store.insert_chunk(
        db, agent=agent, origin=f"/t/{session}.jsonl", session_id=session,
        cwd="/w", timestamp=ts, role="assistant", text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        vector=embed.embed(text))
    db.commit()


@pytest.fixture
def indexed(fake_embed):
    db, _ = store.open_for_write()
    put(db, "we split the auth middleware because session refresh runs first",
        session="auth")
    put(db, "the resolver failed with os error 11001 on the windows box",
        session="resolver")
    put(db, "kitchen renovation budget spreadsheet numbers", session="kitchen")
    db.close()


def write(tmp_path, lines):
    path = tmp_path / "queries.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


# --- eval -------------------------------------------------------------------------

def test_metrics_from_ranks():
    assert evaluate.score([1, 2, None, 4], k=3) == {
        "queries": 4, "hit_at_1": 0.25, "recall_at_k": 0.5, "mrr": round((1 + 0.5) / 4, 4)}
    assert evaluate.score([], k=3)["recall_at_k"] == 0.0


def test_expectations_match_on_every_given_field():
    hit = {"text": "Session Refresh runs first", "session_id": "s1", "session_uid": "u1",
           "agent": "codex", "file": "/home/x/.codex/sessions/a.jsonl"}
    assert evaluate.matches(hit, {"text": "session refresh"})
    assert evaluate.matches(hit, {"session": "u1", "agent": "codex"})
    assert evaluate.matches(hit, {"file": "sessions/a.jsonl"})
    assert not evaluate.matches(hit, {"session": "s1", "agent": "claude-code"})
    assert evaluate.first_rank([{"text": "no"}, hit], [{"text": "nope"}, {"session": "s1"}]) == 2


@pytest.mark.parametrize("line, why", [
    ("not json", "not JSON"),
    ('{"expect": "x"}', "query"),
    ('{"query": "q"}', "expect"),
    ('{"query": "q", "expect": {"colour": "red"}}', "expect"),
    ('{"query": "q", "expect": "x", "since": "whenever"}', "not a date"),
])
def test_a_bad_queries_file_is_refused(tmp_path, line, why):
    with pytest.raises(evaluate.EvalError, match=why):
        evaluate.load(write(tmp_path, [line]))


def test_comments_and_blank_lines_are_skipped(tmp_path):
    queries = evaluate.load(write(tmp_path, ["# a comment", "", '{"query": "q", "expect": "x"}']))
    assert len(queries) == 1 and queries[0]["line"] == 3


@needs_vec
def test_eval_scores_the_real_search(indexed, tmp_path, capsys):
    path = write(tmp_path, [
        '{"query": "why did we split the auth middleware", "expect": "session refresh"}',
        '{"query": "os error 11001", "expect": {"session": "resolver"}}',
        '{"query": "something never discussed anywhere", "expect": "zebra migration"}',
    ])
    assert cli.main(["eval", path, "--mode", "text", "-k", "3", "--json"]) == 0
    report = json.loads(capsys.readouterr().out)
    (result,) = report["results"]
    assert report["queries"] == 3 and report["chunks"] == 3
    assert result["ranks"][:2] == [1, 1] and result["ranks"][2] is None
    assert result["recall_at_k"] == pytest.approx(2 / 3, abs=1e-3)
    assert result["hit_at_1"] == pytest.approx(2 / 3, abs=1e-3)

    assert cli.main(["eval", path, "--mode", "all", "--recency", "30d", "--details"]) == 0
    out = capsys.readouterr().out
    for label in ("hybrid", "vector", "text", "text+rec30d"):
        assert label in out
    assert "recall@10" in out and "line   3" in out


def test_eval_without_an_index_says_so(tmp_path, capsys):
    path = write(tmp_path, ['{"query": "q", "expect": "x"}'])
    assert cli.main(["eval", path]) == 1
    assert "error" in capsys.readouterr().err


# --- token rotation -----------------------------------------------------------------

def test_rotate_replaces_the_secret_and_keeps_the_identity(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_HISTORY_MCP_TOKENS", str(tmp_path / "tokens.json"))
    old = mcp_auth.add_token("router-read", ["read"], "kim")
    (entry,) = mcp_auth.list_tokens()
    assert entry["created"] and entry["age_days"] == 0 and entry["rotated"] is None

    assert cli.main(["mcp", "token", "rotate", "router-read"]) == 0
    cap = capsys.readouterr()
    new = cap.out.strip()
    assert new and new != old and "shown only this once" in cap.err

    tokens = mcp_auth.TokenStore()
    assert tokens.authenticate(old) is None, "the old secret stops working"
    who = tokens.authenticate(new)
    assert who.name == "router-read" and who.scopes == {"read"} and who.tenant == "kim"
    (entry,) = mcp_auth.list_tokens()
    assert entry["rotated"] and entry["age_days"] == 0
    assert "sha256" not in entry

    assert cli.main(["mcp", "token", "rotate", "nobody"]) == 1
    capsys.readouterr()


def test_tokens_from_before_dates_have_no_age(tmp_path, monkeypatch):
    path = tmp_path / "tokens.json"
    monkeypatch.setenv("AGENT_HISTORY_MCP_TOKENS", str(path))
    mcp_auth.add_token("legacy", ["read"], None)
    raw = json.loads(path.read_text())
    raw["tokens"][0].pop("created")
    path.write_text(json.dumps(raw))
    path.chmod(0o600)
    assert mcp_auth.list_tokens()[0]["age_days"] is None


# --- purge --tenant -------------------------------------------------------------------

@needs_vec
def test_purge_reaches_one_tenant_only(tmp_path, monkeypatch, fake_embed, capsys):
    monkeypatch.setenv("AGENT_HISTORY_TENANTS", str(tmp_path / "tenants"))
    for tenant in ("kim", "globex"):
        with config.tenant_home(tenant):
            db, _ = store.open_for_write()
            put(db, f"{tenant} private plan number one", session=f"{tenant}-1")
            db.close()

    assert cli.main(["purge", "--tenant", "kim", "--all"]) == 0
    assert "would delete 1" in capsys.readouterr().out
    assert cli.main(["purge", "--tenant", "kim", "--all", "--yes"]) == 0
    assert "tenant: kim" in capsys.readouterr().out
    with config.tenant_home("kim"):
        assert store.stats(store.open_for_read(check_model=False))["total"] == 0
    with config.tenant_home("globex"):
        assert store.stats(store.open_for_read(check_model=False))["total"] == 1

    assert cli.main(["purge", "--tenant", "../etc", "--all"]) == 1
    assert cli.main(["purge", "--tenant", "nobody", "--all"]) == 1
    capsys.readouterr()
