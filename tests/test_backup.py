"""Backups before risky writes, and integrity checks of the live data."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import namedtuple

import pytest

from agent_history import __version__, backup, cli, config, embed, facts, store

pytestmark = pytest.mark.skipif(not store.extensions_supported(),
                                reason="this Python cannot load SQLite extensions")


def put(db, text, session="s1"):
    store.insert_chunk(
        db, agent="claude-code", origin=f"/t/{session}.jsonl", session_id=session,
        cwd="/w", timestamp="2026-06-01T00:00:00Z", role="assistant", text=text,
        content_hash=hashlib.sha256(text.encode()).hexdigest(),
        vector=embed.embed(text))


@pytest.fixture
def indexed(fake_embed):
    db, _ = store.open_for_write()
    for n in range(5):
        put(db, f"deploy notes number {n}", session="keep")
    for n in range(3):
        put(db, f"secret session line {n}", session="doomed")
    db.commit()
    db.close()
    facts.save(facts.build("gateway", "host", "10.0.0.9"))


def chunks() -> int:
    db = sqlite3.connect(config.db_path())
    try:
        return db.execute("SELECT count(*) FROM chunks").fetchone()[0]
    finally:
        db.close()


# --- making one ------------------------------------------------------------------

def test_a_backup_holds_every_store_with_a_manifest(indexed, monkeypatch):
    (config.data_home() / "mcp-tokens.json").write_text('{"tokens": []}\n')
    (config.data_home() / "outbox").mkdir()
    (config.data_home() / "outbox" / "batch-1.jsonl").write_text("{}\n")
    made = backup.snapshot("manual", manual=True, label="before the test")
    manifest = backup.read_manifest(made)
    assert {"index.db", "facts.db"} <= set(manifest["files"])
    entry = manifest["files"]["index.db"]
    assert entry["integrity"] == "ok" and entry["tables"]["chunks"] == 8
    assert "vec_chunks_rowids" not in entry["tables"], "shadow tables are not content"
    assert manifest["files"]["facts.db"]["tables"]["facts"] == 1
    assert manifest["version"] == __version__ and manifest["manual"]
    assert set(manifest["extras"]) == {"mcp-tokens.json", "outbox"}
    assert (made / "outbox" / "batch-1.jsonl").exists()
    assert made.name.endswith("before-the-test")
    assert backup.verify(made) == []
    if os.name != "nt":
        assert oct(made.stat().st_mode & 0o777) == "0o700"
        assert oct((made / "index.db").stat().st_mode & 0o777) == "0o600"


def test_a_damaged_backup_is_reported(indexed):
    made = backup.snapshot("manual", manual=True)
    copy = made / "index.db"
    data = bytearray(copy.read_bytes())
    data[len(data) // 2] ^= 0xFF
    copy.write_bytes(bytes(data))
    (problem,) = backup.verify(made)
    assert "index.db" in problem and "SHA-256" in problem
    (made / "facts.db").unlink()
    assert any("facts.db: missing" in p for p in backup.verify(made))


def test_no_space_means_no_backup_and_no_change(indexed, monkeypatch, capsys):
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda _p: Usage(10, 10, 1024))
    assert cli.main(["purge", "--session", "doomed", "--yes"]) == 1
    err = capsys.readouterr().err
    assert "backup failed, so nothing was changed" in err and "free space" in err
    assert chunks() == 8


def test_auto_backup_can_be_turned_off_on_purpose(indexed, monkeypatch, capsys):
    monkeypatch.setenv("AGENT_HISTORY_AUTO_BACKUP", "0")
    assert cli.main(["purge", "--session", "doomed", "--yes"]) == 0
    capsys.readouterr()
    assert chunks() == 5 and backup.backups() == []


# --- before risky writes --------------------------------------------------------------

def test_purge_backs_up_first_then_checks(indexed, capsys):
    assert cli.main(["purge", "--session", "doomed", "--yes"]) == 0
    err = capsys.readouterr().err
    (made, manifest), = backup.backups()
    assert manifest["reason"] == "purge" and not manifest["manual"]
    assert manifest["files"]["index.db"]["tables"]["chunks"] == 8
    assert chunks() == 5
    assert "integrity: ok (removed: index.db: chunks 8 → 5 (-3))" in err
    assert made.name in err and "still in backup" in err


def test_fact_forget_backs_up_first(indexed, capsys):
    assert cli.main(["fact", "forget", "gateway", "--yes"]) == 0
    capsys.readouterr()
    (_made, manifest), = backup.backups()
    assert manifest["reason"] == "fact forget"
    assert manifest["files"]["facts.db"]["tables"]["facts"] == 1
    assert facts.rows("gateway") == []


def test_restore_brings_purged_rows_back(indexed, capsys):
    assert cli.main(["purge", "--session", "doomed", "--yes"]) == 0
    (made, _), = backup.backups()
    assert cli.main(["backup", "restore", made.name]) == 0
    assert "would restore" in capsys.readouterr().out and chunks() == 5
    assert cli.main(["backup", "restore", made.name, "--yes"]) == 0
    out = capsys.readouterr().out
    assert chunks() == 8 and "integrity: ok" in out
    pre = [m for _p, m in backup.backups() if m["manual"]]
    assert len(pre) == 1 and pre[0]["files"]["index.db"]["tables"]["chunks"] == 5, \
        "what the restore replaced is kept too"
    rows = store.text_query(store.open_for_read(check_model=False), "secret session", 5)
    assert len(rows) == 3, "restored chunks are searchable again"


def test_a_damaged_backup_is_never_restored(indexed, capsys):
    made = backup.snapshot("manual", manual=True)
    (made / "index.db").write_bytes(b"not a database")
    assert cli.main(["backup", "restore", made.name, "--yes"]) == 1
    assert "not intact" in capsys.readouterr().err
    assert chunks() == 8


def test_only_automatic_backups_are_pruned(indexed, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_BACKUP_KEEP", "2")
    manual = backup.snapshot("manual", manual=True)
    for n in range(4):
        backup.snapshot(f"auto {n}")
    names = [m["reason"] for _p, m in backup.backups()]
    assert names == ["manual", "auto 2", "auto 3"]
    assert manual.exists()


def test_backup_dir_can_live_elsewhere(indexed, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_HISTORY_BACKUP_DIR", str(tmp_path / "other-disk"))
    made = backup.snapshot("manual", manual=True)
    assert made.parent == tmp_path / "other-disk" / config.data_home().name


# --- the first write by a new version ---------------------------------------------------

def test_a_new_version_backs_up_before_its_first_write(indexed, capsys):
    stamp = config.data_home() / backup.STAMP
    assert stamp.read_text().strip() == __version__, "a fresh store is stamped, not copied"
    assert backup.backups() == []
    stamp.write_text("0.18.0\n")
    db, _ = store.open_for_write()
    db.close()
    (made, manifest), = backup.backups()
    assert manifest["reason"] == f"before {__version__} (was 0.18.0)"
    assert made.name.endswith(f"upgrade-to-{__version__}")
    assert stamp.read_text().strip() == __version__
    assert "first write by agent-history" in capsys.readouterr().err
    db, _ = store.open_for_write()
    db.close()
    assert len(backup.backups()) == 1, "once per version"


def test_an_install_from_before_stamps_is_backed_up_once(indexed):
    (config.data_home() / backup.STAMP).unlink()
    facts.save(facts.build("gateway", "port", "8443"))
    (_made, manifest), = backup.backups()
    assert "was unknown" in manifest["reason"]


def test_a_reader_that_must_migrate_backs_up_first(fake_embed):
    config.ensure_data_home()
    db = sqlite3.connect(config.db_path())
    db.executescript("""
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE chunks (id INTEGER PRIMARY KEY, agent TEXT, origin TEXT,
            session_id TEXT, cwd TEXT, timestamp TEXT, role TEXT, text TEXT,
            content_hash TEXT UNIQUE);
        INSERT INTO chunks(agent, origin, session_id, text, content_hash)
            VALUES ('claude-code', '/t/a.jsonl', 'a', 'old row', 'h1');
    """)
    db.commit()
    db.close()
    store.open_for_read(check_model=False).close()
    (_made, manifest), = backup.backups()
    assert manifest["files"]["index.db"]["tables"]["chunks"] == 1
    assert store.has_column(store.open_for_read(check_model=False), "chunks", "machine")


# --- checking the live data ---------------------------------------------------------------

def test_check_passes_on_healthy_data_and_reports_growth(indexed, capsys):
    backup.snapshot("manual", manual=True)
    db, _ = store.open_for_write()
    put(db, "one more chunk", session="new")
    db.commit()
    db.close()
    assert cli.main(["backup", "check", "--no-loss"]) == 0
    out = capsys.readouterr().out
    assert "grew   index.db: chunks 8 → 9 (+1)" in out
    assert "full-text check ok" in out and out.strip().endswith("integrity: ok")


def test_check_catches_a_chunk_without_its_vector(indexed, capsys):
    db = store.open_for_maintenance()
    db.execute("DELETE FROM vec_chunks WHERE rowid = (SELECT max(id) FROM chunks)")
    db.commit()
    db.close()
    assert cli.main(["backup", "check", "--against", "none"]) == 1
    assert "8 chunks but 7 vectors" in capsys.readouterr().out


def test_check_no_loss_fails_after_rows_disappear(indexed, monkeypatch, capsys):
    backup.snapshot("manual", manual=True)
    monkeypatch.setenv("AGENT_HISTORY_AUTO_BACKUP", "0")
    assert cli.main(["purge", "--session", "doomed", "--yes"]) == 0
    capsys.readouterr()
    assert cli.main(["backup", "check"]) == 0, "fewer rows alone is reported, not fatal"
    assert "FEWER  index.db: chunks 8 → 5 (-3)" in capsys.readouterr().out
    assert cli.main(["backup", "check", "--no-loss", "--json"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert not report["ok"] and report["losses"] == ["index.db: chunks 8 → 5 (-3)"]


def test_check_catches_a_corrupt_store(indexed, capsys):
    path = config.db_path()
    data = bytearray(path.read_bytes())
    page = 4096
    for offset in range(page * 2, min(len(data), page * 6)):
        data[offset] = 0
    path.write_bytes(bytes(data))
    assert cli.main(["backup", "check", "--against", "none"]) == 1
    assert "PROBLEM" in capsys.readouterr().out


# --- the command ------------------------------------------------------------------------

def test_backup_commands(indexed, capsys):
    assert cli.main(["backup"]) == 0
    assert "backup:" in capsys.readouterr().out
    assert cli.main(["backup", "create", "--label", "pre-upgrade"]) == 0
    capsys.readouterr()
    assert cli.main(["backup", "list"]) == 0
    listing = capsys.readouterr().out
    assert "pre-upgrade" in listing and "manual" in listing
    assert cli.main(["backup", "verify"]) == 0
    assert "intact" in capsys.readouterr().out
    latest = backup.backups()[-1][0]
    assert cli.main(["backup", "delete", latest.name]) == 0
    assert latest.exists() and "would delete" in capsys.readouterr().out
    assert cli.main(["backup", "delete", latest.name, "--yes"]) == 0
    assert not latest.exists()
    capsys.readouterr()


def test_backup_every_tenant(tmp_path, monkeypatch, fake_embed, capsys):
    monkeypatch.setenv("AGENT_HISTORY_TENANTS", str(tmp_path / "tenants"))
    for tenant in ("kim", "globex"):
        with config.tenant_home(tenant):
            db, _ = store.open_for_write()
            put(db, f"{tenant} chunk")
            db.commit()
            db.close()
    assert cli.main(["backup", "create", "--all-tenants"]) == 0
    out = capsys.readouterr().out
    assert "== globex" in out and "== kim" in out
    for tenant in ("kim", "globex"):
        with config.tenant_home(tenant):
            (_p, manifest), = backup.backups()
            assert manifest["files"]["index.db"]["tables"]["chunks"] == 1
    assert cli.main(["backup", "check", "--all-tenants", "--no-loss"]) == 0
    capsys.readouterr()
    assert cli.main(["backup", "create", "--tenant", "../etc"]) == 1
    capsys.readouterr()
