"""Facts and the time axis over MCP: history_fact, history_fact_get, recall."""
from __future__ import annotations

import pytest

pytest.importorskip("mcp")

from agent_history import config, facts, store  # noqa: E402
from test_mcp import GH, data, hub, put, run_calls, text_of  # noqa: E402,F401

pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


def index_tenant(hub, tenant="kim"):  # noqa: F811
    """What the hub's indexer does for one tenant's facts."""
    mp = pytest.MonkeyPatch()
    try:
        mp.setenv("AGENT_HISTORY_INBOX", str(hub["inbox"] / tenant))
        with config.tenant_home(tenant):
            assert facts.index_inbox() == 0
    finally:
        mp.undo()


def test_history_fact_queues_a_redacted_fact_then_reads_back(hub, capsys):  # noqa: F811
    async def write(client):
        first = await client.call_tool("history_fact", {
            "subject": "vault", "predicate": "rotation", "value": "quarterly",
            "since": "2026-01-01"})
        second = await client.call_tool("history_fact", {
            "subject": "vault", "predicate": "rotation", "value": f"monthly, token {GH}",
            "since": "2026-10-01", "source": "https://claude.ai/chat/abc"})
        return first, second

    first, second = run_calls(hub["tokens"]["write"], write)
    out = data(second)
    assert facts.FACT_ID.match(out["fact_id"]) and out["redacted"]
    assert GH not in out["value"]
    files = sorted((hub["inbox"] / "kim" / "claude-ai" / "facts").glob("*.jsonl"))
    assert len(files) == 2 and all(GH not in f.read_text() for f in files)
    assert data(first)["status"].startswith("queued")

    index_tenant(hub)
    capsys.readouterr()

    async def read(client):
        now = await client.call_tool("history_fact_get", {"subject": "vault"})
        then = await client.call_tool("history_fact_get",
                                      {"subject": "vault", "as_of": "2026-06-01"})
        every = await client.call_tool("history_fact_get",
                                       {"subject": "vault", "history": True})
        recall = await client.call_tool("history_recall",
                                        {"query": "vault rotation", "mode": "text"})
        return now, then, every, recall

    now, then, every, recall = run_calls(hub["tokens"]["read"], read)
    (live,) = data(now)["facts"]
    assert live["value"].startswith("monthly") and live["previous"]["value"] == "quarterly"
    assert data(then)["facts"][0]["value"] == "quarterly"
    assert [f["current"] for f in data(every)["facts"]] == [False, True]
    top = data(recall)["results"][0]
    assert top["role"] == "fact" and top["fact"]["subject"] == "vault"


def test_history_fact_input_rules(hub):  # noqa: F811
    async def write(client):
        neither = await client.call_tool("history_fact", {"subject": "a", "predicate": "b"})
        both = await client.call_tool("history_fact", {"subject": "a", "predicate": "b",
                                                       "value": "c", "retire": True})
        bad_date = await client.call_tool("history_fact", {
            "subject": "a", "predicate": "b", "value": "c", "since": "whenever"})
        retire = await client.call_tool("history_fact", {
            "subject": "a", "predicate": "b", "retire": True})
        return neither, both, bad_date, retire

    neither, both, bad_date, retire = run_calls(hub["tokens"]["write"], write)
    assert neither.is_error and "value is required" in text_of(neither)
    assert both.is_error and bad_date.is_error
    assert data(retire)["value"] is None


def test_a_read_token_cannot_write_facts_and_tenants_stay_apart(hub):  # noqa: F811
    async def write(client):
        return await client.call_tool("history_fact", {
            "subject": "a", "predicate": "b", "value": "c"})

    assert run_calls(hub["tokens"]["read"], write).is_error
    with config.tenant_home("globex"):
        facts.save(facts.build("globex-only", "secret plan", "bot1"))

    async def read(client):
        return await client.call_tool("history_fact_get", {})

    assert data(run_calls(hub["tokens"]["read"], read))["facts"] == [], \
        "kim's token never opens globex's facts"


def test_recall_takes_a_window_and_a_half_life(hub):  # noqa: F811
    with config.tenant_home("kim"):
        db, _ = store.open_for_write()
        put(db, "vault rotation moved to weekly", session="sess-late",
            timestamp="2026-11-01T00:00:00Z")
        db.commit()
        db.close()

    async def call(client):
        early = await client.call_tool("history_recall", {
            "query": "vault rotation", "mode": "text", "until": "2026-06-01"})
        late = await client.call_tool("history_recall", {
            "query": "vault rotation", "mode": "text", "since": "2026-10-01",
            "recency": "30d"})
        bad = await client.call_tool("history_recall", {
            "query": "vault rotation", "until": "someday"})
        return early, late, bad

    early, late, bad = run_calls(hub["tokens"]["read"], call)
    assert all(r["timestamp"] < "2026-06" for r in data(early)["results"])
    (only,) = data(late)["results"]
    assert only["session_id"] == "sess-late" and 0 < only["recency"] <= 1
    assert bad.is_error and "not a date" in text_of(bad)
