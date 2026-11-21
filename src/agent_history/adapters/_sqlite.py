"""Read-only SQLite access for live databases other programs are writing.

A WAL-mode database on a Windows drive (/mnt/c from WSL) often cannot be
opened read-only in place: the shared-memory file cannot be mapped and the
first query fails with "disk I/O error". `open_readonly` tries the plain
read-only open first and, only when that fails, works from a private copy of
the database plus its -wal and -shm files. The source is never written to.
"""
from __future__ import annotations

import contextlib
import shutil
import sqlite3
import tempfile
import urllib.parse
from pathlib import Path
from typing import Iterator


def _connect(path: Path) -> sqlite3.Connection:
    # Percent-encode: '#', '?' or '%' in a path would otherwise be parsed as
    # URI syntax and open the wrong file.
    db = sqlite3.connect(f"file:{urllib.parse.quote(str(path))}?mode=ro", uri=True)
    try:
        db.execute("SELECT count(*) FROM sqlite_master").fetchone()
    except sqlite3.Error:
        db.close()
        raise
    return db


@contextlib.contextmanager
def open_readonly(path: Path) -> Iterator[sqlite3.Connection | None]:
    """Yield a read-only connection to `path`, or None if it cannot be read."""
    try:
        db = _connect(path)
    except sqlite3.Error:
        db = None
    if db is not None:
        try:
            yield db
        finally:
            db.close()
        return
    with tempfile.TemporaryDirectory(prefix="agent-history-") as tmp:
        copy = Path(tmp) / path.name
        try:
            for suffix in ("", "-wal", "-shm"):
                src = path.with_name(path.name + suffix)
                if src.is_file():
                    shutil.copyfile(src, copy.with_name(copy.name + suffix))
            db = _connect(copy)
        except (OSError, sqlite3.Error):
            yield None
            return
        try:
            yield db
        finally:
            db.close()
