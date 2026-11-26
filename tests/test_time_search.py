"""Search over the time axis: --since/--until/--as-of and --recency."""
from __future__ import annotations

import datetime
import hashlib
import json

import pytest

from agent_history import cli, embed, remote, search, serve_ssh, store, timeaxis

pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)

OLD = "2026-03-01T10:00:00Z"
MID = "2026-07-01T10:00:00Z"
NEW = "2026-11-01T10:00:00Z"


def put(db, text, ts, *, session=None):
    session = session or hashlib.sha1(text.encode()).hexdigest()[:8]
    store.insert_chunk(
        db, agent="claude-code", origin=f"/t/{session}.jsonl", session_id=session,
        cwd="/w", timestamp=ts, role="assistant", text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        vector=embed.embed(text))
    db.commit()


@pytest.fixture
def db(fake_embed):
    db, _ = store.open_for_write()
    put(db, "the default model for the fleet is claude-3.7 sonnet", OLD)
    put(db, "the default model for the fleet moved to opus five", MID)
    put(db, "the default model for the fleet is now claude-opus-5-5", NEW)
    put(db, "an undated note about the default model for the fleet", "")
    return db


def texts(rows):
    return [r["text"] for r in rows]


@pytest.mark.parametrize("mode", ["text", "vector", "hybrid"])
def test_as_of_never_returns_a_newer_chunk(db, mode):
    window = timeaxis.window(as_of="2026-07-01")
    rows = search.search_rows("default model fleet", k=10, mode=mode, window=window)
    assert rows, "the past as it stood must still answer"
    for row in rows:
        assert timeaxis.instant(row["timestamp"]) <= window.until
    assert "now claude-opus-5-5" not in " ".join(texts(rows))


@pytest.mark.parametrize("mode", ["text", "vector", "hybrid"])
def test_since_keeps_only_later_chunks(db, mode):
    rows = search.search_rows("default model fleet", k=10, mode=mode,
                              window=timeaxis.window(since="2026-06-01"))
    assert sorted(r["timestamp"] for r in rows) == [MID, NEW]


def test_a_window_on_both_sides(db):
    rows = search.search_rows("default model fleet", k=10, mode="text",
                              window=timeaxis.window(since="2026-04-01", until="2026-08-01"))
    assert [r["timestamp"] for r in rows] == [MID]


def test_until_a_bare_date_includes_that_whole_day(db):
    rows = search.search_rows("default model fleet", k=10, mode="text",
                              window=timeaxis.window(until="2026-11-01"))
    assert NEW in [r["timestamp"] for r in rows]


def test_no_window_and_no_recency_is_byte_identical_to_before(db):
    plain = search.search_rows("default model fleet", k=10, mode="hybrid")
    explicit = search.search_rows("default model fleet", k=10, mode="hybrid",
                                  window=timeaxis.Window(), recency=None)
    assert json.dumps(plain, sort_keys=True) == json.dumps(explicit, sort_keys=True)
    assert all("recency" not in r for r in plain)


def test_recency_reweights_and_reports_its_factor(db):
    anchor = datetime.datetime(2026, 11, 2, tzinfo=datetime.timezone.utc).timestamp()
    rows = [{"text": "old strong", "timestamp": OLD, "score": 0.033, "distance": None},
            {"text": "new weak", "timestamp": NEW, "score": 0.020, "distance": None}]
    month = 30 * 86400.0
    # With a long half-life relevance still wins...
    slow = search.apply_recency(rows, 3650 * 86400.0, anchor)
    assert texts(slow)[0] == "old strong"
    # ...with a month, eight months of age costs more than the score gap.
    fast = search.apply_recency(rows, month, anchor)
    assert texts(fast)[0] == "new weak"
    top = fast[0]
    assert top["relevance"] == 0.020 and 0.9 < top["recency"] <= 1.0
    assert top["score"] == pytest.approx(top["relevance"] * top["recency"], rel=1e-3)


def test_recency_crossover_is_where_the_half_life_says(db):
    anchor = timeaxis.instant(NEW)
    month = 30 * 86400.0
    # One half-life older needs more than twice the relevance to stay ahead.
    older = {"text": "older", "timestamp": "2026-10-02T10:00:00Z", "distance": None}
    newer = {"text": "newer", "timestamp": NEW, "score": 0.010, "distance": None}
    assert texts(search.apply_recency([{**older, "score": 0.0195}, newer],
                                      month, anchor))[0] == "newer"
    assert texts(search.apply_recency([{**older, "score": 0.0205}, newer],
                                      month, anchor))[0] == "older"


def test_recency_works_in_vector_and_text_modes(db):
    for mode in ("vector", "text"):
        rows = search.search_rows("default model fleet", k=3, mode=mode,
                                  recency=timeaxis.parse_span("30d"))
        assert len(rows) == 3
        assert all("recency" in r for r in rows)
        assert rows[0]["timestamp"] == NEW
        assert all(r["timestamp"] for r in rows), "the undated chunk weighs nothing"


def test_recency_is_ignored_for_the_newest_sort(db):
    rows = search.search_rows("default model fleet", k=10, mode="text", sort="newest",
                              recency=timeaxis.parse_span("30d"))
    assert all("recency" not in r for r in rows)


def test_cli_flags(db, capsys):
    assert cli.main(["search", "default model fleet", "--mode", "text",
                     "--as-of", "2026-07-01", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert {r["timestamp"] for r in rows} == {OLD, MID}
    assert cli.main(["search", "default model fleet", "--mode", "text",
                     "--recency", "30d"]) == 0
    assert "rec×" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["--since", "whenever"], ["--recency", "30"], ["--recency", "30m"],
    ["--until", "2026-10-01", "--as-of", "2026-10-02"],
    ["--since", "2026-10-02", "--until", "2026-10-01"],
])
def test_cli_refuses_bad_time_flags(db, argv, capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["search", "x", *argv])
    assert exit_.value.code == 2
    capsys.readouterr()


def test_remote_args_carry_absolute_bounds_and_hours():
    window = timeaxis.window(since="30d")
    args = remote.search_args("q", k=5, cwd=None, agent=None, machine=None,
                              exclude_machine=None, mode="hybrid", sort="relevance",
                              full=False, as_json=True, window=window,
                              recency=timeaxis.parse_span("2w"))
    assert args[args.index("--since") + 1] == timeaxis.iso(window.since)
    assert args[args.index("--recency") + 1] == "336h"
    plain = remote.search_args("q", k=5, cwd=None, agent=None, machine=None,
                               exclude_machine=None, mode="hybrid", sort="relevance",
                               full=False, as_json=True)
    assert "--since" not in plain and "--recency" not in plain, \
        "an older hub must still answer searches that do not use the flags"


def test_serve_ssh_accepts_the_time_flags(tmp_path, monkeypatch, fake_embed, capsys):
    from agent_history import config

    monkeypatch.setenv("AGENT_HISTORY_TENANTS", str(tmp_path / "tenants"))
    with config.tenant_home("kim"):
        db, _ = store.open_for_write()
        put(db, "kim vault rotation was quarterly", OLD)
        put(db, "kim vault rotation is now monthly", NEW)
        db.close()
    command = ("search -k 5 --mode text --until 2026-06-01T00:00:00+00:00 "
               "--recency 720h --json -- vault rotation")
    assert serve_ssh.run("kim", command) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["timestamp"] for r in rows] == [OLD]
    assert serve_ssh.run("kim", "search --since nonsense x") == serve_ssh.REJECTED
    capsys.readouterr()
