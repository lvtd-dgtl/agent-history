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
        db = _snapshot(path, Path(tmp))
        if db is None:
            yield None
            return
        try:
            yield db
        finally:
            db.close()


SNAPSHOT_TRIES = 4


def _stamp(path: Path):
    try:
        st = path.stat()
    except FileNotFoundError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _snapshot(path: Path, tmp: Path) -> sqlite3.Connection | None:
    """A consistent private copy of `path`, or None.

    A rollback journal means a writer may have uncommitted pages in the main
    file that only the journal can undo, so such a source is not copied. The
    main file, -wal and -shm are copied while the writer may still be
    checkpointing, which could pair old main-file pages with a newer WAL; so
    the three files' size and mtime are compared before and after each copy
    and the copy is retried if anything moved. (The backup API would need a
    read-only connection to the source, which is exactly what fails on a
    Windows drive.) Never settling gives up.
    """
    names = ("", "-wal", "-shm")
    srcs = [path.with_name(path.name + n) for n in names]
    copy = tmp / path.name
    try:
        if path.with_name(path.name + "-journal").is_file():
            return None
        for _ in range(SNAPSHOT_TRIES):
            before = [_stamp(s) for s in srcs]
            for s, n in zip(srcs, names):
                dest = copy.with_name(copy.name + n)
                if s.is_file():
                    shutil.copyfile(s, dest)
                elif dest.exists():
                    dest.unlink()
            if before != [_stamp(s) for s in srcs]:
                continue
            return _connect(copy)
    except (OSError, sqlite3.Error):
        # An unreadable store (permissions, metadata or copy failure) is
        # skipped; a file that is merely absent is handled by _stamp.
        return None
    return None
