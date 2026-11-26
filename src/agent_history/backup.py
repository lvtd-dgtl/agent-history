"""Backups of a data home, and integrity checks of the live data.

The index is often the only copy of a conversation: agents delete old
transcripts, and a shipper's files leave the machine once the hub has them.
So every store is backed up before anything that could lose rows:

- the first write by a new version (a schema upgrade, or code that writes
  differently), detected by the version stamp in the data home;
- every command that deletes or rewrites rows: reindex, reembed, purge,
  scrub, relabel, ``notes migrate-records``, ``fact forget``.

A backup is a directory under ``<data home>/backups/`` (or
``$AGENT_HISTORY_BACKUP_DIR``) holding a consistent copy of each SQLite store,
made with SQLite's backup API so it is safe while other processes read and
write, plus the token file and the not-yet-shipped outboxes. Its
``manifest.json`` records each file's size, SHA-256, ``PRAGMA
integrity_check`` result and per-table row counts. It is written under a
temporary name and renamed into place only when complete, so a backup that
exists is a whole one.

``check`` compares the live stores with a backup: corruption, the index's
three views of the chunks (rows, full-text, vectors) disagreeing, and any
table that has fewer rows than the backup recorded.

If a backup cannot be made (no space, unreadable store) the operation that
asked for it does not run: losing the safety net is never silent. Set
``AGENT_HISTORY_AUTO_BACKUP=0`` to run without one, for example to erase data
on purpose; backups keep what a later purge or ``fact forget`` removes until
they are deleted.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, config

MANIFEST = "manifest.json"
STAMP = ".written-by"
PARTIAL = ".partial-"
DEFAULT_KEEP = 5            # automatic backups kept per data home
SPACE_MARGIN = 64 * 2**20   # free space to leave beyond the copy itself
LOCK_TIMEOUT = 600          # seconds to wait for another backup
EXTRA_FILES = ("mcp-tokens.json",)
EXTRA_DIRS = ("outbox", "facts-outbox")
_SQLITE_HEADER = b"SQLite format 3\x00"


class BackupError(RuntimeError):
    """A backup could not be made, read or restored."""


class IntegrityError(RuntimeError):
    """The live data failed its check after an operation."""


# ------------------------------------------------------------------- settings

def auto_enabled() -> bool:
    value = (config._env("AGENT_HISTORY_AUTO_BACKUP", "") or "").strip().lower()
    return value not in ("0", "false", "no", "off")


def keep() -> int:
    raw = (config._env("AGENT_HISTORY_BACKUP_KEEP", "") or "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_KEEP
    except ValueError:
        return DEFAULT_KEEP


def root() -> Path:
    """Where this data home's backups go. With $AGENT_HISTORY_BACKUP_DIR
    (another disk, say) each data home gets its own folder there, so a hub's
    tenants never share one."""
    raw = (config._env("AGENT_HISTORY_BACKUP_DIR", "") or "").strip()
    if raw:
        return Path(raw).expanduser() / config.data_home().name
    return config.data_home() / "backups"


# ---------------------------------------------------------------- what to copy

def stores() -> list[Path]:
    """Every SQLite database directly in the data home."""
    home = config.data_home()
    if not home.is_dir():
        return []
    found = []
    for path in sorted(home.glob("*.db")):
        try:
            with open(path, "rb") as fh:
                if fh.read(16) == _SQLITE_HEADER:
                    found.append(path)
        except OSError:
            continue
    return found


def _extras() -> list[Path]:
    home = config.data_home()
    return [p for p in (*(home / f for f in EXTRA_FILES), *(home / d for d in EXTRA_DIRS))
            if p.exists()]


def _connect(path: Path, *, readonly: bool = False) -> sqlite3.Connection:
    """A connection with sqlite-vec loaded when it can be, so virtual tables
    the stores use are understood by integrity checks."""
    if readonly:
        from urllib.parse import quote
        db = sqlite3.connect(f"file:{quote(str(path))}?mode=ro", uri=True, timeout=60)
    else:
        db = sqlite3.connect(path, timeout=60)
    try:
        import sqlite_vec

        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
    except Exception:  # noqa: BLE001 - without the extension, vec tables are skipped
        pass
    return db


# ------------------------------------------------------------------- describe

def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _tables(db: sqlite3.Connection) -> dict[str, int]:
    """Row count of every table that holds data of its own. Virtual tables'
    shadow tables are left out: their row counts follow internal layout
    (full-text segments merge, vectors are stored in blocks), not content."""
    rows = db.execute("SELECT name, coalesce(sql, '') FROM sqlite_master "
                      "WHERE type = 'table' ORDER BY name").fetchall()
    virtual = [name for name, sql in rows if sql.upper().startswith("CREATE VIRTUAL")]
    names = [name for name, sql in rows
             if not sql.upper().startswith("CREATE VIRTUAL")
             and not name.startswith("sqlite_")
             and not any(name.startswith(v + "_") for v in virtual)]
    return {name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in names}


def _integrity(db: sqlite3.Connection, quick: bool = False) -> str:
    rows = db.execute("PRAGMA quick_check" if quick else "PRAGMA integrity_check").fetchall()
    text = "; ".join(str(r[0]) for r in rows[:5])
    return "ok" if text == "ok" else text


def describe(path: Path, *, quick: bool = False, readonly: bool = True) -> dict:
    """Size, integrity and row counts. A backup's copy is opened read-only so
    checking it can never change it."""
    db = _connect(path, readonly=readonly)
    try:
        return {"bytes": path.stat().st_size, "integrity": _integrity(db, quick),
                "tables": _tables(db)}
    finally:
        db.close()


# --------------------------------------------------------------------- create

def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9.]+", "-", text.lower()).strip("-")[:48] or "backup"


def _lock():
    from .trigger import _exclusive

    config.ensure_data_home()
    return _exclusive(config.data_home() / ".backup.lock", timeout=LOCK_TIMEOUT)


def _private_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if not config.WINDOWS:
        path.chmod(0o700)


def _copy_db(src: Path, dst: Path) -> None:
    """A consistent copy through SQLite's backup API: one step, so the copy is
    a single moment of the source even while other processes write to it."""
    source = sqlite3.connect(src, timeout=60)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    config.secure_file(dst)


def snapshot(reason: str, *, manual: bool = False, label: str | None = None) -> Path | None:
    """Back up every store in the data home; return the backup's directory,
    or None when there is nothing to back up. Raises BackupError."""
    with _lock() as held:
        if not held:
            raise BackupError("another backup of this data home is still running")
        return _snapshot(reason, manual=manual, label=label)


def _snapshot(reason: str, *, manual: bool, label: str | None) -> Path | None:
    files, extras = stores(), _extras()
    if not files:
        return None
    base = root()
    _private_dir(base)
    size = sum(p.stat().st_size for p in files)
    size += sum(Path(str(p) + "-wal").stat().st_size for p in files
                if Path(str(p) + "-wal").exists())
    free = shutil.disk_usage(base).free
    if free < size + SPACE_MARGIN:
        raise BackupError(
            f"not enough free space in {base} for a backup: {size / 2**20:.0f} MB "
            f"needed, {free / 2**20:.0f} MB free. Free some space or set "
            "AGENT_HISTORY_BACKUP_DIR to another disk.")

    when = datetime.datetime.now(datetime.timezone.utc)
    name = f"{when.strftime('%Y%m%d-%H%M%S')}-{_slug(label or reason)}"
    final, n = base / name, 1
    while final.exists():
        n += 1
        final = base / f"{name}-{n}"
    work = base / f"{PARTIAL}{final.name}"
    if work.exists():
        shutil.rmtree(work)
    _private_dir(work)

    entries: dict[str, dict] = {}
    try:
        for path in files:
            copy = work / path.name
            _copy_db(path, copy)
            info = describe(copy)
            if info["integrity"] != "ok":
                raise BackupError(f"the copy of {path.name} failed its integrity "
                                  f"check ({info['integrity']}); the live store may "
                                  "be damaged — run `agent-history backup check`")
            entries[path.name] = {**info, "sha256": _sha256(copy)}
        copied = []
        for extra in extras:
            target = work / extra.name
            if extra.is_dir():
                shutil.copytree(extra, target)
            else:
                shutil.copy2(extra, target)
                config.secure_file(target)
            copied.append(extra.name)
        manifest = {
            "format": 1, "name": final.name, "reason": reason, "label": label,
            "manual": manual, "created": when.isoformat(timespec="microseconds"),
            "version": __version__, "host": socket.gethostname(),
            "data_home": str(config.data_home()), "files": entries, "extras": copied,
        }
        manifest_path = work / MANIFEST
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        config.secure_file(manifest_path)
        os.replace(work, final)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    if not manual:
        prune()
    return final


# ----------------------------------------------------------------------- read

def read_manifest(path: Path) -> dict:
    try:
        return json.loads((path / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BackupError(f"{path} is not a backup (no readable {MANIFEST}: {exc})") from None


def backups() -> list[tuple[Path, dict]]:
    """Complete backups of this data home, oldest first."""
    base = root()
    if not base.is_dir():
        return []
    found = []
    for path in base.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        try:
            found.append((path, read_manifest(path)))
        except BackupError:
            continue
    return sorted(found, key=lambda item: (item[1].get("created") or "", item[0].name))


def resolve(name: str) -> Path:
    """A backup by directory name, path, or ``latest``."""
    if name == "latest":
        found = backups()
        if not found:
            raise BackupError(f"no backups in {root()}")
        return found[-1][0]
    path = Path(name).expanduser()
    if not path.is_absolute():
        path = root() / name
    read_manifest(path)
    return path


def prune(keep_count: int | None = None) -> list[Path]:
    """Delete automatic backups beyond the newest ``keep_count``; backups made
    by hand are never deleted here. Leftover partial copies go too."""
    keep_count = keep() if keep_count is None else keep_count
    automatic = [path for path, manifest in backups() if not manifest.get("manual")]
    doomed = automatic[:-keep_count] if len(automatic) > keep_count else []
    for path in doomed:
        shutil.rmtree(path, ignore_errors=True)
    base = root()
    if base.is_dir():
        cutoff = time.time() - 86400
        for path in base.iterdir():
            if path.name.startswith(PARTIAL) and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
    return doomed


def verify(path: Path) -> list[str]:
    """Problems with a backup itself: a file missing, changed since it was
    written, or failing SQLite's integrity check. Empty when intact."""
    manifest = read_manifest(path)
    problems = []
    for name, entry in manifest.get("files", {}).items():
        copy = path / name
        if not copy.exists():
            problems.append(f"{name}: missing")
            continue
        if _sha256(copy) != entry.get("sha256"):
            problems.append(f"{name}: changed since the backup was made (SHA-256 differs)")
            continue
        state = describe(copy)["integrity"]
        if state != "ok":
            problems.append(f"{name}: integrity check failed ({state})")
    return problems


# ---------------------------------------------------------------------- check

@dataclass
class Report:
    against: str | None = None
    problems: list[str] = field(default_factory=list)
    losses: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    stores: dict[str, dict] = field(default_factory=dict)

    def ok(self, no_loss: bool = False) -> bool:
        return not self.problems and not (no_loss and self.losses)

    def as_dict(self) -> dict:
        return {"against": self.against, "problems": self.problems,
                "losses": self.losses, "changes": self.changes, "stores": self.stores}


def _index_views(path: Path, deep: bool) -> tuple[dict, list[str]]:
    """The index holds each chunk three times: the row, its full-text entry
    and its vector. They must agree."""
    db = _connect(path)
    problems: list[str] = []
    views: dict = {}
    try:
        names = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
        if "chunks" not in names:
            return views, problems
        views["chunks"] = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        if "fts_chunks_docsize" in names:
            views["fulltext"] = db.execute(
                "SELECT count(*) FROM fts_chunks_docsize").fetchone()[0]
            if views["fulltext"] != views["chunks"]:
                problems.append(f"index.db: {views['chunks']} chunks but "
                                f"{views['fulltext']} full-text entries")
        if "vec_chunks" in names:
            try:
                views["vectors"] = db.execute(
                    "SELECT count(*) FROM vec_chunks").fetchone()[0]
            except sqlite3.OperationalError:
                views["vectors"] = None  # sqlite-vec not loadable here
            if views["vectors"] is not None and views["vectors"] != views["chunks"]:
                problems.append(f"index.db: {views['chunks']} chunks but "
                                f"{views['vectors']} vectors")
        if "vec_chunks_new" in names:
            views["reembed_in_progress"] = True
        if deep and "fts_chunks" in names:
            try:
                db.execute("INSERT INTO fts_chunks(fts_chunks, rank) "
                           "VALUES('integrity-check', 1)")
                db.rollback()
                views["fulltext_check"] = "ok"
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc) or "busy" in str(exc):
                    views["fulltext_check"] = "skipped (index busy)"
                else:
                    views["fulltext_check"] = str(exc)
                    problems.append(f"index.db: full-text index check failed ({exc})")
            except sqlite3.DatabaseError as exc:
                views["fulltext_check"] = str(exc)
                problems.append(f"index.db: full-text index check failed ({exc})")
    finally:
        db.close()
    return views, problems


def check(*, against: str | Path | None = "latest", quick: bool = False) -> Report:
    """Check the live stores, and compare their row counts with a backup
    (``latest`` by default; None for no comparison)."""
    report = Report()
    for path in stores():
        try:
            info = describe(path, quick=quick, readonly=False)
        except sqlite3.DatabaseError as exc:
            report.problems.append(f"{path.name}: cannot be read ({exc})")
            continue
        report.stores[path.name] = info
        if info["integrity"] != "ok":
            report.problems.append(f"{path.name}: integrity check failed ({info['integrity']})")
        if path.name == "index.db":
            views, problems = _index_views(path, deep=not quick)
            info["views"] = views
            report.problems.extend(problems)

    baseline = None
    if against is not None:
        try:
            baseline = against if isinstance(against, Path) else resolve(str(against))
        except BackupError:
            if against != "latest":
                raise
    if baseline is not None:
        manifest = read_manifest(baseline)
        report.against = manifest.get("name") or baseline.name
        for name, entry in manifest.get("files", {}).items():
            live = report.stores.get(name)
            if live is None:
                report.losses.append(f"{name}: in the backup but missing now")
                continue
            for table, before in entry.get("tables", {}).items():
                after = live["tables"].get(table)
                if after is None:
                    report.losses.append(f"{name}: table {table} ({before} rows) is gone")
                elif after < before:
                    report.losses.append(f"{name}: {table} {before} → {after} "
                                         f"({after - before})")
                elif after > before:
                    report.changes.append(f"{name}: {table} {before} → {after} "
                                          f"(+{after - before})")
    return report


def print_report(report: Report, *, no_loss: bool = False) -> None:
    for name, info in report.stores.items():
        views = info.get("views") or {}
        extra = ""
        if views.get("chunks") is not None:
            extra = (f"  chunks {views['chunks']}, full-text {views.get('fulltext', '-')}, "
                     f"vectors {views.get('vectors', '-')}")
            if views.get("fulltext_check"):
                extra += f", full-text check {views['fulltext_check']}"
        print(f"{name:18} {info['bytes'] / 2**20:8.1f} MB  integrity {info['integrity']}{extra}")
    if report.against:
        print(f"compared with backup {report.against}:")
        for line in report.changes:
            print(f"  grew   {line}")
        for line in report.losses:
            print(f"  FEWER  {line}")
        if not report.changes and not report.losses:
            print("  no table changed size")
    for line in report.problems:
        print(f"PROBLEM: {line}")
    state = "ok" if report.ok(no_loss) else "FAILED"
    print(f"integrity: {state}")


# ------------------------------------------------------------------- automatic

def _stamp_path() -> Path:
    return config.data_home() / STAMP


def written_by() -> str | None:
    try:
        return _stamp_path().read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def guard_upgrade() -> Path | None:
    """Back up the data home the first time this version writes to it.

    Cheap when the stamp already names this version (one small file read).
    Raises BackupError when a needed backup cannot be made, so a new version
    never changes a store it could not first copy."""
    if written_by() == __version__ or not auto_enabled():
        return None
    with _lock() as held:
        if not held:
            raise BackupError("another backup of this data home is still running")
        previous = written_by()
        if previous == __version__:
            return None
        made = None
        if stores():
            made = _snapshot(f"before {__version__} (was {previous or 'unknown'})",
                             manual=False, label=f"upgrade-to-{__version__}")
            print(f"backup: {made} (first write by agent-history {__version__})",
                  file=sys.stderr)
        stamp = _stamp_path()
        tmp = stamp.with_name(f"{STAMP}.{os.getpid()}.tmp")
        tmp.write_text(__version__ + "\n", encoding="utf-8")
        os.replace(tmp, stamp)
        return made


@contextlib.contextmanager
def guarded(reason: str):
    """Back up before an operation that deletes or rewrites rows; check the
    live data after it.

    Yields the backup's path (None with AGENT_HISTORY_AUTO_BACKUP=0 or
    nothing to back up). A failed backup raises BackupError before the
    operation starts; a failed check afterwards raises IntegrityError, naming
    the backup to restore."""
    if not auto_enabled():
        yield None
        return
    made = snapshot(reason)
    if made:
        print(f"backup: {made}", file=sys.stderr)
    yield made
    report = check(against=made, quick=True)
    if report.problems:
        raise IntegrityError(
            "the data failed its check after " + reason + ":\n  "
            + "\n  ".join(report.problems)
            + (f"\nrestore the copy made just before with: agent-history backup "
               f"restore {made.name} --yes" if made else ""))
    lost = "; ".join(report.losses)
    print(f"integrity: ok{f' (removed: {lost})' if lost else ''}", file=sys.stderr)
    if made and report.losses:
        print(f"note: the removed rows are still in backup {made.name} and older "
              "backups; delete those to erase them everywhere "
              "(agent-history backup delete NAME --yes)", file=sys.stderr)


# --------------------------------------------------------------------- restore

def restore(path: Path) -> Path | None:
    """Put a backup's stores back in place, after backing up what is there
    now. The indexing lock is held so no run writes in between; readers see
    the restored content through SQLite's own locking. Raises BackupError."""
    from .trigger import _exclusive

    problems = verify(path)
    if problems:
        raise BackupError("the backup is not intact; nothing was restored:\n  "
                          + "\n  ".join(problems))
    manifest = read_manifest(path)
    with _exclusive(config.lock_path(), timeout=LOCK_TIMEOUT) as held:
        if not held:
            raise BackupError("an indexing run holds the lock; retry when it finishes")
        before = snapshot("before restoring " + path.name, manual=True,
                          label=f"pre-restore-{path.name}") if stores() else None
        home = config.ensure_data_home()
        for name in manifest.get("files", {}):
            _copy_db(path / name, home / name)
        for name in manifest.get("extras", []):
            source, target = path / name, home / name
            if source.is_dir():
                # Queued batches: put back any that are missing, keep the rest.
                for item in source.rglob("*"):
                    dest = target / item.relative_to(source)
                    if item.is_dir():
                        dest.mkdir(parents=True, exist_ok=True)
                    elif not dest.exists():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, dest)
            else:
                shutil.copy2(source, target)
                config.secure_file(target)
    return before
