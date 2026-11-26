"""`index` and `reindex` from the command line take the indexing lock.

Every other writer of the index (`trigger`, `sync`, `import`, `reembed`)
already holds it. A bare `agent-history index` racing a hook-triggered run
read the same set of indexed hashes, embedded the same new records, and the
second one to insert failed on ``chunks.content_hash``.
"""
from __future__ import annotations

import sqlite3

import pytest

from agent_history import cli, config, index, store, trigger
from conftest import msg

# These drive a real index, which needs the sqlite-vec extension.
pytestmark = pytest.mark.skipif(
    not store.extensions_supported(),
    reason="this Python cannot load SQLite extensions",
)


def chunks() -> int:
    db = sqlite3.connect(config.db_path())
    try:
        return db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    finally:
        db.close()


def test_index_backs_off_while_another_run_holds_the_lock(transcripts,
                                                          fake_embed, capsys):
    transcripts("proj", "s", [msg("user", "a message worth indexing here")])
    config.ensure_data_home()
    with trigger._exclusive(config.lock_path()) as held:
        assert held
        assert cli.main(["index"]) == 1
    assert "lock" in capsys.readouterr().err
    assert not fake_embed, "nothing may be embedded while another run indexes"
    assert not config.db_path().exists()

    # With the lock free again, it indexes.
    assert cli.main(["index"]) == 0
    assert chunks() > 0


def test_reindex_never_resets_the_store_under_another_run(transcripts,
                                                          fake_embed, capsys):
    transcripts("proj", "s", [msg("user", "a message worth indexing here")])
    assert cli.main(["index"]) == 0
    before = chunks()
    assert before > 0

    with trigger._exclusive(config.lock_path()) as held:
        assert held
        assert cli.main(["reindex"]) == 1
    assert "lock" in capsys.readouterr().err
    assert chunks() == before, "the store must not be reset mid-run"


def test_the_lock_is_released_after_a_cli_index(transcripts, fake_embed):
    transcripts("proj", "s", [msg("user", "a message worth indexing here")])
    assert cli.main(["index"]) == 0
    with trigger._exclusive(config.lock_path()) as held:
        assert held, "a finished `index` must not leave the lock held"


def test_trigger_still_indexes_while_it_holds_the_lock(transcripts, fake_embed):
    """`trigger` calls `index.run` with the lock already taken; `run` must not
    try to take it again."""
    transcripts("proj", "s", [msg("user", "a message worth indexing here")])
    assert trigger.run("hook", foreground=True) == 0
    assert chunks() > 0


def test_run_exclusive_wraps_run(monkeypatch):
    seen = []
    monkeypatch.setattr(index, "run", lambda reindex=False, force=False:
                        seen.append((reindex, force)) or 0)
    config.ensure_data_home()
    assert index.run_exclusive(reindex=True, force=True) == 0
    assert seen == [(True, True)]
